from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import pandas as pd

from common import (
    canonical_identifier,
    clean_dimension,
    find_header_column,
    find_legacy_tx_header_row,
    read_csv_header,
)


def _norm_date(series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.strftime("%Y-%m-%d").fillna("")


def _norm_timestamp(series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.strftime("%Y-%m-%d %H:%M:%S").fillna(series.astype(str).str.strip())


def _add_signature(store, row, amount: float):
    key = (
        str(row.get("date", "")), str(row.get("timestamp", "")),
        clean_dimension(row.get("bus", "")), clean_dimension(row.get("driver", "")),
        str(row.get("identifier", "")), clean_dimension(row.get("route", "")),
        clean_dimension(row.get("run", "")), clean_dimension(row.get("trip", "")),
    )
    store[key] += float(amount)


def _scan_gfl(path: str, targets: set[tuple[str, str]], chunksize: int = 100_000):
    headers = read_csv_header(path)
    cols = {
        "time": find_header_column(headers, ["Transaction Time", "Date Time", "Timestamp"]),
        "product_type": find_header_column(headers, ["Product Type"]),
        "product": find_header_column(headers, ["Product"]),
        "bus": find_header_column(headers, ["Bus Number", "Bus"]),
        "driver": find_header_column(headers, ["Driver"]),
        "route": find_header_column(headers, ["Route", "Route Number", "Route ID"]),
        "run": find_header_column(headers, ["Run"]),
        "trip": find_header_column(headers, ["Trip"]),
        "key_ttp": find_header_column(headers, ["Key/TTP", "Key TTP", "Fare Identifier"]),
        "ridership": find_header_column(headers, ["Ridership", "Boardings", "Riders"]),
    }
    if not cols["time"] or not cols["ridership"]:
        return defaultdict(float)
    usecols = [c for c in cols.values() if c]
    target_dates = {d for d, _ in targets}
    store = defaultdict(float)
    for chunk in pd.read_csv(path, usecols=usecols, dtype=str, chunksize=chunksize, low_memory=False):
        dates = _norm_date(chunk[cols["time"]])
        mask = dates.isin(target_dates)
        if not mask.any():
            continue
        sub = chunk.loc[mask].copy()
        sub["date"] = dates.loc[mask].values
        sub["timestamp"] = _norm_timestamp(sub[cols["time"]])
        sub["identifier"] = [
            canonical_identifier(
                key_ttp_raw=r.get(cols["key_ttp"]) if cols["key_ttp"] else None,
                product=r.get(cols["product"]) if cols["product"] else None,
                product_type=r.get(cols["product_type"]) if cols["product_type"] else None,
            ) for _, r in sub.iterrows()
        ]
        sub = sub[[ (d, i) in targets for d, i in zip(sub["date"], sub["identifier"]) ]]
        if sub.empty:
            continue
        for _, r in sub.iterrows():
            rid = pd.to_numeric(r.get(cols["ridership"]) if cols["ridership"] else 0, errors="coerce")
            rid = 0.0 if pd.isna(rid) else float(rid)
            rec = {
                "date": r["date"], "timestamp": r["timestamp"], "identifier": r["identifier"],
                "bus": r.get(cols["bus"], "") if cols["bus"] else "",
                "driver": r.get(cols["driver"], "") if cols["driver"] else "",
                "route": r.get(cols["route"], "") if cols["route"] else "",
                "run": r.get(cols["run"], "") if cols["run"] else "",
                "trip": r.get(cols["trip"], "") if cols["trip"] else "",
            }
            _add_signature(store, rec, rid)
    return store


def _scan_legacy(path: str, targets: set[tuple[str, str]], chunksize: int = 100_000):
    header_row = find_legacy_tx_header_row(path)
    headers = read_csv_header(path, skip_rows=header_row)
    cols = {
        "time": find_header_column(headers, ["Date Time", "DateTime", "Transaction Time"]),
        "product": find_header_column(headers, ["Product"]),
        "bus": find_header_column(headers, ["Bus"]),
        "driver": find_header_column(headers, ["Driver"]),
        "route": find_header_column(headers, ["Route"]),
        "run": find_header_column(headers, ["Run"]),
        "trip": find_header_column(headers, ["Trip"]),
        "key": find_header_column(headers, ["Keys", "Key"]),
        "ttp": find_header_column(headers, ["TTP"]),
    }
    if not cols["time"]:
        return defaultdict(float)
    usecols = [c for c in cols.values() if c]
    target_dates = {d for d, _ in targets}
    store = defaultdict(float)
    for chunk in pd.read_csv(path, skiprows=header_row, usecols=usecols, dtype=str, chunksize=chunksize, low_memory=False):
        dates = _norm_date(chunk[cols["time"]])
        mask = dates.isin(target_dates)
        if not mask.any():
            continue
        sub = chunk.loc[mask].copy()
        sub["date"] = dates.loc[mask].values
        sub["timestamp"] = _norm_timestamp(sub[cols["time"]])
        sub["identifier"] = [
            canonical_identifier(
                key_raw=r.get(cols["key"]) if cols["key"] else None,
                ttp_raw=r.get(cols["ttp"]) if cols["ttp"] else None,
                product=r.get(cols["product"]) if cols["product"] else None,
            ) for _, r in sub.iterrows()
        ]
        sub = sub[[ (d, i) in targets for d, i in zip(sub["date"], sub["identifier"]) ]]
        if sub.empty:
            continue
        for _, r in sub.iterrows():
            rec = {
                "date": r["date"], "timestamp": r["timestamp"], "identifier": r["identifier"],
                "bus": r.get(cols["bus"], "") if cols["bus"] else "",
                "driver": r.get(cols["driver"], "") if cols["driver"] else "",
                "route": r.get(cols["route"], "") if cols["route"] else "",
                "run": r.get(cols["run"], "") if cols["run"] else "",
                "trip": r.get(cols["trip"], "") if cols["trip"] else "",
            }
            _add_signature(store, rec, 1.0)
    return store


def _assignment_text(assignments: dict[tuple[str, str, str, str], float]) -> str:
    parts = []
    for (route, run, trip, driver), count in sorted(assignments.items(), key=lambda kv: (-kv[1], kv[0])):
        label = f"R{route or '?'} / Run {run or '?'} / Trip {trip or '?'}"
        if driver:
            label += f" / Driver {driver}"
        parts.append(f"{label} × {count:g}")
    return "; ".join(parts[:8]) + ("; ..." if len(parts) > 8 else "")


def match_remaining_transactions(gfl_path: str, legacy_path: str, targets: Iterable[tuple[str, str]]) -> pd.DataFrame:
    """Targeted second-pass transaction matching for only discrepant day/category pairs."""
    targets = {(str(d), str(i)) for d, i in targets if d and i}
    columns = [
        "Date", "Timestamp", "Bus", "Identifier", "Legacy Count", "GFL Riders",
        "Exact Assignment Matches", "Assignment Difference", "Count Difference",
        "Legacy Assignments", "GFL Assignments", "Likely Cause", "Confidence %", "Evidence",
    ]
    if not targets:
        return pd.DataFrame(columns=columns)

    legacy = _scan_legacy(legacy_path, targets)
    gfl = _scan_gfl(gfl_path, targets)

    def base_map(store):
        out = defaultdict(lambda: defaultdict(float))
        for (date, ts, bus, driver, ident, route, run, trip), count in store.items():
            base = (date, ts, bus, ident)
            out[base][(route, run, trip, driver)] += count
        return out

    lb, gb = base_map(legacy), base_map(gfl)
    rows = []
    for base in sorted(set(lb) | set(gb)):
        la, ga = lb.get(base, {}), gb.get(base, {})
        lt, gt = sum(la.values()), sum(ga.values())
        exact = 0.0
        for assignment in set(la) | set(ga):
            exact += min(la.get(assignment, 0.0), ga.get(assignment, 0.0))
        count_diff = gt - lt
        assignment_diff = max(0.0, min(lt, gt) - exact)
        if abs(count_diff) < 1e-9 and assignment_diff < 1e-9:
            continue
        if abs(count_diff) < 1e-9 and assignment_diff > 0:
            cause = "Same transaction signature exists in both systems but route/run/trip assignment differs"
            confidence = 99 if base[1] and base[2] else 95
        elif lt == 0:
            cause = "GFL rider has no matching Legacy transaction signature"
            confidence = 92
        elif gt == 0:
            cause = "Legacy rider has no matching GFL transaction signature"
            confidence = 92
        else:
            cause = "Partial transaction-count mismatch after timestamp/bus/category matching"
            confidence = 88
        evidence = (
            f"Matched on date={base[0]}, timestamp={base[1]}, bus={base[2] or '?'}, category={base[3]}. "
            f"Legacy={lt:g}, GFL riders={gt:g}, exact same route/run/trip assignment={exact:g}."
        )
        rows.append({
            "Date": base[0], "Timestamp": base[1], "Bus": base[2], "Identifier": base[3],
            "Legacy Count": lt, "GFL Riders": gt, "Exact Assignment Matches": exact,
            "Assignment Difference": assignment_diff, "Count Difference": count_diff,
            "Legacy Assignments": _assignment_text(la), "GFL Assignments": _assignment_text(ga),
            "Likely Cause": cause, "Confidence %": confidence, "Evidence": evidence,
        })
    if not rows:
        return pd.DataFrame(columns=columns)
    out = pd.DataFrame(rows)
    out["_priority"] = out["Count Difference"].abs() + out["Assignment Difference"]
    return out.sort_values(["_priority", "Date", "Identifier"], ascending=[False, True, True]).drop(columns="_priority").reset_index(drop=True)
