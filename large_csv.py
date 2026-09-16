from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import duckdb
import pandas as pd

from common import (
    canonical_identifier,
    clean_dimension,
    find_header_column,
    find_legacy_tx_header_row,
    quote_ident,
    quote_sql_string,
    read_csv_header,
)


@dataclass
class LargeCSVProfile:
    kind: str
    source_path: str
    totals: Dict[str, float] = field(default_factory=dict)
    identifier_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    route_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    route_run_trip: pd.DataFrame = field(default_factory=pd.DataFrame)
    route_run_trip_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    route_revenue: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    confidence: int = 100


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    con.execute("PRAGMA threads=2")
    con.execute("PRAGMA preserve_insertion_order=false")
    # DuckDB can spill hash/group-by work to disk instead of forcing everything into RAM.
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


def _col_expr(col: Optional[str], alias: str, numeric: bool = False) -> str:
    if not col:
        return f"0.0 AS {quote_ident(alias)}" if numeric else f"'' AS {quote_ident(alias)}"
    q = quote_ident(col)
    if numeric:
        return f"COALESCE(TRY_CAST(REPLACE(REPLACE({q}, ',', ''), '$', '') AS DOUBLE), 0.0) AS {quote_ident(alias)}"
    return f"COALESCE(CAST({q} AS VARCHAR), '') AS {quote_ident(alias)}"


def _canonicalize_aggregates(df: pd.DataFrame, key_col="key_ttp_raw", key_raw_col=None, ttp_raw_col=None) -> pd.DataFrame:
    if df.empty:
        df["identifier"] = pd.Series(dtype="object")
        return df
    df = df.copy()
    df["identifier"] = [
        canonical_identifier(
            key_ttp_raw=row.get(key_col) if key_col else None,
            key_raw=row.get(key_raw_col) if key_raw_col else None,
            ttp_raw=row.get(ttp_raw_col) if ttp_raw_col else None,
            product=row.get("product"),
            product_type=row.get("product_type"),
        )
        for _, row in df.iterrows()
    ]
    for c in ["route", "run", "trip", "bus"]:
        if c in df.columns:
            df[c] = df[c].map(clean_dimension)
    return df


