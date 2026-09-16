from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from inference import RuleInference, infer_legacy_ridership_rules
from large_csv import LargeCSVProfile
from legacy_routesum import LegacyRouteSum


FINDING_COLUMNS = [
    "Severity", "Level", "Metric", "Identifier", "Legacy", "GFL", "Difference", "Difference %",
    "Status", "Likely Cause", "Confidence %", "Evidence"
]


def _pct(diff, legacy):
    if abs(float(legacy)) < 1e-12:
        return np.nan
    return float(diff) / float(legacy) * 100.0


def _severity(diff, explained=False):
    if abs(float(diff)) < 1e-9:
        return "Info"
    return "Warning" if explained else "Error"


def add_finding(rows, *, level, metric, identifier, legacy, gfl, cause, confidence, evidence,
                status=None, severity=None):
    legacy = float(legacy) if legacy is not None and not pd.isna(legacy) else 0.0
    gfl = float(gfl) if gfl is not None and not pd.isna(gfl) else 0.0
    diff = gfl - legacy
    explained = any(x in cause.lower() for x in ["rule", "reassignment", "data edit", "matched", "reconcile", "display"])
    rows.append({
        "Severity": severity or _severity(diff, explained),
        "Level": level,
        "Metric": metric,
        "Identifier": identifier,
        "Legacy": legacy,
        "GFL": gfl,
        "Difference": diff,
        "Difference %": _pct(diff, legacy),
        "Status": status or ("Match" if abs(diff) < 1e-9 else ("Explained" if explained else "Unresolved")),
        "Likely Cause": cause,
        "Confidence %": int(confidence),
        "Evidence": evidence,
    })


def _legacy_category_counts(routesum: LegacyRouteSum, legacy_tx: Optional[LargeCSVProfile]) -> pd.DataFrame:
    frames = []
    have_keys = not routesum.key_counts.empty
    have_ttps = not routesum.ttp_counts.empty
    if have_keys:
        frames.append(routesum.key_counts.rename(columns={"count": "legacy_count"}))
    if have_ttps:
        frames.append(routesum.ttp_counts.rename(columns={"count": "legacy_count"}))
    if legacy_tx is not None and not legacy_tx.route_identifier.empty and (not have_keys or not have_ttps):
        tx = legacy_tx.route_identifier.rename(columns={"event_count": "legacy_count"})
        if not have_keys:
            frames.append(tx[tx["identifier"].str.startswith("KEY ", na=False)][["route", "identifier", "legacy_count"]])
        if not have_ttps:
            frames.append(tx[tx["identifier"].str.startswith("TTP ", na=False)][["route", "identifier", "legacy_count"]])
    if not routesum.route_summary.empty and "preset" in routesum.route_summary.columns:
        p = routesum.route_summary[["route", "preset"]].rename(columns={"preset": "legacy_count"}).copy()
        p["identifier"] = "PRESET"
        frames.append(p[["route", "identifier", "legacy_count"]])
    if frames:
        frames = [x for x in frames if x is not None and not x.empty]
        if frames:
            return pd.concat(frames, ignore_index=True).groupby(["route", "identifier"], as_index=False)["legacy_count"].sum()
    return pd.DataFrame(columns=["route", "identifier", "legacy_count"])


def _rule_map(inference: RuleInference) -> dict[str, bool]:
    if inference.rules.empty:
        return {}
    return dict(zip(inference.rules["identifier"], inference.rules["inferred_counts_ridership"].astype(bool)))


def _normalize_gfl_ridership(gfl: LargeCSVProfile, rule_map: dict[str, bool]):
    ident = gfl.identifier_summary.copy()
    if ident.empty:
        return float(gfl.totals.get("ridership", 0)), pd.DataFrame()
    ident["legacy_rule"] = ident["identifier"].map(rule_map)
    ident["normalized_ridership"] = ident["ridership"]
    mask = ident["legacy_rule"].eq(False)
    ident.loc[mask, "normalized_ridership"] = 0.0
    raw = float(gfl.totals.get("ridership", 0))
    removed = float((ident["ridership"] - ident["normalized_ridership"]).sum())
    return raw - removed, ident


