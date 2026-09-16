from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
import math
import re

import pandas as pd

from parsers import ParseResult


FINDING_COLUMNS = [
    "Severity", "Level", "Metric", "Identifier", "Legacy", "GFL", "Adjusted GFL",
    "Difference", "Difference %", "Status", "Likely Cause", "Confidence %", "Evidence"
]


def _num(v):
    try:
        if pd.isna(v):
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def _pct(diff, legacy):
    legacy = _num(legacy)
    if abs(legacy) < 1e-12:
        return None
    return 100.0 * _num(diff) / abs(legacy)


def _sev(diff, tol=1e-9):
    return "Info" if abs(_num(diff)) <= tol else "Warning"


def normalize_rule_set(values: Iterable[str]) -> set:
    out = set()
    for v in values:
        s = str(v).strip().upper()
        if not s:
            continue
        s = re.sub(r"^KEY\s*", "", s)
        out.add(s)
    return out


def apply_gfl_rules(df: pd.DataFrame, non_ridership_keys: set, non_ridership_ttps: set) -> pd.DataFrame:
    x = df.copy()
    non_ridership_keys = normalize_rule_set(non_ridership_keys)
    non_ridership_ttps = {str(v).strip().replace("TTP", "").strip() for v in non_ridership_ttps if str(v).strip()}
    x["adjusted_ridership"] = pd.to_numeric(x["ridership"], errors="coerce").fillna(0)
    if "key" in x:
        x.loc[x["key"].astype(str).str.upper().isin(non_ridership_keys), "adjusted_ridership"] = 0
    if "ttp" in x:
        x.loc[x["ttp"].astype(str).isin(non_ridership_ttps), "adjusted_ridership"] = 0
    return x


def apply_legacy_rules(df: pd.DataFrame, non_ridership_keys: set, non_ridership_ttps: set) -> pd.DataFrame:
    x = df.copy()
    non_ridership_keys = normalize_rule_set(non_ridership_keys)
    non_ridership_ttps = {str(v).strip().replace("TTP", "").strip() for v in non_ridership_ttps if str(v).strip()}
    x["expected_ridership"] = 0.0
    if "is_fare_event" in x:
        x.loc[x["is_fare_event"], "expected_ridership"] = 1.0
    if "key" in x:
        x.loc[x["key"].astype(str).str.upper().isin(non_ridership_keys), "expected_ridership"] = 0
    if "ttp" in x:
        x.loc[x["ttp"].astype(str).isin(non_ridership_ttps), "expected_ridership"] = 0
    return x


def add_finding(rows: List[dict], *, severity="Warning", level, metric, identifier, legacy, gfl,
                adjusted_gfl=None, status="Difference", cause="Unexplained discrepancy",
                confidence=60, evidence=""):
    compare_val = gfl if adjusted_gfl is None else adjusted_gfl
    diff = _num(compare_val) - _num(legacy)
    rows.append({
        "Severity": severity if abs(diff) > 1e-9 else "Info",
        "Level": level,
        "Metric": metric,
        "Identifier": identifier,
        "Legacy": legacy,
        "GFL": gfl,
        "Adjusted GFL": adjusted_gfl if adjusted_gfl is not None else gfl,
        "Difference": diff,
        "Difference %": _pct(diff, legacy),
        "Status": status if abs(diff) > 1e-9 else "Match",
        "Likely Cause": cause if abs(diff) > 1e-9 else "Matched",
        "Confidence %": confidence,
        "Evidence": evidence,
    })


def group_identifier(df: pd.DataFrame, id_prefix: str, value_col: str) -> pd.DataFrame:
    x = df[df["identifier"].astype(str).str.startswith(id_prefix)].copy()
    if x.empty:
        return pd.DataFrame(columns=["identifier", value_col])
    return x.groupby("identifier", dropna=False)[value_col].sum().reset_index()


def event_counts(df: pd.DataFrame, id_prefix: str) -> pd.DataFrame:
    x = df[df["identifier"].astype(str).str.startswith(id_prefix)].copy()
    if x.empty:
        return pd.DataFrame(columns=["identifier", "event_count"])
    return x.groupby("identifier").size().rename("event_count").reset_index()


