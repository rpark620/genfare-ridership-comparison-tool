from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Iterable

import duckdb
import pandas as pd

from common import (
    clean_dimension,
    find_header_column,
    find_legacy_tx_header_row,
    quote_ident,
    quote_sql_string,
    read_csv_header,
)
from legacy_routesum import LegacyRouteSum


@dataclass
class RevenueProfile:
    kind: str
    source_path: str
    totals: Dict[str, float] = field(default_factory=dict)
    day_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    route_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_route_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_product_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_run_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_bus_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_route_run_bus_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    confidence: int = 100


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=2")
    con.execute("PRAGMA preserve_insertion_order=false")
    try:
        con.execute("PRAGMA memory_limit='900MB'")
    except Exception:
        pass
    return con


def _read_csv_expr(path: str, skip: int = 0) -> str:
    return (
        f"read_csv_auto({quote_sql_string(path)}, header=true, skip={int(skip)}, "
        "all_varchar=true, ignore_errors=true, null_padding=true, sample_size=20000)"
    )


def _col_expr(col: str | None, alias: str, numeric: bool = False) -> str:
    if not col:
        return f"0.0 AS {quote_ident(alias)}" if numeric else f"'' AS {quote_ident(alias)}"
    q = quote_ident(col)
    if numeric:
        return f"COALESCE(TRY_CAST(REPLACE(REPLACE({q}, ',', ''), '$', '') AS DOUBLE), 0.0) AS {quote_ident(alias)}"
    return f"COALESCE(CAST({q} AS VARCHAR), '') AS {quote_ident(alias)}"


def _date_expr(col: str = "transaction_time") -> str:
    q = quote_ident(col)
    return (
        "COALESCE("
        f"strftime(TRY_STRPTIME({q}, '%Y-%m-%d %H:%M:%S'), '%Y-%m-%d'),"
        f"strftime(TRY_STRPTIME({q}, '%m/%d/%Y %H:%M:%S'), '%Y-%m-%d'),"
        f"strftime(TRY_STRPTIME({q}, '%m/%d/%Y %H:%M'), '%Y-%m-%d'),"
        "''"
        ")"
    )


def _timestamp_expr(col: str = "transaction_time") -> str:
    q = quote_ident(col)
    return (
        "COALESCE("
        f"strftime(TRY_STRPTIME({q}, '%Y-%m-%d %H:%M:%S'), '%Y-%m-%d %H:%M:%S'),"
        f"strftime(TRY_STRPTIME({q}, '%m/%d/%Y %H:%M:%S'), '%Y-%m-%d %H:%M:%S'),"
        f"strftime(TRY_STRPTIME({q}, '%m/%d/%Y %H:%M'), '%Y-%m-%d %H:%M:%S'),"
        f"CAST({q} AS VARCHAR)"
        ")"
    )


