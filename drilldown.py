from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from inference import RuleInference
from large_csv import LargeCSVProfile
from legacy_routesum import LegacyRouteSum
from transaction_match import match_remaining_transactions


def _norm_date(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, errors="coerce")
    out = parsed.dt.strftime("%Y-%m-%d")
    return out.fillna(values.astype(str).str.strip())


def _type_for_identifier(identifier: str) -> str:
    if str(identifier).startswith("KEY "):
        return "Key"
    if str(identifier).startswith("TTP "):
        return "TTP"
    if identifier == "PRESET":
        return "Preset"
    return "Other"


def _legacy_day_totals(routesum: LegacyRouteSum, legacy_tx: Optional[LargeCSVProfile], rules: dict[str, bool]):
    rd = getattr(routesum, "route_date_summary", pd.DataFrame())
    if rd is not None and not rd.empty and "date" in rd.columns:
        work = rd.copy()
        work["date"] = _norm_date(work["date"])
        work = work[work["date"].ne("")]
        if not work.empty:
            out = work.groupby("date", as_index=False)["ridership"].sum().rename(columns={"ridership": "legacy_ridership"})
            out["legacy_source"] = "ROUTESUM by route-date"
            out["legacy_daily_authoritative"] = True
            return out

    if legacy_tx is not None:
        # Use only fare-use / boarding-candidate Legacy transactions. This deliberately
        # excludes issuance/admin rows such as Issue card and Transfer issued, which can
        # otherwise make a daily ridership reconstruction look much larger than it is.
        work = getattr(legacy_tx, "boarding_day_route_identifier", pd.DataFrame()).copy()
        if work.empty:
            work = getattr(legacy_tx, "boarding_day_identifier", pd.DataFrame()).copy()
        if not work.empty:
            if "route" in work.columns:
                # ROUTESUM used by this tool is normally filtered to Route > 0.
                route_num = pd.to_numeric(work["route"], errors="coerce")
                work = work[(route_num.isna()) | (route_num > 0)]
            effective_rules = dict(rules)
            effective_rules["PRESET"] = True
            work["counts_ridership"] = work["identifier"].map(effective_rules)
            work = work[work["counts_ridership"].eq(True)]
            out = work.groupby("date", as_index=False)["event_count"].sum().rename(columns={"event_count": "legacy_ridership"})
            out["legacy_source"] = "Derived from Legacy fare-use transactions + inferred ridership behavior"
            out["legacy_daily_authoritative"] = False
            return out
    return pd.DataFrame(columns=["date", "legacy_ridership", "legacy_source", "legacy_daily_authoritative"])