def profile_gfl_ridership(path: str) -> LargeCSVProfile:
    headers = read_csv_header(path)
    rid_col = find_header_column(headers, ["Ridership", "Boardings", "Riders"])
    route_col = find_header_column(headers, ["Route", "Route Number", "Route ID"])
    keyttp_col = find_header_column(headers, ["Key/TTP", "Key TTP", "Fare Identifier"])
    amount_col = find_header_column(headers, ["Amount Charged", "Amount", "Charge"])
    if not rid_col or not route_col:
        raise ValueError("This does not look like a GFL Ridership Raw Data export: Ridership and/or Route was not found.")

    mapping = {
        "transaction_time": find_header_column(headers, ["Transaction Time", "Date Time", "Timestamp"]),
        "product_type": find_header_column(headers, ["Product Type"]),
        "product": find_header_column(headers, ["Product"]),
        "organization": find_header_column(headers, ["Organization", "Agency"]),
        "media_type": find_header_column(headers, ["Media Type"]),
        "bus": find_header_column(headers, ["Bus Number", "Bus"]),
        "driver": find_header_column(headers, ["Driver"]),
        "route": route_col,
        "run": find_header_column(headers, ["Run"]),
        "trip": find_header_column(headers, ["Trip"]),
        "fareset": find_header_column(headers, ["Fareset ID", "FS", "Fareset"]),
        "key_ttp_raw": keyttp_col,
        "amount_charged": amount_col,
        "ridership": rid_col,
    }
    select = ",\n".join([
        _col_expr(mapping["product_type"], "product_type"),
        _col_expr(mapping["product"], "product"),
        _col_expr(mapping["route"], "route"),
        _col_expr(mapping["run"], "run"),
        _col_expr(mapping["trip"], "trip"),
        _col_expr(mapping["bus"], "bus"),
        _col_expr(mapping["key_ttp_raw"], "key_ttp_raw"),
        _col_expr(mapping["ridership"], "ridership", numeric=True),
        _col_expr(mapping["amount_charged"], "amount_charged", numeric=True),
    ])

    con = _connect()
    try:
        con.execute(f"CREATE TEMP VIEW src AS SELECT {select} FROM {_read_csv_expr(path)}")
        totals = con.execute("""
            SELECT COUNT(*) AS "rows",
                   SUM(ridership) ridership,
                   SUM(amount_charged) amount_charged
            FROM src
        """).fetchdf().iloc[0].to_dict()

        ident = con.execute("""
            SELECT product_type, product, key_ttp_raw,
                   COUNT(*) event_count,
                   SUM(ridership) ridership,
                   SUM(amount_charged) amount_charged
            FROM src
            GROUP BY ALL
        """).fetchdf()
        ident = _canonicalize_aggregates(ident)
        ident = ident.groupby("identifier", dropna=False, as_index=False).agg(
            event_count=("event_count", "sum"), ridership=("ridership", "sum"), amount_charged=("amount_charged", "sum")
        )

        route_ident = con.execute("""
            SELECT route, product_type, product, key_ttp_raw,
                   COUNT(*) event_count,
                   SUM(ridership) ridership,
                   SUM(amount_charged) amount_charged
            FROM src
            GROUP BY ALL
        """).fetchdf()
        route_ident = _canonicalize_aggregates(route_ident)
        route_ident = route_ident.groupby(["route", "identifier"], dropna=False, as_index=False).agg(
            event_count=("event_count", "sum"), ridership=("ridership", "sum"), amount_charged=("amount_charged", "sum")
        )

        runtrip = con.execute("""
            SELECT route, run, trip,
                   COUNT(*) event_count,
                   SUM(ridership) ridership,
                   SUM(amount_charged) amount_charged
            FROM src
            GROUP BY ALL
        """).fetchdf()
        for c in ["route", "run", "trip"]:
            runtrip[c] = runtrip[c].map(clean_dimension)

        runtrip_ident = con.execute("""
            SELECT route, run, trip, product_type, product, key_ttp_raw,
                   COUNT(*) event_count,
                   SUM(ridership) ridership
            FROM src
            GROUP BY ALL
        """).fetchdf()
        runtrip_ident = _canonicalize_aggregates(runtrip_ident)
        runtrip_ident = runtrip_ident[runtrip_ident["identifier"].ne("")]
        if not runtrip_ident.empty:
            runtrip_ident = runtrip_ident.groupby(["route", "run", "trip", "identifier"], as_index=False).agg(
                event_count=("event_count", "sum"), ridership=("ridership", "sum")
            )

        route_rev = route_ident.groupby("route", as_index=False)["amount_charged"].sum().rename(columns={"amount_charged": "revenue"})
    finally:
        con.close()

    warnings = []
    if not amount_col:
        warnings.append("Amount Charged was not found. Upload the optional GFL Revenue Raw Data file if revenue comparison is needed.")
    if not keyttp_col:
        warnings.append("Key/TTP was not found. Key/TTP rule-difference analysis will be limited.")

    return LargeCSVProfile(
        kind="GFL_RIDERSHIP_RAW",
        source_path=path,
        totals={k: float(v or 0) for k, v in totals.items()},
        identifier_summary=ident,
        route_identifier=route_ident,
        route_run_trip=runtrip,
        route_run_trip_identifier=runtrip_ident,
        route_revenue=route_rev,
        metadata={
            "ridership_column": rid_col,
            "amount_column": amount_col or "not found",
            "key_ttp_column": keyttp_col or "not found",
            "engine": "DuckDB streaming/aggregate scan",
        },
        warnings=warnings,
        confidence=100 if keyttp_col else 90,
    )


def profile_gfl_revenue(path: str) -> LargeCSVProfile:
    headers = read_csv_header(path)
    rev_col = find_header_column(headers, ["Revenue", "Net Revenue", "Amount Charged"])
    route_col = find_header_column(headers, ["Route", "Route Number", "Route ID"])
    if not rev_col or not route_col:
        raise ValueError("This does not look like a GFL Revenue Raw Data export.")
    run_col = find_header_column(headers, ["Run"])
    trip_col = find_header_column(headers, ["Trip"])
    select = ",\n".join([
        _col_expr(route_col, "route"),
        _col_expr(run_col, "run"),
        _col_expr(trip_col, "trip"),
        _col_expr(rev_col, "revenue", numeric=True),
    ])
    con = _connect()
    try:
        con.execute(f"CREATE TEMP VIEW src AS SELECT {select} FROM {_read_csv_expr(path)}")
        total = con.execute('SELECT COUNT(*) AS "rows", SUM(revenue) AS revenue FROM src').fetchdf().iloc[0].to_dict()
        route = con.execute("SELECT route, SUM(revenue) revenue FROM src GROUP BY route").fetchdf()
        rt = con.execute("SELECT route, run, trip, SUM(revenue) revenue FROM src GROUP BY ALL").fetchdf()
        for df in [route, rt]:
            for c in [x for x in ["route", "run", "trip"] if x in df.columns]:
                df[c] = df[c].map(clean_dimension)
    finally:
        con.close()
    return LargeCSVProfile(
        kind="GFL_REVENUE_RAW",
        source_path=path,
        totals={k: float(v or 0) for k, v in total.items()},
        route_revenue=route,
        route_run_trip=rt,
        metadata={"revenue_column": rev_col, "engine": "DuckDB streaming/aggregate scan"},
    )