def _norm_date_series(s: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(s, errors="coerce")
    out = parsed.dt.strftime("%Y-%m-%d")
    return out.fillna(s.astype(str).str.strip())


def canonical_revenue_category(product_type=None, product=None) -> str:
    ptype = "" if product_type is None or pd.isna(product_type) else str(product_type).strip().upper()
    p = "" if product is None or pd.isna(product) else str(product).strip().upper()
    if "PRESET" in ptype or p == "PRESET":
        return "PRESET"
    if ":" in p:
        p = p.split(":", 1)[1].strip()
    p = " ".join(p.split())
    aliases = {
        "DAYPASS": "DAY PASS",
        "FREEKID": "FREE KID",
        "HALF FARE": "HALFFARE",
        "SHORTFARE": "SHRTFARE",
    }
    return aliases.get(p, p)


def _clean_dims(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in columns:
        if c in out.columns:
            out[c] = out[c].map(clean_dimension)
    if "date" in out.columns:
        out["date"] = _norm_date_series(out["date"])
    return out


def _group_products(df: pd.DataFrame, group_cols: list[str], value_col: str, count_col: str = "event_count") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=group_cols + ["category", count_col, value_col])
    work = df.copy()
    work["category"] = [canonical_revenue_category(r.get("product_type"), r.get("product")) for _, r in work.iterrows()]
    work = work[work["category"].ne("")]
    agg = {count_col: (count_col, "sum"), value_col: (value_col, "sum")}
    if "positive_amount" in work.columns:
        agg["positive_amount"] = ("positive_amount", "sum")
    if "negative_amount" in work.columns:
        agg["negative_amount"] = ("negative_amount", "sum")
    return work.groupby(group_cols + ["category"], as_index=False, dropna=False).agg(**agg)


def profile_gfl_revenue(path: str) -> RevenueProfile:
    headers = read_csv_header(path)
    revenue_col = find_header_column(headers, ["Revenue", "Amount Charged", "Amount"])
    time_col = find_header_column(headers, ["Transaction Time", "Date Time", "Timestamp"])
    route_col = find_header_column(headers, ["Route", "Route Number", "Route ID"])
    if not revenue_col or not time_col:
        raise ValueError("This does not look like a GenfareLink Revenue raw-data export: Revenue and/or Transaction Time was not found.")

    mapping = {
        "transaction_time": time_col,
        "product_type": find_header_column(headers, ["Product Type"]),
        "product": find_header_column(headers, ["Product"]),
        "bus": find_header_column(headers, ["Bus Number", "Bus"]),
        "route": route_col,
        "run": find_header_column(headers, ["Run"]),
        "trip": find_header_column(headers, ["Trip"]),
        "revenue": revenue_col,
    }
    select = ",\n".join([
        _col_expr(mapping["transaction_time"], "transaction_time"),
        _col_expr(mapping["product_type"], "product_type"),
        _col_expr(mapping["product"], "product"),
        _col_expr(mapping["route"], "route"),
        _col_expr(mapping["run"], "run"),
        _col_expr(mapping["trip"], "trip"),
        _col_expr(mapping["bus"], "bus"),
        _col_expr(mapping["revenue"], "revenue", numeric=True),
    ])

    con = _connect()
    try:
        con.execute(f"CREATE TEMP VIEW src0 AS SELECT {select} FROM {_read_csv_expr(path)}")
        con.execute(f"""
            CREATE TEMP VIEW src AS
            SELECT *, {_date_expr()} AS date, {_timestamp_expr()} AS timestamp
            FROM src0
        """)
        totals = con.execute('SELECT COUNT(*) AS "rows", SUM(revenue) AS revenue FROM src').fetchdf().iloc[0].to_dict()
        day = con.execute("""
            SELECT date, COUNT(*) event_count, SUM(revenue) revenue
            FROM src WHERE date <> '' GROUP BY date ORDER BY date
        """).fetchdf()
        route = con.execute("""
            SELECT route, COUNT(*) event_count, SUM(revenue) revenue
            FROM src GROUP BY route ORDER BY route
        """).fetchdf()
        day_route = con.execute("""
            SELECT date, route, COUNT(*) event_count, SUM(revenue) revenue
            FROM src WHERE date <> '' GROUP BY date, route ORDER BY date, route
        """).fetchdf()
        prod_raw = con.execute("""
            SELECT date, product_type, product, COUNT(*) event_count, SUM(revenue) revenue
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_run = con.execute("""
            SELECT date, route, run, COUNT(*) event_count, SUM(revenue) revenue
            FROM src WHERE date <> '' GROUP BY date, route, run
        """).fetchdf()
        day_bus = con.execute("""
            SELECT date, route, bus, COUNT(*) event_count, SUM(revenue) revenue
            FROM src WHERE date <> '' GROUP BY date, route, bus
        """).fetchdf()
        day_detail = con.execute("""
            SELECT date, route, run, bus, COUNT(*) event_count, SUM(revenue) revenue
            FROM src WHERE date <> '' GROUP BY date, route, run, bus
        """).fetchdf()
    finally:
        con.close()

    for frame in [day, route, day_route, day_run, day_bus, day_detail, prod_raw]:
        if frame is not None and not frame.empty:
            for c in ["route", "run", "bus"]:
                if c in frame.columns:
                    frame[c] = frame[c].map(clean_dimension)
            if "date" in frame.columns:
                frame["date"] = _norm_date_series(frame["date"])
    products = _group_products(prod_raw, ["date"], "revenue")

    warnings = []
    if not route_col:
        warnings.append("Route was not found. Route-level revenue reconciliation will be limited.")
    if not mapping["product"]:
        warnings.append("Product was not found. Product-level investigation will be limited.")

    return RevenueProfile(
        kind="GFL_REVENUE_RAW",
        source_path=path,
        totals={k: float(v or 0) for k, v in totals.items()},
        day_summary=day,
        route_summary=route,
        day_route_summary=day_route,
        day_product_summary=products,
        day_run_summary=day_run,
        day_bus_summary=day_bus,
        day_route_run_bus_summary=day_detail,
        metadata={
            "revenue_column": revenue_col,
            "transaction_time_column": time_col,
            "engine": "DuckDB streaming/aggregate scan",
        },
        warnings=warnings,
        confidence=100,
    )


def profile_legacy_revenue_transactions(path: str) -> RevenueProfile:
    header_row = find_legacy_tx_header_row(path)
    headers = read_csv_header(path, skip_rows=header_row)
    time_col = find_header_column(headers, ["Date Time", "DateTime", "Transaction Time"])
    tx_col = find_header_column(headers, ["Transaction Type", "Type"])
    amount_col = find_header_column(headers, ["Amt Chrg", "Amount Charged", "Revenue"])
    if not time_col or not amount_col:
        raise ValueError("Legacy Transaction Detail revenue fields were not recognized (Date Time / Amt Chrg).")

    mapping = {
        "transaction_time": time_col,
        "transaction_type": tx_col,
        "product": find_header_column(headers, ["Product"]),
        "bus": find_header_column(headers, ["Bus"]),
        "route": find_header_column(headers, ["Route"]),
        "run": find_header_column(headers, ["Run"]),
        "trip": find_header_column(headers, ["Trip"]),
        "amount": amount_col,
    }
    select = ",\n".join([
        _col_expr(mapping["transaction_time"], "transaction_time"),
        _col_expr(mapping["transaction_type"], "transaction_type"),
        _col_expr(mapping["product"], "product"),
        _col_expr(mapping["route"], "route"),
        _col_expr(mapping["run"], "run"),
        _col_expr(mapping["trip"], "trip"),
        _col_expr(mapping["bus"], "bus"),
        _col_expr(mapping["amount"], "amount", numeric=True),
    ])

    con = _connect()
    try:
        con.execute(f"CREATE TEMP VIEW raw0 AS SELECT {select} FROM {_read_csv_expr(path, skip=header_row)}")
        con.execute(f"""
            CREATE TEMP VIEW src AS
            SELECT *, {_date_expr()} AS date, {_timestamp_expr()} AS timestamp
            FROM raw0
            WHERE regexp_matches(transaction_time, '^[0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}}|^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}')
        """)
        totals = con.execute("""
            SELECT COUNT(*) AS "rows", SUM(amount) AS amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src
        """).fetchdf().iloc[0].to_dict()
        day = con.execute("""
            SELECT date, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src WHERE date <> '' GROUP BY date ORDER BY date
        """).fetchdf()
        route = con.execute("""
            SELECT route, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src GROUP BY route
        """).fetchdf()
        day_route = con.execute("""
            SELECT date, route, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src WHERE date <> '' GROUP BY date, route
        """).fetchdf()
        prod_raw = con.execute("""
            SELECT date, transaction_type, product, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_run = con.execute("""
            SELECT date, route, run, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src WHERE date <> '' GROUP BY date, route, run
        """).fetchdf()
        day_bus = con.execute("""
            SELECT date, route, bus, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src WHERE date <> '' GROUP BY date, route, bus
        """).fetchdf()
        day_detail = con.execute("""
            SELECT date, route, run, bus, COUNT(*) event_count, SUM(amount) amount,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) positive_amount,
                   SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) negative_amount
            FROM src WHERE date <> '' GROUP BY date, route, run, bus
        """).fetchdf()
    finally:
        con.close()

    for frame in [day, route, day_route, day_run, day_bus, day_detail, prod_raw]:
        if frame is not None and not frame.empty:
            for c in ["route", "run", "bus"]:
                if c in frame.columns:
                    frame[c] = frame[c].map(clean_dimension)
            if "date" in frame.columns:
                frame["date"] = _norm_date_series(frame["date"])

    # Legacy Transaction Detail amounts are investigative evidence only. They do not
    # necessarily add to ROUTESUM Current + Unclassified Revenue.
    prod_raw = prod_raw.rename(columns={"transaction_type": "product_type"})
    products = _group_products(prod_raw, ["date"], "amount")

    return RevenueProfile(
        kind="LEGACY_TRANSACTION_DETAIL_REVENUE_EVIDENCE",
        source_path=path,
        totals={k: float(v or 0) for k, v in totals.items()},
        day_summary=day,
        route_summary=route,
        day_route_summary=day_route,
        day_product_summary=products,
        day_run_summary=day_run,
        day_bus_summary=day_bus,
        day_route_run_bus_summary=day_detail,
        metadata={"header_row": str(header_row + 1), "amount_column": amount_col, "engine": "DuckDB streaming/aggregate scan"},
        warnings=[
            "Legacy Transaction Detail Amt Chrg is investigative evidence only and is not treated as authoritative Legacy revenue."
        ],
        confidence=85,
    )


def _legacy_authoritative_day(routesum: LegacyRouteSum) -> pd.DataFrame:
    rd = routesum.route_date_summary.copy() if routesum.route_date_summary is not None else pd.DataFrame()
    if rd.empty or "date" not in rd.columns:
        return pd.DataFrame(columns=["date", "current_revenue", "unclassified_revenue", "legacy_revenue"])
    rd["date"] = _norm_date_series(rd["date"])
    rd = rd[rd["date"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$")].copy()
    if rd.empty:
        return pd.DataFrame(columns=["date", "current_revenue", "unclassified_revenue", "legacy_revenue"])
    out = rd.groupby("date", as_index=False).agg(
        current_revenue=("current_revenue", "sum"),
        unclassified_revenue=("unclassified_revenue", "sum"),
    )
    out["legacy_revenue"] = out["current_revenue"] + out["unclassified_revenue"]
    return out


def _legacy_authoritative_day_route(routesum: LegacyRouteSum) -> pd.DataFrame:
    rd = routesum.route_date_summary.copy() if routesum.route_date_summary is not None else pd.DataFrame()
    if rd.empty or "date" not in rd.columns:
        return pd.DataFrame(columns=["date", "route", "current_revenue", "unclassified_revenue", "legacy_revenue"])
    rd["date"] = _norm_date_series(rd["date"])
    rd = rd[rd["date"].astype(str).str.match(r"^\d{4}-\d{2}-\d{2}$")].copy()
    if rd.empty:
        return pd.DataFrame(columns=["date", "route", "current_revenue", "unclassified_revenue", "legacy_revenue"])
    rd["route"] = rd["route"].map(clean_dimension)
    out = rd.groupby(["date", "route"], as_index=False).agg(
        current_revenue=("current_revenue", "sum"),
        unclassified_revenue=("unclassified_revenue", "sum"),
    )
    out["legacy_revenue"] = out["current_revenue"] + out["unclassified_revenue"]
    return out


def _period_route(routesum: LegacyRouteSum) -> pd.DataFrame:
    r = routesum.route_summary.copy() if routesum.route_summary is not None else pd.DataFrame()
    if r.empty:
        return pd.DataFrame(columns=["route", "current_revenue", "unclassified_revenue", "legacy_revenue"])
    r["route"] = r["route"].map(clean_dimension)
    r["legacy_revenue"] = pd.to_numeric(r["current_revenue"], errors="coerce").fillna(0) + pd.to_numeric(r["unclassified_revenue"], errors="coerce").fillna(0)
    return r[["route", "current_revenue", "unclassified_revenue", "legacy_revenue"]]


def _merge_authoritative_routes(legacy: pd.DataFrame, gfl: pd.DataFrame, day_level: bool) -> pd.DataFrame:
    keys = ["date", "route"] if day_level else ["route"]
    l = legacy.copy()
    g = gfl.copy()
    if not g.empty:
        rename = {"revenue": "gfl_revenue", "event_count": "gfl_records"}
        g = g.rename(columns=rename)
    keepg = keys + [c for c in ["gfl_records", "gfl_revenue"] if c in g.columns]
    g = g[keepg] if not g.empty else pd.DataFrame(columns=keys + ["gfl_records", "gfl_revenue"])
    out = l.merge(g, on=keys, how="outer")
    for c in ["current_revenue", "unclassified_revenue", "legacy_revenue", "gfl_revenue", "gfl_records"]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    out["difference"] = out["gfl_revenue"] - out["legacy_revenue"]
    out["status"] = out["difference"].abs().le(0.005).map({True: "Match", False: "Difference"})
    return out.sort_values(keys).reset_index(drop=True)


def _merge_investigative(legacy: pd.DataFrame, gfl: pd.DataFrame, keys: list[str], legacy_value="amount") -> pd.DataFrame:
    l = legacy.copy() if legacy is not None else pd.DataFrame()
    g = gfl.copy() if gfl is not None else pd.DataFrame()
    if l.empty:
        l = pd.DataFrame(columns=keys + [legacy_value, "event_count", "positive_amount", "negative_amount"])
    if g.empty:
        g = pd.DataFrame(columns=keys + ["revenue", "event_count"])
    l = l.rename(columns={legacy_value: "legacy_detail_amount", "event_count": "legacy_detail_records"})
    g = g.rename(columns={"revenue": "gfl_revenue", "event_count": "gfl_records"})
    lkeep = keys + [c for c in ["legacy_detail_records", "legacy_detail_amount", "positive_amount", "negative_amount"] if c in l.columns]
    gkeep = keys + [c for c in ["gfl_records", "gfl_revenue"] if c in g.columns]
    out = l[lkeep].merge(g[gkeep], on=keys, how="outer")
    for c in ["legacy_detail_records", "legacy_detail_amount", "positive_amount", "negative_amount", "gfl_records", "gfl_revenue"]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    out["investigative_difference"] = out["gfl_revenue"] - out["legacy_detail_amount"]
    return out


def _equal_opposite(route_cmp: pd.DataFrame) -> pd.DataFrame:
    cols = ["date", "legacy_location", "gfl_location", "amount", "confidence", "evidence"]
    if route_cmp is None or route_cmp.empty:
        return pd.DataFrame(columns=cols)
    records = []
    for date, grp in route_cmp.groupby("date", dropna=False):
        diff = grp[pd.to_numeric(grp["difference"], errors="coerce").abs() > 0.005].copy()
        neg = diff[diff["difference"] < -0.005].copy()
        pos = diff[diff["difference"] > 0.005].copy()
        used = set()
        for ni, nr in neg.iterrows():
            target = abs(float(nr["difference"]))
            candidates = pos.loc[[i for i in pos.index if i not in used]].copy()
            if candidates.empty:
                continue
            candidates["gap"] = (candidates["difference"] - target).abs()
            pi = candidates["gap"].idxmin()
            pr = pos.loc[pi]
            gap = abs(float(pr["difference"]) - target)
            if gap <= 0.01:
                used.add(pi)
                amount = min(target, abs(float(pr["difference"])))
                records.append({
                    "date": date,
                    "legacy_location": f"Route {nr['route']}",
                    "gfl_location": f"Route {pr['route']}",
                    "amount": amount,
                    "confidence": 94,
                    "evidence": f"Equal-and-opposite route revenue movement of ${amount:,.2f}: Legacy is higher on Route {nr['route']} while GFL is higher on Route {pr['route']} by the same amount.",
                })
    return pd.DataFrame(records, columns=cols)


def _build_findings(summary: dict, day_cmp: pd.DataFrame, period_route_cmp: pd.DataFrame) -> pd.DataFrame:
    rows = []
    rows.append({
        "Severity": "Info" if abs(summary["difference"]) <= 0.005 else "Error",
        "Level": "Overall",
        "Identifier": "Total revenue",
        "Legacy": summary["legacy_total_revenue"],
        "GFL": summary["gfl_revenue"],
        "Difference": summary["difference"],
        "Status": "Match" if abs(summary["difference"]) <= 0.005 else "Unresolved",
        "Likely Cause": "Match" if abs(summary["difference"]) <= 0.005 else "Revenue total mismatch",
        "Evidence": "Legacy uses ROUTESUM Current + Unclassified Revenue; GFL uses the dedicated Revenue raw-data export.",
    })
    if day_cmp is not None and not day_cmp.empty:
        for _, r in day_cmp.iterrows():
            if not bool(r.get("legacy_authoritative", False)):
                continue
            rows.append({
                "Severity": "Info" if abs(float(r["difference"])) <= 0.005 else "Warning",
                "Level": "Day",
                "Identifier": str(r["date"]),
                "Legacy": float(r["legacy_revenue"]),
                "GFL": float(r["gfl_revenue"]),
                "Difference": float(r["difference"]),
                "Status": "Match" if abs(float(r["difference"])) <= 0.005 else "Unresolved",
                "Likely Cause": "Match" if abs(float(r["difference"])) <= 0.005 else "Daily revenue mismatch",
                "Evidence": "Authoritative Legacy daily revenue comes from ROUTESUM BY ROUTE-DATE.",
            })
    if period_route_cmp is not None and not period_route_cmp.empty:
        for _, r in period_route_cmp.iterrows():
            if abs(float(r["difference"])) <= 0.005:
                continue
            rows.append({
                "Severity": "Warning",
                "Level": "Route",
                "Identifier": f"Route {r['route']}",
                "Legacy": float(r["legacy_revenue"]),
                "GFL": float(r["gfl_revenue"]),
                "Difference": float(r["difference"]),
                "Status": "Unresolved",
                "Likely Cause": "Route allocation difference, report scope difference, or missing/extra revenue transactions",
                "Evidence": "Route-level Legacy revenue is authoritative from ROUTESUM; GFL route revenue is from the Revenue raw-data export.",
            })
    return pd.DataFrame(rows)


def reconcile_revenue(gfl: RevenueProfile, routesum: LegacyRouteSum, legacy_tx: RevenueProfile | None = None, transaction_matches: pd.DataFrame | None = None) -> dict:
    legacy_current = float(routesum.totals.get("current_revenue", 0.0) or 0.0)
    legacy_unclassified = float(routesum.totals.get("unclassified_revenue", 0.0) or 0.0)
    legacy_total = legacy_current + legacy_unclassified
    gfl_total = float(gfl.totals.get("revenue", 0.0) or 0.0)

    auth_day = _legacy_authoritative_day(routesum)
    gday = gfl.day_summary.copy()
    if not gday.empty:
        gday = gday.rename(columns={"revenue": "gfl_revenue", "event_count": "gfl_records"})
    else:
        gday = pd.DataFrame(columns=["date", "gfl_records", "gfl_revenue"])

    dates = sorted(set(auth_day.get("date", pd.Series(dtype=str)).astype(str)) | set(gday.get("date", pd.Series(dtype=str)).astype(str)))
    if legacy_tx is not None and legacy_tx.day_summary is not None and not legacy_tx.day_summary.empty:
        dates = sorted(set(dates) | set(legacy_tx.day_summary["date"].astype(str)))
    rows = []
    for date in dates:
        la = auth_day[auth_day["date"].astype(str) == str(date)]
        gg = gday[gday["date"].astype(str) == str(date)]
        lt = legacy_tx.day_summary[legacy_tx.day_summary["date"].astype(str) == str(date)] if legacy_tx is not None and not legacy_tx.day_summary.empty else pd.DataFrame()
        authoritative = not la.empty
        current = float(la["current_revenue"].sum()) if authoritative else float("nan")
        unclass = float(la["unclassified_revenue"].sum()) if authoritative else float("nan")
        ltotal = float(la["legacy_revenue"].sum()) if authoritative else float("nan")
        grev = float(gg["gfl_revenue"].sum()) if not gg.empty else 0.0
        txnet = float(lt["amount"].sum()) if not lt.empty else float("nan")
        diff = grev - ltotal if authoritative else float("nan")
        rows.append({
            "date": date,
            "current_revenue": current,
            "unclassified_revenue": unclass,
            "legacy_revenue": ltotal,
            "gfl_revenue": grev,
            "difference": diff,
            "legacy_detail_net": txnet,
            "legacy_authoritative": authoritative,
            "status": ("Match" if abs(diff) <= 0.005 else "Needs drill-down") if authoritative else "Legacy daily total unavailable",
        })
    day_cmp = pd.DataFrame(rows)

    period_route_cmp = _merge_authoritative_routes(_period_route(routesum), gfl.route_summary, False)
    auth_day_route = _legacy_authoritative_day_route(routesum)
    day_route_cmp = _merge_authoritative_routes(auth_day_route, gfl.day_route_summary, True) if not auth_day_route.empty else pd.DataFrame(columns=["date", "route", "current_revenue", "unclassified_revenue", "legacy_revenue", "gfl_records", "gfl_revenue", "difference", "status"])

    product_cmp = pd.DataFrame()
    run_cmp = pd.DataFrame()
    bus_cmp = pd.DataFrame()
    detail_cmp = pd.DataFrame()
    if legacy_tx is not None:
        product_cmp = _merge_investigative(legacy_tx.day_product_summary, gfl.day_product_summary, ["date", "category"], "amount")
        run_cmp = _merge_investigative(legacy_tx.day_run_summary, gfl.day_run_summary, ["date", "route", "run"], "amount")
        bus_cmp = _merge_investigative(legacy_tx.day_bus_summary, gfl.day_bus_summary, ["date", "route", "bus"], "amount")
        detail_cmp = _merge_investigative(legacy_tx.day_route_run_bus_summary, gfl.day_route_run_bus_summary, ["date", "route", "run", "bus"], "amount")

    movements = _equal_opposite(day_route_cmp)
    notes = [
        "Authoritative Legacy revenue is ROUTESUM Current Revenue + Unclassified Revenue.",
        "Legacy Transaction Detail Amt Chrg is used only as investigative evidence because it does not necessarily add to ROUTESUM revenue.",
    ]
    if auth_day.empty:
        notes.append("This ROUTESUM does not contain authoritative daily revenue. Use a BY ROUTE-DATE ROUTESUM to enable true day-by-day revenue differences.")

    summary = {
        "legacy_current_revenue": legacy_current,
        "legacy_unclassified_revenue": legacy_unclassified,
        "legacy_total_revenue": legacy_total,
        "gfl_revenue": gfl_total,
        "difference": gfl_total - legacy_total,
        "gfl_rows": int(round(gfl.totals.get("rows", 0))),
        "legacy_transaction_rows": int(round(legacy_tx.totals.get("rows", 0))) if legacy_tx is not None else 0,
        "legacy_transaction_detail_net_amount": float(legacy_tx.totals.get("amount", 0.0)) if legacy_tx is not None else None,
        "has_authoritative_daily_legacy": not auth_day.empty,
    }
    findings = _build_findings(summary, day_cmp, period_route_cmp)
    return {
        "summary": summary,
        "findings": findings,
        "drilldowns": {
            "day_comparison": day_cmp,
            "period_route_comparison": period_route_cmp,
            "day_route_comparison": day_route_cmp,
            "day_product_investigation": product_cmp,
            "day_run_investigation": run_cmp,
            "day_bus_investigation": bus_cmp,
            "day_route_run_bus_investigation": detail_cmp,
            "equal_opposite_movements": movements,
            "transaction_matches": transaction_matches if transaction_matches is not None else pd.DataFrame(),
            "notes": notes,
        },
    }


def match_revenue_transactions(gfl_path: str, legacy_path: str, dates: list[str]) -> pd.DataFrame:
    """Targeted, investigative transaction matching for selected problem days.

    Matching uses timestamp + bus + normalized product/category + exact cent amount. Route/run/trip
    is deliberately excluded from the signature so assignment differences can be detected.
    Legacy Transaction Detail remains investigative evidence only.
    """
    columns = [
        "Date", "Timestamp", "Bus", "Category", "Amount", "Legacy Records", "GFL Records",
        "Count Difference", "Assignment Difference", "Legacy Assignments", "GFL Assignments",
        "Likely Cause", "Evidence", "Confidence %",
    ]
    dates = [str(d) for d in dates if str(d)]
    if not dates:
        return pd.DataFrame(columns=columns)
    date_sql = ",".join(quote_sql_string(d) for d in dates)

    # GFL columns
    gh = read_csv_header(gfl_path)
    gt = find_header_column(gh, ["Transaction Time", "Date Time", "Timestamp"])
    gp = find_header_column(gh, ["Product"])
    gpt = find_header_column(gh, ["Product Type"])
    gb = find_header_column(gh, ["Bus Number", "Bus"])
    gr = find_header_column(gh, ["Route"])
    grun = find_header_column(gh, ["Run"])
    gtrip = find_header_column(gh, ["Trip"])
    grev = find_header_column(gh, ["Revenue", "Amount Charged", "Amount"])

    lhrow = find_legacy_tx_header_row(legacy_path)
    lh = read_csv_header(legacy_path, skip_rows=lhrow)
    lt = find_header_column(lh, ["Date Time", "DateTime", "Transaction Time"])
    lp = find_header_column(lh, ["Product"])
    lb = find_header_column(lh, ["Bus"])
    lr = find_header_column(lh, ["Route"])
    lrun = find_header_column(lh, ["Run"])
    ltrip = find_header_column(lh, ["Trip"])
    lamt = find_header_column(lh, ["Amt Chrg", "Amount Charged", "Revenue"])

    if not all([gt, grev, lt, lamt]):
        return pd.DataFrame(columns=columns)

    con = _connect()
    try:
        gselect = ",".join([
            _col_expr(gt, "transaction_time"), _col_expr(gpt, "product_type"), _col_expr(gp, "product"),
            _col_expr(gb, "bus"), _col_expr(gr, "route"), _col_expr(grun, "run"), _col_expr(gtrip, "trip"),
            _col_expr(grev, "amount", numeric=True),
        ])
        con.execute(f"CREATE TEMP VIEW g0 AS SELECT {gselect} FROM {_read_csv_expr(gfl_path)}")
        con.execute(f"CREATE TEMP VIEW g AS SELECT *, {_date_expr()} date, {_timestamp_expr()} timestamp FROM g0")
        gdf = con.execute(f"""
            SELECT date, timestamp, bus, route, run, trip, product_type, product, amount, COUNT(*) records
            FROM g WHERE date IN ({date_sql}) AND ABS(amount) > 0.004
            GROUP BY ALL
        """).fetchdf()

        lselect = ",".join([
            _col_expr(lt, "transaction_time"), _col_expr(lp, "product"), _col_expr(lb, "bus"),
            _col_expr(lr, "route"), _col_expr(lrun, "run"), _col_expr(ltrip, "trip"),
            _col_expr(lamt, "amount", numeric=True),
        ])
        con.execute(f"CREATE TEMP VIEW l0 AS SELECT {lselect} FROM {_read_csv_expr(legacy_path, skip=lhrow)}")
        con.execute(f"CREATE TEMP VIEW l AS SELECT *, {_date_expr()} date, {_timestamp_expr()} timestamp FROM l0")
        ldf = con.execute(f"""
            SELECT date, timestamp, bus, route, run, trip, product, amount, COUNT(*) records
            FROM l WHERE date IN ({date_sql}) AND ABS(amount) > 0.004
            GROUP BY ALL
        """).fetchdf()
    finally:
        con.close()

    def prep(df: pd.DataFrame, is_gfl: bool) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        for c in ["bus", "route", "run", "trip"]:
            out[c] = out[c].map(clean_dimension)
        out["date"] = _norm_date_series(out["date"])
        if is_gfl:
            out["category"] = [canonical_revenue_category(r.get("product_type"), r.get("product")) for _, r in out.iterrows()]
        else:
            out["category"] = [canonical_revenue_category(None, r.get("product")) for _, r in out.iterrows()]
        out["amount"] = pd.to_numeric(out["amount"], errors="coerce").fillna(0).round(2)
        out["assignment"] = out.apply(lambda r: f"R{r['route']}/Run{r['run']}/Trip{r['trip']}", axis=1)
        return out

    gdf = prep(gdf, True)
    ldf = prep(ldf, False)
    keys = ["date", "timestamp", "bus", "category", "amount"]

    def collapse(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=keys + [f"{prefix}_records", f"{prefix}_assignments"])
        work = df.copy()
        # Expand record count into assignment totals without expanding rows.
        assn = work.groupby(keys + ["assignment"], as_index=False)["records"].sum()
        assn["assignment_text"] = assn.apply(lambda r: f"{r['assignment']} ({int(r['records'])})", axis=1)
        text = assn.groupby(keys, as_index=False)["assignment_text"].agg(lambda x: "; ".join(sorted(x)))
        totals = work.groupby(keys, as_index=False)["records"].sum().rename(columns={"records": f"{prefix}_records"})
        return totals.merge(text.rename(columns={"assignment_text": f"{prefix}_assignments"}), on=keys, how="left")

    lc = collapse(ldf, "legacy")
    gc = collapse(gdf, "gfl")
    merged = lc.merge(gc, on=keys, how="outer")
    if merged.empty:
        return pd.DataFrame(columns=columns)
    merged["legacy_records"] = pd.to_numeric(merged.get("legacy_records"), errors="coerce").fillna(0)
    merged["gfl_records"] = pd.to_numeric(merged.get("gfl_records"), errors="coerce").fillna(0)
    merged["legacy_assignments"] = merged.get("legacy_assignments", pd.Series(dtype=str)).fillna("")
    merged["gfl_assignments"] = merged.get("gfl_assignments", pd.Series(dtype=str)).fillna("")
    merged["count_difference"] = merged["gfl_records"] - merged["legacy_records"]
    merged["assignment_difference"] = (
        merged["count_difference"].abs().le(1e-9)
        & merged["legacy_assignments"].ne(merged["gfl_assignments"])
        & merged["legacy_assignments"].ne("")
        & merged["gfl_assignments"].ne("")
    )
    merged = merged[(merged["count_difference"].abs() > 1e-9) | merged["assignment_difference"]].copy()
    if merged.empty:
        return pd.DataFrame(columns=columns)

    def cause(r):
        if r["assignment_difference"]:
            return "Likely route/run/trip assignment difference"
        if r["legacy_records"] == 0:
            return "GFL revenue event has no exact Legacy Transaction Detail signature"
        if r["gfl_records"] == 0:
            return "Legacy Transaction Detail financial event has no exact GFL Revenue signature"
        return "Revenue-event count difference for the same timestamp/bus/category/amount"

    merged["cause"] = merged.apply(cause, axis=1)
    merged["confidence"] = merged["assignment_difference"].map({True: 94, False: 78})
    merged["evidence"] = merged.apply(
        lambda r: (
            f"Exact signature uses {r['timestamp']}, bus {r['bus']}, {r['category']}, ${float(r['amount']):,.2f}. "
            f"Legacy detail records={int(r['legacy_records'])}; GFL records={int(r['gfl_records'])}. "
            + (f"Assignments differ: Legacy {r['legacy_assignments']} vs GFL {r['gfl_assignments']}." if r['assignment_difference'] else "")
        ), axis=1
    )
    out = pd.DataFrame({
        "Date": merged["date"], "Timestamp": merged["timestamp"], "Bus": merged["bus"],
        "Category": merged["category"], "Amount": merged["amount"],
        "Legacy Records": merged["legacy_records"], "GFL Records": merged["gfl_records"],
        "Count Difference": merged["count_difference"], "Assignment Difference": merged["assignment_difference"],
        "Legacy Assignments": merged["legacy_assignments"], "GFL Assignments": merged["gfl_assignments"],
        "Likely Cause": merged["cause"], "Evidence": merged["evidence"], "Confidence %": merged["confidence"],
    })
    return out.sort_values(["Date", "Timestamp", "Bus", "Category"]).reset_index(drop=True)