def build_day_comparison(gfl: LargeCSVProfile, routesum: LegacyRouteSum, legacy_tx: Optional[LargeCSVProfile], rules: dict[str, bool]) -> pd.DataFrame:
    legacy = _legacy_day_totals(routesum, legacy_tx, rules)
    if legacy.empty or gfl.day_summary.empty:
        return pd.DataFrame(columns=["Date", "Legacy Ridership", "Legacy Source", "Legacy Daily Authoritative", "GFL Raw Ridership", "GFL Normalized Ridership", "Raw Difference", "Normalized Difference", "Status"])

    raw = gfl.day_summary[["date", "ridership"]].rename(columns={"ridership": "gfl_raw_ridership"})
    if not gfl.day_identifier.empty:
        norm = gfl.day_identifier.copy()
        effective_rules = dict(rules)
        effective_rules["PRESET"] = True
        norm["counts_ridership"] = norm["identifier"].map(effective_rules)
        norm["normalized"] = norm["ridership"]
        norm.loc[norm["counts_ridership"].eq(False), "normalized"] = 0.0
        norm = norm.groupby("date", as_index=False)["normalized"].sum().rename(columns={"normalized": "gfl_normalized_ridership"})
    else:
        norm = raw.rename(columns={"gfl_raw_ridership": "gfl_normalized_ridership"})

    out = legacy.merge(raw, on="date", how="outer").merge(norm, on="date", how="outer")
    for c in ["legacy_ridership", "gfl_raw_ridership", "gfl_normalized_ridership"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["legacy_source"] = out.get("legacy_source", "").fillna("No Legacy daily total available")
    out["legacy_daily_authoritative"] = out.get("legacy_daily_authoritative", False).fillna(False).astype(bool)

    # Only calculate a daily difference where a Legacy daily value actually exists.
    out["raw_difference"] = out["gfl_raw_ridership"] - out["legacy_ridership"]
    out["normalized_difference"] = out["gfl_normalized_ridership"] - out["legacy_ridership"]
    out["status"] = np.select(
        [
            out["legacy_ridership"].isna(),
            out["normalized_difference"].abs() < 1e-9,
            out["raw_difference"].abs() < 1e-9,
        ],
        ["Legacy daily total unavailable", "Reconciled after behavior normalization", "Raw match"],
        default="Needs drill-down",
    )
    return out.rename(columns={
        "date": "Date", "legacy_ridership": "Legacy Ridership", "gfl_raw_ridership": "GFL Raw Ridership",
        "raw_difference": "Raw Difference", "gfl_normalized_ridership": "GFL Normalized Ridership",
        "normalized_difference": "Normalized Difference", "legacy_source": "Legacy Source",
        "legacy_daily_authoritative": "Legacy Daily Authoritative", "status": "Status",
    }).sort_values("Date").reset_index(drop=True)

def build_day_identifier_comparison(gfl: LargeCSVProfile, legacy_tx: Optional[LargeCSVProfile], inference: RuleInference, rules: dict[str, bool]) -> pd.DataFrame:
    columns = ["Date", "Type", "Identifier", "Legacy Events", "GFL Ridership Records", "Record Difference", "Legacy Expected Riders", "GFL Riders", "Rider Difference", "Legacy Counts Ridership", "GFL Riders/Record", "Problem Summary", "Likely Cause", "Evidence"]
    legacy_day = pd.DataFrame() if legacy_tx is None else getattr(legacy_tx, "boarding_day_identifier", pd.DataFrame())
    if legacy_tx is None or legacy_day.empty or gfl.day_identifier.empty:
        return pd.DataFrame(columns=columns)
    l = legacy_day[["date", "identifier", "event_count"]].rename(columns={"event_count": "legacy_events"})
    g = gfl.day_identifier[["date", "identifier", "event_count", "ridership"]].rename(columns={"event_count": "gfl_events", "ridership": "gfl_riders"})
    out = l.merge(g, on=["date", "identifier"], how="outer").fillna(0)
    display_mismatch = set()
    if not inference.rules.empty and "legacy_display_mismatch" in inference.rules.columns:
        display_mismatch = set(inference.rules.loc[inference.rules["legacy_display_mismatch"].eq(True), "identifier"].astype(str))
    rows = []
    for _, r in out.iterrows():
        ident = str(r["identifier"])
        legacy_events = float(r["legacy_events"])
        gfl_events = float(r["gfl_events"])
        gfl_riders = float(r["gfl_riders"])
        rule = rules.get(ident)
        expected = legacy_events if rule is True else (0.0 if rule is False else np.nan)
        gfl_rate = gfl_riders / gfl_events if gfl_events else 0.0
        record_diff = gfl_events - legacy_events
        rider_diff = gfl_riders - expected if not pd.isna(expected) else np.nan
        if rule is False and abs(gfl_riders) < 1e-9:
            summary = "Legacy records the fare event, while GFL correctly contributes 0 riders for this category"
            cause = "Expected non-rider category behavior"
        elif ident in display_mismatch and (pd.isna(rider_diff) or abs(rider_diff) < 1e-9):
            summary = "Legacy detailed key-ridership display is misleading, but actual ridership behavior reconciles"
            cause = "Legacy internal key-ridership display inconsistency"
        elif not pd.isna(rider_diff) and abs(rider_diff) > 1e-9:
            if rule is False and gfl_riders > 0:
                summary = f"Legacy expects 0 riders, but GFL contributes {gfl_riders:g}"
                cause = "Ridership-rule/configuration difference"
            elif rider_diff < 0:
                summary = f"{-rider_diff:g} rider(s) are missing from GFL for this fare category"
                cause = "Missing GFL rider records or report-scope/filter difference"
            else:
                summary = f"{rider_diff:g} extra GFL rider(s) exist for this fare category"
                cause = "Extra GFL rider records or report-scope/filter difference"
        elif rule is True and abs(record_diff) > 1e-9:
            summary = f"Rider totals reconcile, but raw record counts differ by {record_diff:+g}"
            cause = "Duplicate/zero-ridership row behavior or report-recording difference"
        elif rule is None and abs(record_diff) > 1e-9:
            summary = f"Record count differs by {record_diff:+g} and Legacy rider behavior could not be inferred"
            cause = "Unresolved record-count discrepancy"
        else:
            summary = "Ridership behavior matches"
            cause = "Match"
        evidence = f"Legacy fare-use events={legacy_events:g}; GFL ridership-export records={gfl_events:g}; GFL riders={gfl_riders:g}."
        if rule is not None:
            evidence += f" Inferred Legacy counts-ridership={bool(rule)}."
        rows.append({
            "Date": str(r["date"]), "Type": _type_for_identifier(ident), "Identifier": ident,
            "Legacy Events": legacy_events, "GFL Ridership Records": gfl_events, "Record Difference": record_diff,
            "Legacy Expected Riders": expected, "GFL Riders": gfl_riders, "Rider Difference": rider_diff,
            "Legacy Counts Ridership": rule, "GFL Riders/Record": gfl_rate,
            "Problem Summary": summary, "Likely Cause": cause, "Evidence": evidence,
        })
    result = pd.DataFrame(rows, columns=columns)
    result["_abs"] = result["Record Difference"].abs() + pd.to_numeric(result["Rider Difference"], errors="coerce").abs().fillna(0)
    return result.sort_values(["Date", "_abs", "Type", "Identifier"], ascending=[True, False, True, True]).drop(columns="_abs").reset_index(drop=True)


def _detail_merge(gfl: LargeCSVProfile, legacy_tx: Optional[LargeCSVProfile], rules: dict[str, bool]) -> pd.DataFrame:
    columns = ["Date", "Route", "Run", "Bus", "Identifier", "Legacy Events", "GFL Ridership Records", "GFL Riders", "Record Difference"]
    legacy_detail = pd.DataFrame() if legacy_tx is None else getattr(legacy_tx, "boarding_day_route_run_bus_identifier", pd.DataFrame())
    if legacy_tx is None or legacy_detail.empty or gfl.day_route_run_bus_identifier.empty:
        return pd.DataFrame(columns=columns)

    effective_rules = dict(rules)
    effective_rules["PRESET"] = True
    l0 = legacy_detail.copy()
    l0["counts_ridership"] = l0["identifier"].map(effective_rules)
    l0 = l0[l0["counts_ridership"].eq(True)]

    g0 = gfl.day_route_run_bus_identifier.copy()
    g0["counts_ridership"] = g0["identifier"].map(effective_rules)
    g0 = g0[g0["counts_ridership"].eq(True)]

    l = l0.rename(columns={"date": "Date", "route": "Route", "run": "Run", "bus": "Bus", "identifier": "Identifier", "event_count": "Legacy Events"})
    g = g0.rename(columns={"date": "Date", "route": "Route", "run": "Run", "bus": "Bus", "identifier": "Identifier", "event_count": "GFL Ridership Records", "ridership": "GFL Riders"})
    out = l[["Date", "Route", "Run", "Bus", "Identifier", "Legacy Events"]].merge(
        g[["Date", "Route", "Run", "Bus", "Identifier", "GFL Ridership Records", "GFL Riders"]],
        on=["Date", "Route", "Run", "Bus", "Identifier"], how="outer"
    ).fillna(0)
    out["Record Difference"] = out["GFL Ridership Records"] - out["Legacy Events"]
    return out[columns]

def _aggregate_dimension(detail: pd.DataFrame, dims: list[str]) -> pd.DataFrame:
    if detail.empty:
        return pd.DataFrame()
    group = ["Date", "Identifier"] + dims
    out = detail.groupby(group, as_index=False).agg({"Legacy Events": "sum", "GFL Ridership Records": "sum", "GFL Riders": "sum"})
    out["Record Difference"] = out["GFL Ridership Records"] - out["Legacy Events"]
    return out


def _pair_offsets(df: pd.DataFrame, context_cols: list[str], location_col: str, level: str) -> list[dict]:
    """Find balancing aggregate offsets inside a shared context.

    These are clues only. They are deliberately not called confirmed reassignments because
    aggregate counts do not prove the same rider moved. Exact timestamp/bus/category
    transaction matching is the confirmation layer.
    """
    results = []
    if df.empty:
        return results
    for context, group in df.groupby(context_cols, dropna=False):
        if not isinstance(context, tuple):
            context = (context,)
        total_diff = float(group["Record Difference"].sum())
        if abs(total_diff) > 1e-9:
            continue
        pos = [[str(r[location_col]), float(r["Record Difference"])] for _, r in group[group["Record Difference"] > 0].iterrows()]
        neg = [[str(r[location_col]), float(-r["Record Difference"])] for _, r in group[group["Record Difference"] < 0].iterrows()]
        pi = ni = 0
        while pi < len(pos) and ni < len(neg):
            to_loc, pv = pos[pi]
            from_loc, nv = neg[ni]
            amount = min(pv, nv)
            if amount > 0:
                ctx = dict(zip(context_cols, context))
                identifier = str(ctx.get("Identifier", ""))
                date = str(ctx.get("Date", ""))
                context_bits = [f"{c} {ctx.get(c)}" for c in context_cols if c not in {"Date", "Identifier"} and str(ctx.get(c, "")) not in {"", "0", "nan"}]
                shared_context = " / ".join(context_bits) if context_bits else "same day/category"
                if level == "Route":
                    cause = "Aggregate route offset — possible reassignment, not yet confirmed"
                    conf = 78
                elif level == "Run":
                    cause = "Aggregate run offset — possible reassignment, not yet confirmed"
                    conf = 74
                else:
                    cause = "Aggregate bus offset — possible assignment difference, not yet confirmed"
                    conf = 70
                evidence = (
                    f"{date} {identifier}: {amount:g} event(s) balance from {level} {from_loc} in Legacy to {level} {to_loc} in GFL "
                    f"within {shared_context}. This is an aggregate clue only; confirm with exact timestamp/bus/category transaction matching below."
                )
                results.append({
                    "Date": date, "Identifier": identifier, "Level": level, "Shared Context": shared_context,
                    "Legacy Location": from_loc, "GFL Location": to_loc, "Count": amount,
                    "Likely Cause": cause, "Confidence %": conf, "Evidence": evidence,
                })
            pos[pi][1] -= amount
            neg[ni][1] -= amount
            if pos[pi][1] <= 1e-9:
                pi += 1
            if neg[ni][1] <= 1e-9:
                ni += 1
    return results


def build_specific_comparisons(gfl: LargeCSVProfile, legacy_tx: Optional[LargeCSVProfile], rules: dict[str, bool]):
    detail = _detail_merge(gfl, legacy_tx, rules)
    if detail.empty:
        return {
            "route": pd.DataFrame(), "run": pd.DataFrame(), "bus": pd.DataFrame(),
            "route_run_bus": detail, "movements": pd.DataFrame(),
        }
    route = _aggregate_dimension(detail, ["Route"])
    run = _aggregate_dimension(detail, ["Route", "Run"])
    bus = _aggregate_dimension(detail, ["Route", "Bus"])

    # Movement clues are paired only inside a meaningful shared context so a deficit on
    # one bus is never paired with a surplus on a different bus and presented as one move.
    route_by_bus = _aggregate_dimension(detail, ["Bus", "Route"])
    run_by_route_bus = _aggregate_dimension(detail, ["Route", "Bus", "Run"])
    bus_by_route_run = _aggregate_dimension(detail, ["Route", "Run", "Bus"])
    movement_rows = []
    movement_rows += _pair_offsets(route_by_bus, ["Date", "Identifier", "Bus"], "Route", "Route")
    movement_rows += _pair_offsets(run_by_route_bus, ["Date", "Identifier", "Route", "Bus"], "Run", "Run")
    movement_rows += _pair_offsets(bus_by_route_run, ["Date", "Identifier", "Route", "Run"], "Bus", "Bus")
    movements = pd.DataFrame(movement_rows)
    if not movements.empty:
        movements = movements.sort_values(["Date", "Count", "Identifier"], ascending=[True, False, True]).reset_index(drop=True)
    return {"route": route, "run": run, "bus": bus, "route_run_bus": detail, "movements": movements}


def build_drilldowns(gfl: LargeCSVProfile, routesum: LegacyRouteSum, legacy_tx: Optional[LargeCSVProfile], inference: RuleInference, rules: dict[str, bool]) -> dict:
    day = build_day_comparison(gfl, routesum, legacy_tx, rules)
    day_ident = build_day_identifier_comparison(gfl, legacy_tx, inference, rules)
    specific = build_specific_comparisons(gfl, legacy_tx, rules)

    targets = set()
    if not day_ident.empty:
        problem = day_ident[(pd.to_numeric(day_ident["Rider Difference"], errors="coerce").abs() > 1e-9) | day_ident["Likely Cause"].isin(["Unresolved record-count discrepancy", "Legacy internal key-ridership display inconsistency"])]
        for _, r in problem.iterrows():
            if rules.get(str(r["Identifier"])) is True:
                targets.add((str(r["Date"]), str(r["Identifier"])))
    route = specific.get("route", pd.DataFrame())
    if route is not None and not route.empty:
        for _, r in route[route["Record Difference"].abs() > 1e-9].iterrows():
            if rules.get(str(r["Identifier"])) is True:
                targets.add((str(r["Date"]), str(r["Identifier"])))

    transaction_matches = pd.DataFrame()
    transaction_error = ""
    if legacy_tx is not None and targets:
        try:
            transaction_matches = match_remaining_transactions(gfl.source_path, legacy_tx.source_path, targets)
        except Exception as exc:
            transaction_error = str(exc)

    notes = []
    if legacy_tx is None:
        notes.append("Legacy Transaction Detail was not uploaded, so day/category, route/run/bus, and transaction-match drill-downs are limited.")
    elif day.empty:
        notes.append("Daily ridership could not be derived from the supplied formats.")
    if transaction_error:
        notes.append(f"Targeted transaction matching could not complete: {transaction_error}")
    if not day.empty and "Legacy Source" in day.columns and day["Legacy Source"].str.contains("Derived", na=False).any():
        derived_total = float(pd.to_numeric(day["Legacy Ridership"], errors="coerce").fillna(0).sum())
        authoritative = float(routesum.totals.get("ridership", 0))
        if abs(derived_total - authoritative) > 1e-9:
            notes.append(f"Daily Legacy ridership reconstruction did not validate: derived fare-use riders sum to {derived_total:g}, while authoritative ROUTESUM total is {authoritative:g}. Daily values are shown as derived and should not be treated as authoritative raw Legacy totals.")
        else:
            notes.append("Daily Legacy ridership is reconstructed from fare-use transactions and validates exactly to the authoritative ROUTESUM total.")

    return {
        "day_comparison": day,
        "day_identifier": day_ident,
        "route_comparison_by_day": specific.get("route", pd.DataFrame()),
        "run_comparison_by_day": specific.get("run", pd.DataFrame()),
        "bus_comparison_by_day": specific.get("bus", pd.DataFrame()),
        "route_run_bus_by_day": specific.get("route_run_bus", pd.DataFrame()),
        "equal_opposite_movements": specific.get("movements", pd.DataFrame()),
        "transaction_matches": transaction_matches,
        "transaction_targets": sorted(targets),
        "notes": notes,
    }
