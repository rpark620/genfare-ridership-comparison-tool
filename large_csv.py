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
    day_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_route_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    day_route_run_bus_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    boarding_day_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    boarding_day_route_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    boarding_day_route_run_bus_identifier: pd.DataFrame = field(default_factory=pd.DataFrame)
    transaction_signatures: pd.DataFrame = field(default_factory=pd.DataFrame)
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


def _col_expr(col: Optional[str], alias: str, numeric: bool = False) -> str:
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


def _canonicalize_aggregates(df: pd.DataFrame, key_col="key_ttp_raw", key_raw_col=None, ttp_raw_col=None) -> pd.DataFrame:
    if df.empty:
        df = df.copy()
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
    for c in ["route", "run", "trip", "bus", "driver"]:
        if c in df.columns:
            df[c] = df[c].map(clean_dimension)
    if "date" in df.columns:
        df["date"] = df["date"].astype(str)
    if "timestamp" in df.columns:
        df["timestamp"] = df["timestamp"].astype(str)
    return df


def _group_identifier(df: pd.DataFrame, group_cols: list[str], has_ridership: bool) -> pd.DataFrame:
    if df.empty:
        cols = group_cols + ["identifier", "event_count"] + (["ridership"] if has_ridership else [])
        return pd.DataFrame(columns=cols)
    df = df[df["identifier"].ne("")].copy()
    agg = {"event_count": ("event_count", "sum")}
    if has_ridership:
        agg["ridership"] = ("ridership", "sum")
    return df.groupby(group_cols + ["identifier"], dropna=False, as_index=False).agg(**agg)