def _normalized_route_gfl(gfl: LargeCSVProfile, rule_map: dict[str, bool]) -> pd.DataFrame:
    d = gfl.route_identifier.copy()
    if d.empty:
        return pd.DataFrame(columns=["route", "gfl_raw_ridership", "gfl_normalized_ridership"])
    d["legacy_rule"] = d["identifier"].map(rule_map)
    d["normalized"] = d["ridership"]
    d.loc[d["legacy_rule"].eq(False), "normalized"] = 0.0
    return d.groupby("route", as_index=False).agg(
        gfl_raw_ridership=("ridership", "sum"), gfl_normalized_ridership=("normalized", "sum")
    )


def detect_route_reassignments(routesum: LegacyRouteSum, gfl: LargeCSVProfile, legacy_tx: Optional[LargeCSVProfile]) -> pd.DataFrame:
    legacy = _legacy_category_counts(routesum, legacy_tx)
    if legacy.empty or gfl.route_identifier.empty:
        return pd.DataFrame(columns=["identifier", "legacy_route", "gfl_route", "count", "confidence", "evidence"])
    g = gfl.route_identifier[["route", "identifier", "event_count"]].rename(columns={"event_count": "gfl_count"})
    c = legacy.merge(g, on=["route", "identifier"], how="outer").fillna(0)
    c["difference"] = c["gfl_count"] - c["legacy_count"]
    results = []
    for ident, group in c.groupby("identifier"):
        sys_legacy = float(group["legacy_count"].sum())
        sys_gfl = float(group["gfl_count"].sum())
        if abs(sys_legacy - sys_gfl) > 1e-9:
            continue
        positives = [[str(r.route), float(r.difference)] for _, r in group[group.difference > 0].iterrows()]
        negatives = [[str(r.route), float(-r.difference)] for _, r in group[group.difference < 0].iterrows()]
        pi = ni = 0
        while pi < len(positives) and ni < len(negatives):
            pr, pv = positives[pi]
            nr, nv = negatives[ni]
            amount = min(pv, nv)
            if amount > 0:
                confidence = 90
                evidence = f"Systemwide {ident} count matches ({sys_gfl:g}). GFL Route {pr} is +{amount:g} while Legacy Route {nr} is +{amount:g} relative to GFL."
                # Stronger evidence if the same route/run/trip/identifier groups appear in transaction detail.
                if legacy_tx is not None and not legacy_tx.route_run_trip_identifier.empty and not gfl.route_run_trip_identifier.empty:
                    l = legacy_tx.route_run_trip_identifier
                    gg = gfl.route_run_trip_identifier
                    la = l[(l.identifier == ident) & (l.route.astype(str) == nr)].groupby(["run", "trip"])["event_count"].sum()
                    ga = gg[(gg.identifier == ident) & (gg.route.astype(str) == pr)].groupby(["run", "trip"])["event_count"].sum()
                    common = la.to_frame("legacy").join(ga.to_frame("gfl"), how="inner")
                    matching = float(common[["legacy", "gfl"]].min(axis=1).sum()) if not common.empty else 0.0
                    if matching >= amount * 0.8:
                        confidence = 98
                        evidence += f" Matching run/trip evidence accounts for approximately {matching:g} events."
                results.append({
                    "identifier": ident, "legacy_route": nr, "gfl_route": pr,
                    "count": amount, "confidence": confidence, "evidence": evidence,
                })
            positives[pi][1] -= amount
            negatives[ni][1] -= amount
            if positives[pi][1] <= 1e-9:
                pi += 1
            if negatives[ni][1] <= 1e-9:
                ni += 1
    return pd.DataFrame(results)