def _outer_counts(legacy_df, gfl_df, prefix):
    lc = event_counts(legacy_df, prefix).rename(columns={"event_count": "legacy_count"})
    gc = event_counts(gfl_df, prefix).rename(columns={"event_count": "gfl_count"})
    c = lc.merge(gc, on="identifier", how="outer").fillna(0)
    if not c.empty:
        c["legacy_count"] = c["legacy_count"].astype(float)
        c["gfl_count"] = c["gfl_count"].astype(float)
    return c


def detect_route_reassignments(legacy_tx: pd.DataFrame, gfl: pd.DataFrame) -> pd.DataFrame:
    """Find exact positive/negative route offsets for the same KEY/TTP with matching system totals."""
    rows = []
    if legacy_tx is None or legacy_tx.empty:
        return pd.DataFrame()
    for prefix in ["KEY ", "TTP "]:
        ids = sorted(set(legacy_tx.loc[legacy_tx.identifier.str.startswith(prefix, na=False), "identifier"]) |
                     set(gfl.loc[gfl.identifier.str.startswith(prefix, na=False), "identifier"]))
        for ident in ids:
            l = legacy_tx[legacy_tx.identifier == ident].groupby("route").size().astype(float)
            g = gfl[gfl.identifier == ident].groupby("route").size().astype(float)
            if abs(l.sum() - g.sum()) > 1e-9:
                continue
            comp = pd.concat([l.rename("legacy"), g.rename("gfl")], axis=1).fillna(0)
            comp["delta"] = comp["gfl"] - comp["legacy"]
            pos = [[idx, float(v)] for idx, v in comp.loc[comp.delta > 0, "delta"].items()]
            neg = [[idx, float(-v)] for idx, v in comp.loc[comp.delta < 0, "delta"].items()]
            for p in pos:
                remain = p[1]
                for n in neg:
                    if remain <= 0 or n[1] <= 0:
                        continue
                    moved = min(remain, n[1])
                    if moved <= 0:
                        continue
                    confidence = 96 if moved == p[1] or moved == n[1] else 90
                    rows.append({
                        "identifier": ident,
                        "from_legacy_route": str(n[0]),
                        "to_gfl_route": str(p[0]),
                        "count": moved,
                        "confidence": confidence,
                        "evidence": f"Systemwide {ident} count matches; GFL route {p[0]} is +{moved:g} while legacy route {n[0]} is -{moved:g}."
                    })
                    remain -= moved
                    n[1] -= moved
    return pd.DataFrame(rows)