def profile_gfl_ridership(path: str) -> LargeCSVProfile:
    headers = read_csv_header(path)
    rid_col = find_header_column(headers, ["Ridership", "Boardings", "Riders"])
    route_col = find_header_column(headers, ["Route", "Route Number", "Route ID"])
    keyttp_col = find_header_column(headers, ["Key/TTP", "Key TTP", "Fare Identifier"])
    if not rid_col or not route_col:
        raise ValueError("This does not look like a GFL Ridership Raw Data export: Ridership and/or Route was not found.")

    mapping = {
        "transaction_time": find_header_column(headers, ["Transaction Time", "Date Time", "Timestamp"]),
        "product_type": find_header_column(headers, ["Product Type"]),
        "product": find_header_column(headers, ["Product"]),
        "bus": find_header_column(headers, ["Bus Number", "Bus"]),
        "driver": find_header_column(headers, ["Driver"]),
        "route": route_col,
        "run": find_header_column(headers, ["Run"]),
        "trip": find_header_column(headers, ["Trip"]),
        "key_ttp_raw": keyttp_col,
        "ridership": rid_col,
    }
    select = ",\n".join([
        _col_expr(mapping["transaction_time"], "transaction_time"),
        _col_expr(mapping["product_type"], "product_type"),
        _col_expr(mapping["product"], "product"),
        _col_expr(mapping["route"], "route"),
        _col_expr(mapping["run"], "run"),
        _col_expr(mapping["trip"], "trip"),
        _col_expr(mapping["bus"], "bus"),
        _col_expr(mapping["driver"], "driver"),
        _col_expr(mapping["key_ttp_raw"], "key_ttp_raw"),
        _col_expr(mapping["ridership"], "ridership", numeric=True),
    ])

    con = _connect()
    try:
        con.execute(f"CREATE TEMP VIEW src0 AS SELECT {select} FROM {_read_csv_expr(path)}")
        con.execute(f"""
            CREATE TEMP VIEW src AS
            SELECT *, {_date_expr()} AS date, {_timestamp_expr()} AS timestamp
            FROM src0
        """)
        totals = con.execute('SELECT COUNT(*) AS "rows", SUM(ridership) AS ridership FROM src').fetchdf().iloc[0].to_dict()

        ident_raw = con.execute("""
            SELECT product_type, product, key_ttp_raw, COUNT(*) event_count, SUM(ridership) ridership
            FROM src GROUP BY ALL
        """).fetchdf()
        ident_raw = _canonicalize_aggregates(ident_raw)
        ident = _group_identifier(ident_raw, [], True)

        route_ident_raw = con.execute("""
            SELECT route, product_type, product, key_ttp_raw, COUNT(*) event_count, SUM(ridership) ridership
            FROM src GROUP BY ALL
        """).fetchdf()
        route_ident_raw = _canonicalize_aggregates(route_ident_raw)
        route_ident = _group_identifier(route_ident_raw, ["route"], True)

        runtrip = con.execute("""
            SELECT route, run, trip, COUNT(*) event_count, SUM(ridership) ridership
            FROM src GROUP BY ALL
        """).fetchdf()
        for c in ["route", "run", "trip"]:
            runtrip[c] = runtrip[c].map(clean_dimension)

        runtrip_ident_raw = con.execute("""
            SELECT route, run, trip, product_type, product, key_ttp_raw,
                   COUNT(*) event_count, SUM(ridership) ridership
            FROM src GROUP BY ALL
        """).fetchdf()
        runtrip_ident_raw = _canonicalize_aggregates(runtrip_ident_raw)
        runtrip_ident = _group_identifier(runtrip_ident_raw, ["route", "run", "trip"], True)

        day_summary = con.execute("""
            SELECT date, COUNT(*) event_count, SUM(ridership) ridership
            FROM src WHERE date <> '' GROUP BY date ORDER BY date
        """).fetchdf()

        day_ident_raw = con.execute("""
            SELECT date, product_type, product, key_ttp_raw,
                   COUNT(*) event_count, SUM(ridership) ridership
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_ident_raw = _canonicalize_aggregates(day_ident_raw)
        day_identifier = _group_identifier(day_ident_raw, ["date"], True)

        day_route_ident_raw = con.execute("""
            SELECT date, route, product_type, product, key_ttp_raw,
                   COUNT(*) event_count, SUM(ridership) ridership
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_route_ident_raw = _canonicalize_aggregates(day_route_ident_raw)
        day_route_identifier = _group_identifier(day_route_ident_raw, ["date", "route"], True)

        day_detail_raw = con.execute("""
            SELECT date, route, run, bus, product_type, product, key_ttp_raw,
                   COUNT(*) event_count, SUM(ridership) ridership
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_detail_raw = _canonicalize_aggregates(day_detail_raw)
        day_route_run_bus_identifier = _group_identifier(day_detail_raw, ["date", "route", "run", "bus"], True)

        transaction_signatures = pd.DataFrame()
    finally:
        con.close()

    warnings = []
    if not keyttp_col:
        warnings.append("Key/TTP was not found. Key/TTP rule-difference analysis will be limited.")
    if not mapping["transaction_time"]:
        warnings.append("Transaction Time was not found. Day and transaction drill-downs will be unavailable.")

    return LargeCSVProfile(
        kind="GFL_RIDERSHIP_RAW",
        source_path=path,
        totals={k: float(v or 0) for k, v in totals.items()},
        identifier_summary=ident,
        route_identifier=route_ident,
        route_run_trip=runtrip,
        route_run_trip_identifier=runtrip_ident,
        day_summary=day_summary,
        day_identifier=day_identifier,
        day_route_identifier=day_route_identifier,
        day_route_run_bus_identifier=day_route_run_bus_identifier,
        transaction_signatures=transaction_signatures,
        metadata={
            "ridership_column": rid_col,
            "key_ttp_column": keyttp_col or "not found",
            "transaction_time_column": mapping["transaction_time"] or "not found",
            "engine": "DuckDB streaming/aggregate scan",
        },
        warnings=warnings,
        confidence=100 if keyttp_col else 90,
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
    bus_col = find_header_column(headers, ["Bus"])
    driver_col = find_header_column(headers, ["Driver"])
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
        _col_expr(driver_col, "driver"),
        _col_expr(key_col, "key_raw"),
        _col_expr(ttp_col, "ttp_raw"),
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
        total = con.execute('SELECT COUNT(*) AS "rows" FROM src').fetchdf().iloc[0].to_dict()

        agg = con.execute("""
            SELECT transaction_type, product, route, run, trip, bus, driver, key_raw, ttp_raw,
                   COUNT(*) event_count
            FROM src GROUP BY ALL
        """).fetchdf()
        day_raw = con.execute("""
            SELECT date, transaction_type, product, key_raw, ttp_raw, COUNT(*) event_count
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_route_raw = con.execute("""
            SELECT date, route, transaction_type, product, key_raw, ttp_raw, COUNT(*) event_count
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()
        day_detail_raw = con.execute("""
            SELECT date, route, run, bus, transaction_type, product, key_raw, ttp_raw, COUNT(*) event_count
            FROM src WHERE date <> '' GROUP BY ALL
        """).fetchdf()

        # Candidate fare-use/boarding transactions. Administrative issuance events such as
        # Issue card (142) and Transfer issued (128) are intentionally excluded.
        boarding_day_raw = con.execute("""
            SELECT date, transaction_type, product, key_raw, ttp_raw, COUNT(*) event_count
            FROM src
            WHERE date <> '' AND regexp_matches(transaction_type, '^(114|115|116|118|119)\\s*-')
            GROUP BY ALL
        """).fetchdf()
        boarding_day_route_raw = con.execute("""
            SELECT date, route, transaction_type, product, key_raw, ttp_raw, COUNT(*) event_count
            FROM src
            WHERE date <> '' AND regexp_matches(transaction_type, '^(114|115|116|118|119)\\s*-')
            GROUP BY ALL
        """).fetchdf()
        boarding_day_detail_raw = con.execute("""
            SELECT date, route, run, bus, transaction_type, product, key_raw, ttp_raw, COUNT(*) event_count
            FROM src
            WHERE date <> '' AND regexp_matches(transaction_type, '^(114|115|116|118|119)\\s*-')
            GROUP BY ALL
        """).fetchdf()
        sig_raw = pd.DataFrame()
    finally:
        con.close()

    def canon(frame: pd.DataFrame) -> pd.DataFrame:
        return _canonicalize_aggregates(frame, key_col=None, key_raw_col="key_raw", ttp_raw_col="ttp_raw")

    if agg.empty:
        ident = pd.DataFrame(columns=["identifier", "event_count"])
        route_ident = pd.DataFrame(columns=["route", "identifier", "event_count"])
        runtrip_ident = pd.DataFrame(columns=["route", "run", "trip", "identifier", "event_count"])
    else:
        agg = canon(agg)
        ident = _group_identifier(agg, [], False)
        route_ident = _group_identifier(agg, ["route"], False)
        runtrip_ident = _group_identifier(agg, ["route", "run", "trip"], False)

    day_identifier = _group_identifier(canon(day_raw), ["date"], False) if not day_raw.empty else pd.DataFrame(columns=["date", "identifier", "event_count"])
    day_route_identifier = _group_identifier(canon(day_route_raw), ["date", "route"], False) if not day_route_raw.empty else pd.DataFrame(columns=["date", "route", "identifier", "event_count"])
    day_route_run_bus_identifier = _group_identifier(canon(day_detail_raw), ["date", "route", "run", "bus"], False) if not day_detail_raw.empty else pd.DataFrame(columns=["date", "route", "run", "bus", "identifier", "event_count"])
    boarding_day_identifier = _group_identifier(canon(boarding_day_raw), ["date"], False) if not boarding_day_raw.empty else pd.DataFrame(columns=["date", "identifier", "event_count"])
    boarding_day_route_identifier = _group_identifier(canon(boarding_day_route_raw), ["date", "route"], False) if not boarding_day_route_raw.empty else pd.DataFrame(columns=["date", "route", "identifier", "event_count"])
    boarding_day_route_run_bus_identifier = _group_identifier(canon(boarding_day_detail_raw), ["date", "route", "run", "bus"], False) if not boarding_day_detail_raw.empty else pd.DataFrame(columns=["date", "route", "run", "bus", "identifier", "event_count"])
    transaction_signatures = _group_identifier(canon(sig_raw), ["date", "timestamp", "bus", "driver", "route", "run", "trip"], False) if not sig_raw.empty else pd.DataFrame(columns=["date", "timestamp", "bus", "driver", "route", "run", "trip", "identifier", "event_count"])

    day_summary = pd.DataFrame(columns=["date", "event_count"])
    if not day_identifier.empty:
        day_summary = day_identifier.groupby("date", as_index=False)["event_count"].sum()

    return LargeCSVProfile(
        kind="LEGACY_TRANSACTION_DETAIL",
        source_path=path,
        totals={k: float(v or 0) for k, v in total.items()},
        identifier_summary=ident,
        route_identifier=route_ident,
        route_run_trip_identifier=runtrip_ident,
        day_summary=day_summary,
        day_identifier=day_identifier,
        day_route_identifier=day_route_identifier,
        day_route_run_bus_identifier=day_route_run_bus_identifier,
        boarding_day_identifier=boarding_day_identifier,
        boarding_day_route_identifier=boarding_day_route_identifier,
        boarding_day_route_run_bus_identifier=boarding_day_route_run_bus_identifier,
        transaction_signatures=transaction_signatures,
        metadata={"header_row": str(header_row + 1), "engine": "DuckDB streaming/aggregate scan"},
        confidence=100,
    )