def reconcile(gfl: LargeCSVProfile, routesum: LegacyRouteSum,
              legacy_tx: Optional[LargeCSVProfile] = None,
              gfl_revenue: Optional[LargeCSVProfile] = None) -> dict:
    findings = []
    inference = infer_legacy_ridership_rules(routesum, legacy_tx)
    rules = _rule_map(inference)

    legacy_riders = float(routesum.totals.get("ridership", 0))
    raw_gfl = float(gfl.totals.get("ridership", 0))
    normalized_gfl, gfl_ident = _normalize_gfl_ridership(gfl, rules)
    removed = raw_gfl - normalized_gfl

    add_finding(
        findings, level="Overall", metric="Ridership", identifier="Raw total ridership",
        legacy=legacy_riders, gfl=raw_gfl,
        cause="Raw ridership difference" if abs(raw_gfl - legacy_riders) > 1e-9 else "Matched raw ridership",
        confidence=100, evidence="Legacy total comes from ROUTESUM; GFL total is the sum of the Ridership column."
    )
    if rules:
        cause = "Ridership-rule differences fully explain total" if abs(normalized_gfl - legacy_riders) < 1e-9 else "Rule normalization explains part of total difference"
        add_finding(
            findings, level="Overall", metric="Ridership", identifier="GFL normalized to inferred Legacy rules",
            legacy=legacy_riders, gfl=normalized_gfl, cause=cause,
            confidence=inference.confidence,
            evidence=f"Automatic Legacy rule inference removed {removed:g} GFL riders; no manual ridership rules were supplied."
        )

    legacy_revenue = float(routesum.totals.get("total_revenue", 0))
    gfl_amount = float(gfl.totals.get("amount_charged", 0))
    gfl_rev_total = float(gfl_revenue.totals.get("revenue", 0)) if gfl_revenue is not None else gfl_amount
    add_finding(
        findings, level="Overall", metric="Revenue", identifier="Total revenue",
        legacy=legacy_revenue, gfl=gfl_rev_total,
        cause="Matched total revenue" if abs(gfl_rev_total - legacy_revenue) < 0.01 else "Revenue total mismatch",
        confidence=100 if abs(gfl_rev_total - legacy_revenue) < 0.01 else 80,
        evidence="GFL uses optional Revenue Raw Data when supplied; otherwise it uses Amount Charged from the Ridership Raw Data export. Legacy uses Current + Unclassified Revenue."
    )
    if gfl_revenue is not None:
        add_finding(
            findings, level="Validation", metric="Revenue", identifier="GFL Revenue file vs Ridership Amount Charged",
            legacy=gfl_amount, gfl=float(gfl_revenue.totals.get("revenue", 0)),
            cause="Independent GFL exports reconcile" if abs(float(gfl_revenue.totals.get("revenue", 0)) - gfl_amount) < 0.01 else "GFL exports disagree",
            confidence=100,
            evidence="Optional cross-check only; the Revenue Raw Data file is not required."
        )

    # Legacy internal fit and hidden ridership contributions.
    if not inference.route_fit.empty:
        residual = float(inference.route_fit["residual"].abs().sum())
        add_finding(
            findings, level="Legacy Internal", metric="Ridership Reconstruction", identifier="ROUTESUM category reconstruction",
            legacy=legacy_riders, gfl=legacy_riders - residual,
            cause="Legacy category counts reconcile to overall/route ridership" if inference.exact_fit else "Legacy category tables do not fully reconstruct ridership",
            confidence=inference.confidence,
            evidence=f"Rules inferred from {inference.source}. Absolute route-level reconstruction residual: {residual:g}."
        )

    legacy_counts = _legacy_category_counts(routesum, legacy_tx)
    legacy_ident_totals = legacy_counts.groupby("identifier")["legacy_count"].sum() if not legacy_counts.empty else pd.Series(dtype=float)
    gfl_totals = gfl.identifier_summary.set_index("identifier") if not gfl.identifier_summary.empty else pd.DataFrame()
    rule_table = inference.rules.set_index("identifier") if not inference.rules.empty else pd.DataFrame()
    identifiers = sorted(set(legacy_ident_totals.index.tolist()) | set(gfl.identifier_summary.get("identifier", pd.Series(dtype=str)).tolist()))
    for ident in identifiers:
        if not (ident.startswith("KEY ") or ident.startswith("TTP ") or ident == "PRESET"):
            continue
        lc = float(legacy_ident_totals.get(ident, 0))
        if not gfl_totals.empty and ident in gfl_totals.index:
            row = gfl_totals.loc[ident]
            if isinstance(row, pd.DataFrame):
                gc = float(row["event_count"].sum()); grid = float(row["ridership"].sum())
            else:
                gc = float(row.get("event_count", 0)); grid = float(row.get("ridership", 0))
        else:
            gc = grid = 0.0
        if abs(gc - lc) < 1e-9:
            cause = "Matched event count"
            conf = 100
        else:
            cause = "Event-count discrepancy"
            conf = 85
        add_finding(findings, level="Key" if ident.startswith("KEY ") else ("TTP" if ident.startswith("TTP ") else "Preset"),
                    metric="Event Count", identifier=ident, legacy=lc, gfl=gc, cause=cause, confidence=conf,
                    evidence=f"Legacy count={lc:g}; GFL raw-event count={gc:g}.")

        if not rule_table.empty and ident in rule_table.index and gc > 0:
            rr = rule_table.loc[ident]
            legacy_rate = 1.0 if bool(rr["inferred_counts_ridership"]) else 0.0
            gfl_rate = grid / gc if gc else 0.0
            if abs(gfl_rate - legacy_rate) < 1e-9:
                cause = "Matched ridership behavior"
                conf = inference.confidence
            else:
                cause = "Ridership-rule difference between GFL and Legacy"
                conf = min(99, inference.confidence)
            add_finding(findings, level="Key" if ident.startswith("KEY ") else "TTP",
                        metric="Ridership Rate", identifier=ident, legacy=legacy_rate, gfl=gfl_rate,
                        cause=cause, confidence=conf,
                        evidence=f"Legacy rule is inferred from route/overall totals; GFL contributed {grid:g} riders across {gc:g} events.")

    if not inference.rules.empty:
        hidden = inference.rules[inference.rules["legacy_display_mismatch"] == True]
        for _, r in hidden.iterrows():
            ident = r["identifier"]
            displayed = float(r["legacy_displayed_key_ridership"])
            inferred = float(r["inferred_legacy_ridership"])
            add_finding(
                findings, level="Legacy Internal", metric="Key Ridership Display", identifier=ident,
                legacy=displayed, gfl=inferred,
                cause="Legacy detailed key-ridership section does not reflect contribution required by overall/route totals",
                confidence=inference.confidence,
                evidence=f"Legacy key presses={r['legacy_event_count']:g}; detailed key-ridership page shows {displayed:g}, but route/overall totals require {inferred:g}."
            )

    # Route comparison after applying automatically inferred Legacy rules to GFL.
    norm_route = _normalized_route_gfl(gfl, rules)
    if not routesum.route_summary.empty:
        lr = routesum.route_summary[["route", "ridership", "current_revenue", "unclassified_revenue"]].copy()
        lr["legacy_revenue"] = lr["current_revenue"] + lr["unclassified_revenue"]
        rc = lr.merge(norm_route, on="route", how="outer").fillna(0)
        gfl_route_rev = (gfl_revenue.route_revenue if gfl_revenue is not None else gfl.route_revenue).rename(columns={"revenue": "gfl_revenue"})
        rc = rc.merge(gfl_route_rev, on="route", how="outer").fillna(0)
        for _, r in rc.iterrows():
            ld, gd = float(r["ridership"]), float(r["gfl_normalized_ridership"])
            add_finding(
                findings, level="Route", metric="Ridership", identifier=f"Route {r['route']}", legacy=ld, gfl=gd,
                cause="Matched route ridership" if abs(gd-ld)<1e-9 else "Route allocation difference, Data Edit, or missing/extra transactions",
                confidence=100 if abs(gd-ld)<1e-9 else 72,
                evidence="GFL route ridership is normalized using automatically inferred Legacy ridership behavior."
            )
            add_finding(
                findings, level="Route", metric="Revenue", identifier=f"Route {r['route']}", legacy=float(r["legacy_revenue"]), gfl=float(r["gfl_revenue"]),
                cause="Matched route revenue" if abs(float(r["gfl_revenue"])-float(r["legacy_revenue"]))<0.01 else "Route revenue allocation difference",
                confidence=100 if abs(float(r["gfl_revenue"])-float(r["legacy_revenue"]))<0.01 else 75,
                evidence="Legacy route revenue = Current + Unclassified Revenue."
            )

    reassign = detect_route_reassignments(routesum, gfl, legacy_tx)
    for _, r in reassign.iterrows():
        add_finding(
            findings, level="Route/Product", metric="Event Count",
            identifier=f"{r['identifier']}: Legacy Route {r['legacy_route']} → GFL Route {r['gfl_route']}",
            legacy=float(r["count"]), gfl=float(r["count"]),
            cause="Possible route reassignment / Data Edit", confidence=int(r["confidence"]),
            evidence=r["evidence"], status="Explained", severity="Warning"
        )

    # Run/trip derived comparison using transaction detail if supplied.
    runtrip = pd.DataFrame()
    if legacy_tx is not None and not legacy_tx.route_run_trip_identifier.empty:
        l = legacy_tx.route_run_trip_identifier.copy()
        l["counts_ridership"] = l["identifier"].map(rules)
        l = l[l["counts_ridership"].eq(True)]
        ld = l.groupby(["route", "run", "trip"], as_index=False)["event_count"].sum().rename(columns={"event_count": "legacy_derived_ridership"})
        gd = gfl.route_run_trip.copy()
        # Start with raw run/trip ridership, then remove inferred non-rider identifiers at the same route/run/trip.
        gi = gfl.route_run_trip_identifier.copy()
        gi["counts_ridership"] = gi["identifier"].map(rules)
        subtract = gi[gi["counts_ridership"].eq(False)].groupby(["route", "run", "trip"], as_index=False)["ridership"].sum().rename(columns={"ridership": "remove"})
        gd = gd.merge(subtract, on=["route", "run", "trip"], how="left").fillna({"remove": 0})
        gd["gfl_normalized_ridership"] = gd["ridership"] - gd["remove"]
        runtrip = ld.merge(gd[["route", "run", "trip", "gfl_normalized_ridership"]], on=["route", "run", "trip"], how="outer").fillna(0)
        runtrip["difference"] = runtrip["gfl_normalized_ridership"] - runtrip["legacy_derived_ridership"]
        for _, r in runtrip[runtrip["difference"].abs() > 1e-9].iterrows():
            add_finding(
                findings, level="Route/Run/Trip", metric="Derived Ridership",
                identifier=f"Route {r['route']} / Run {r['run']} / Trip {r['trip']}",
                legacy=float(r["legacy_derived_ridership"]), gfl=float(r["gfl_normalized_ridership"]),
                cause="Possible route/run/trip reassignment, report filter difference, or unmatched event",
                confidence=72,
                evidence="Legacy Transaction Detail has no direct Ridership column here; value is derived from inferred Legacy fare-category rules."
            )

    f = pd.DataFrame(findings, columns=FINDING_COLUMNS)
    if not f.empty:
        f["_abs"] = pd.to_numeric(f["Difference"], errors="coerce").abs().fillna(0)
        f = f.sort_values(["_abs", "Level", "Identifier"], ascending=[False, True, True]).drop(columns="_abs").reset_index(drop=True)

    return {
        "findings": f,
        "rule_inference": inference,
        "route_reassignments": reassign,
        "run_trip": runtrip,
        "summary": {
            "legacy_ridership": legacy_riders,
            "gfl_raw_ridership": raw_gfl,
            "gfl_legacy_rule_normalized_ridership": normalized_gfl,
            "inferred_rule_adjustment": removed,
            "legacy_revenue": legacy_revenue,
            "gfl_revenue": gfl_rev_total,
            "gfl_amount_charged": gfl_amount,
            "rule_confidence": inference.confidence,
            "rule_exact_fit": inference.exact_fit,
            "rule_source": inference.source,
            "gfl_rows": int(gfl.totals.get("rows", 0)),
            "legacy_transaction_rows": int(legacy_tx.totals.get("rows", 0)) if legacy_tx is not None else 0,
        },
    }