def profile_legacy_transaction_detail(path: str) -> LargeCSVProfile:
    header_row = find_legacy_tx_header_row(path)
    headers = read_csv_header(path, skip_rows=header_row)
    tx_col = find_header_column(headers, ["Transaction Type", "Type"])
    dt_col = find_header_column(headers, ["Date Time", "DateTime", "Transaction Time"])
    route_col = find_header_column(headers, ["Route"])
    run_col = find_header_column(headers, ["Run"])
    trip_col = find_header_column(headers, ["Trip"])
    key_col = find_header_column(headers, ["Keys", "Key"])
    ttp_col = find_header_column(headers, ["TTP"])
    product_col = find_header_column(headers, ["Product"])
    amount_col = find_header_column(headers, ["Amt Chrg", "Amount Charged", "Amount"])
    bus_col = find_header_column(headers, ["Bus"])
    if not tx_col or not dt_col:
        raise ValueError("Legacy Transaction Detail columns were not recognized.")

    select = ",\n".join([
        _col_expr(tx_col, "transaction_type"),
        _col_expr(dt_col, "transaction_time"),
        _col_expr(product_col, "product"),
        _col_expr(route_col, "route"),
        _col_expr(run_col, "run"),
        _col_expr(trip_col, "trip"),
        _col_expr(bus_col, "bus"),
        _col_expr(key_col, "key_raw"),
        _col_expr(ttp_col, "ttp_raw"),
        _col_expr(amount_col, "amount_charged", numeric=True),
    ])

    con = _connect()
    try:
        con.execute(f"CREATE TEMP VIEW raw AS SELECT {select} FROM {_read_csv_expr(path, skip=header_row)}")
        # Date-time filter removes Search Criteria/footer rows from the report export.
        con.execute("""
            CREATE TEMP VIEW src AS
            SELECT * FROM raw
            WHERE regexp_matches(transaction_time, '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}|^[0-9]{4}-[0-9]{2}-[0-9]{2}')
        """)
        total = con.execute('SELECT COUNT(*) AS "rows", SUM(amount_charged) AS amount_charged FROM src').fetchdf().iloc[0].to_dict()
        agg = con.execute("""
            SELECT transaction_type, product, route, run, trip, bus, key_raw, ttp_raw,
                   COUNT(*) event_count, SUM(amount_charged) amount_charged
            FROM src
            GROUP BY ALL
        """).fetchdf()
    finally:
        con.close()

    if agg.empty:
        ident = pd.DataFrame(columns=["identifier", "event_count", "amount_charged"])
        route_ident = pd.DataFrame(columns=["route", "identifier", "event_count"])
        runtrip_ident = pd.DataFrame(columns=["route", "run", "trip", "identifier", "event_count"])
    else:
        agg = _canonicalize_aggregates(agg, key_col=None, key_raw_col="key_raw", ttp_raw_col="ttp_raw")
        agg = agg[agg["identifier"].ne("")].copy()
        ident = agg.groupby("identifier", as_index=False).agg(event_count=("event_count", "sum"), amount_charged=("amount_charged", "sum"))
        route_ident = agg.groupby(["route", "identifier"], as_index=False).agg(event_count=("event_count", "sum"))
        runtrip_ident = agg.groupby(["route", "run", "trip", "identifier"], as_index=False).agg(event_count=("event_count", "sum"))

    return LargeCSVProfile(
        kind="LEGACY_TRANSACTION_DETAIL",
        source_path=path,
        totals={k: float(v or 0) for k, v in total.items()},
        identifier_summary=ident,
        route_identifier=route_ident,
        route_run_trip_identifier=runtrip_ident,
        metadata={"header_row": str(header_row + 1), "engine": "DuckDB streaming/aggregate scan"},
        confidence=100,
    )