def reconcile(gfl_rid: ParseResult, gfl_rev: ParseResult, legacy_routesum: ParseResult,
              legacy_tx: Optional[ParseResult], *, non_ridership_keys: set, non_ridership_ttps: set):
    findings: List[dict] = []
    gfl = apply_gfl_rules(gfl_rid.data, non_ridership_keys, non_ridership_ttps)
    ltx = apply_legacy_rules(legacy_tx.data, non_ridership_keys, non_ridership_ttps) if legacy_tx and not legacy_tx.data.empty else pd.DataFrame()

    legacy_riders = _num(legacy_routesum.totals.get("ridership", 0))
    legacy_rev = _num(legacy_routesum.totals.get("total_revenue", legacy_routesum.totals.get("current_revenue", 0)))
    raw_gfl_riders = float(gfl["ridership"].sum())
    adj_gfl_riders = float(gfl["adjusted_ridership"].sum())
    gfl_revenue = _num(gfl_rev.totals.get("revenue", 0))
    gfl_amount = _num(gfl_rid.totals.get("amount_charged", 0))

    removed = raw_gfl_riders - adj_gfl_riders
    overall_cause = "Business-rule adjustment explains raw GFL ridership difference" if abs(adj_gfl_riders - legacy_riders) < 1e-9 and abs(raw_gfl_riders-legacy_riders) > 1e-9 else "Unexplained overall ridership difference"
    overall_conf = 100 if abs(adj_gfl_riders - legacy_riders) < 1e-9 else 70
    add_finding(findings, level="Overall", metric="Ridership", identifier="Total Ridership",
                legacy=legacy_riders, gfl=raw_gfl_riders, adjusted_gfl=adj_gfl_riders,
                cause=overall_cause, confidence=overall_conf,
                evidence=f"Raw GFL={raw_gfl_riders:g}; rules removed {removed:g}; adjusted GFL={adj_gfl_riders:g}.")

    rev_cause = "Matched total revenue" if abs(gfl_revenue-legacy_rev) < 0.01 else "Revenue total mismatch"
    add_finding(findings, level="Overall", metric="Revenue", identifier="Total Revenue",
                legacy=legacy_rev, gfl=gfl_revenue, adjusted_gfl=gfl_revenue,
                cause=rev_cause, confidence=100 if abs(gfl_revenue-legacy_rev)<0.01 else 75,
                evidence=f"Legacy total uses Current Revenue + Unclassified Revenue where available.")

    # GFL independent revenue cross-check against Amount Charged contained in ridership raw export.
    add_finding(findings, level="Validation", metric="Revenue", identifier="GFL Revenue File vs Ridership Amount Charged",
                legacy=gfl_amount, gfl=gfl_revenue, adjusted_gfl=gfl_revenue,
                cause="Independent GFL exports reconcile" if abs(gfl_amount-gfl_revenue)<0.01 else "GFL exports disagree",
                confidence=100, evidence="Compares GFL ridership raw Amount Charged sum to GFL revenue raw Revenue sum.")

    # Key / TTP count comparisons from transaction detail when available.
    if not ltx.empty:
        for prefix, level in [("KEY ", "Key"), ("TTP ", "TTP")]:
            comp = _outer_counts(ltx, gfl, prefix)
            for _, r in comp.iterrows():
                ident = r["identifier"]
                lc, gc = float(r["legacy_count"]), float(r["gfl_count"])
                if prefix == "KEY ":
                    key = ident.replace("KEY ", "")
                    expected_legacy_rid = 0 if key.upper() in normalize_rule_set(non_ridership_keys) else lc
                    gfl_rows = gfl[gfl.identifier == ident]
                    raw_rid = float(gfl_rows.ridership.sum())
                    adj_rid = float(gfl_rows.adjusted_ridership.sum())
                    if abs(gc-lc)<1e-9 and abs(raw_rid-expected_legacy_rid)>1e-9 and abs(adj_rid-expected_legacy_rid)<1e-9:
                        cause = "Ridership configuration/business rule"
                        conf = 99
                        ev = f"Event counts match ({gc:g}); raw GFL ridership={raw_rid:g}; rule-adjusted ridership={adj_rid:g}."
                    elif abs(gc-lc)<1e-9:
                        cause = "Matched key event count"
                        conf = 100
                        ev = f"Legacy and GFL both contain {gc:g} events."
                    else:
                        cause = "Key event-count mismatch"
                        conf = 80
                        ev = f"Legacy events={lc:g}; GFL events={gc:g}."
                    add_finding(findings, level="Key", metric="Event Count", identifier=ident,
                                legacy=lc, gfl=gc, adjusted_gfl=gc, cause=cause, confidence=conf, evidence=ev)
                else:
                    cause = "Matched TTP event count" if abs(gc-lc)<1e-9 else "TTP event-count mismatch"
                    add_finding(findings, level="TTP", metric="Event Count", identifier=ident,
                                legacy=lc, gfl=gc, adjusted_gfl=gc, cause=cause,
                                confidence=100 if abs(gc-lc)<1e-9 else 80,
                                evidence=f"Legacy events={lc:g}; GFL events={gc:g}.")

    # Route ridership authoritative comparison (ROUTESUM vs adjusted GFL)
    if not legacy_routesum.data.empty:
        lr = legacy_routesum.data.groupby("route", dropna=False).agg(legacy_ridership=("ridership", "sum"), legacy_revenue=("current_revenue", "sum"), legacy_unclass=("unclassified_revenue", "sum")).reset_index()
        gr = gfl.groupby("route", dropna=False)["adjusted_ridership"].sum().rename("gfl_ridership").reset_index()
        rr = gfl_rev.data.groupby("route", dropna=False)["revenue"].sum().rename("gfl_revenue").reset_index()
        rc = lr.merge(gr, on="route", how="outer").merge(rr, on="route", how="outer").fillna(0)
        rc["legacy_total_revenue"] = rc["legacy_revenue"] + rc["legacy_unclass"]
        for _, r in rc.iterrows():
            route = str(r["route"])
            ld, gd = float(r["legacy_ridership"]), float(r["gfl_ridership"])
            cause = "Matched route ridership" if abs(gd-ld)<1e-9 else "Route allocation difference or missing/extra transactions"
            conf = 100 if abs(gd-ld)<1e-9 else 65
            add_finding(findings, level="Route", metric="Ridership", identifier=f"Route {route}", legacy=ld, gfl=gd, adjusted_gfl=gd, cause=cause, confidence=conf,
                        evidence="Legacy route total comes from ROUTESUM; GFL route total uses rule-adjusted ridership.")
            lrev, grev = float(r["legacy_total_revenue"]), float(r["gfl_revenue"])
            add_finding(findings, level="Route", metric="Revenue", identifier=f"Route {route}", legacy=lrev, gfl=grev, adjusted_gfl=grev,
                        cause="Matched route revenue" if abs(grev-lrev)<0.01 else "Route revenue allocation difference",
                        confidence=100 if abs(grev-lrev)<0.01 else 70,
                        evidence="Legacy route revenue = current + unclassified; GFL from revenue raw export.")

    reassign = detect_route_reassignments(ltx, gfl) if not ltx.empty else pd.DataFrame()
    if not reassign.empty:
        for _, r in reassign.iterrows():
            add_finding(findings, level="Route/Product", metric="Event Count", identifier=f"{r['identifier']}: Legacy Route {r['from_legacy_route']} → GFL Route {r['to_gfl_route']}",
                        legacy=r["count"], gfl=r["count"], adjusted_gfl=r["count"], status="Explained",
                        cause="Possible route reassignment / Data Edit", confidence=int(r["confidence"]), evidence=r["evidence"])

    # Run / Trip derived comparisons. These are lower-confidence because Legacy transaction detail can contain issuance/nonboarding events.
    runtrip = pd.DataFrame()
    if not ltx.empty:
        ld = ltx[ltx["is_fare_event"]].groupby(["route", "run", "trip"], dropna=False)["expected_ridership"].sum().rename("legacy_derived_ridership")
        gd = gfl.groupby(["route", "run", "trip"], dropna=False)["adjusted_ridership"].sum().rename("gfl_adjusted_ridership")
        runtrip = pd.concat([ld, gd], axis=1).fillna(0).reset_index()
        runtrip["difference"] = runtrip["gfl_adjusted_ridership"] - runtrip["legacy_derived_ridership"]
        for _, r in runtrip[runtrip.difference.abs() > 1e-9].iterrows():
            add_finding(findings, level="Route/Run/Trip", metric="Derived Ridership", identifier=f"Route {r['route']} / Run {r['run']} / Trip {r['trip']}",
                        legacy=float(r["legacy_derived_ridership"]), gfl=float(r["gfl_adjusted_ridership"]), adjusted_gfl=float(r["gfl_adjusted_ridership"]),
                        cause="Possible route/run/trip reassignment, filter difference, or unmatched fare event", confidence=70,
                        evidence="Legacy value is derived from Transaction Detail fare identifiers and current ridership rules; use supporting transactions to confirm.")

    findings_df = pd.DataFrame(findings, columns=FINDING_COLUMNS)
    # Sort differences first, then hierarchy.
    if not findings_df.empty:
        findings_df["_absdiff"] = pd.to_numeric(findings_df["Difference"], errors="coerce").abs().fillna(0)
        findings_df = findings_df.sort_values(["_absdiff", "Level", "Identifier"], ascending=[False, True, True]).drop(columns="_absdiff").reset_index(drop=True)

    return {
        "findings": findings_df,
        "gfl_adjusted": gfl,
        "legacy_tx_adjusted": ltx,
        "route_reassignments": reassign,
        "run_trip": runtrip,
        "summary": {
            "legacy_ridership": legacy_riders,
            "gfl_raw_ridership": raw_gfl_riders,
            "gfl_adjusted_ridership": adj_gfl_riders,
            "legacy_revenue": legacy_rev,
            "gfl_revenue": gfl_revenue,
            "gfl_ridership_amount_charged": gfl_amount,
            "ridership_rule_adjustment": removed,
        }
    }
