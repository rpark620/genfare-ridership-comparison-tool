from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

try:
    from pypdf import PdfReader
except Exception:  # optional until PDF fallback used
    PdfReader = None


WORD_TO_KEY = {
    "ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4",
    "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9",
}


@dataclass
class ParseResult:
    kind: str
    data: pd.DataFrame = field(default_factory=pd.DataFrame)
    totals: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    confidence: int = 100


def _bytes(obj) -> bytes:
    if obj is None:
        return b""
    if isinstance(obj, bytes):
        return obj
    if hasattr(obj, "getvalue"):
        return obj.getvalue()
    if hasattr(obj, "read"):
        pos = None
        try:
            pos = obj.tell()
        except Exception:
            pass
        b = obj.read()
        try:
            if pos is not None:
                obj.seek(pos)
        except Exception:
            pass
        return b
    raise TypeError("Unsupported file object")


def _text_from_bytes(b: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def normalize_col_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def find_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    norm = {normalize_col_name(c): c for c in df.columns}
    for cand in candidates:
        n = normalize_col_name(cand)
        if n in norm:
            return norm[n]
    # conservative contains fallback
    for cand in candidates:
        n = normalize_col_name(cand)
        for nc, orig in norm.items():
            if n and (n in nc or nc in n):
                return orig
    return None


def normalize_key(value) -> Optional[str]:
    if pd.isna(value):
        return None
    s = str(value).strip().upper()
    if not s or s in {"NAN", "NONE"}:
        return None
    # Do not interpret the number in an explicit TTP label as a fare key.
    if "TTP" in s:
        return None
    s = s.replace("KEY", " ").strip()
    for word, digit in WORD_TO_KEY.items():
        if re.search(rf"\b{word}\b", s):
            return digit
    m = re.search(r"\b([0-9A-D*])\b", s)
    if m:
        return m.group(1)
    # Sometimes only the literal letter remains (e.g. D)
    if s in set("ABCD*"):
        return s
    return None


def normalize_ttp(value) -> Optional[str]:
    if pd.isna(value):
        return None
    s = str(value).strip().upper()
    if not s or s in {"NAN", "NONE", "0", "0.0"}:
        return None
    m = re.search(r"TTP\s*([0-9]+)", s)
    if not m:
        m = re.search(r"\b([0-9]+)(?:\.0)?\b", s)
    if not m:
        return None
    n = int(m.group(1))
    # 255 is commonly a non-product/sentinel value in legacy transaction detail.
    if n in {0, 255}:
        return None
    return str(n)


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False).str.replace("$", "", regex=False), errors="coerce").fillna(0)


def read_tabular_csv(file_obj, header="infer", skiprows=None) -> pd.DataFrame:
    b = _bytes(file_obj)
    text = _text_from_bytes(b)
    return pd.read_csv(io.StringIO(text), header=header, skiprows=skiprows, engine="python")


def parse_gfl_ridership(file_obj) -> ParseResult:
    df = read_tabular_csv(file_obj)
    rid_col = find_column(df, ["Ridership", "Boardings", "Riders"])
    route_col = find_column(df, ["Route", "Route Number", "Route ID"])
    keyttp_col = find_column(df, ["Key/TTP", "Key TTP", "Fare Identifier"])
    amount_col = find_column(df, ["Amount Charged", "Amount", "Charge"])
    if not rid_col or not route_col:
        raise ValueError("This does not look like a GFL ridership raw-data export (Ridership/Route columns not found).")

    out = pd.DataFrame(index=df.index)
    mapping = {
        "transaction_time": ["Transaction Time", "Date Time", "Timestamp"],
        "product_type": ["Product Type"],
        "product": ["Product"],
        "organization": ["Organization", "Agency"],
        "media_type": ["Media Type"],
        "bus": ["Bus Number", "Bus"],
        "driver": ["Driver"],
        "route": ["Route", "Route Number", "Route ID"],
        "run": ["Run"],
        "trip": ["Trip"],
        "fareset": ["Fareset ID", "FS", "Fareset"],
        "key_ttp_raw": ["Key/TTP", "Key TTP", "Fare Identifier"],
        "amount_charged": ["Amount Charged", "Amount", "Charge"],
        "ridership": ["Ridership", "Boardings", "Riders"],
    }
    for std, cands in mapping.items():
        c = find_column(df, cands)
        out[std] = df[c] if c else pd.NA

    out["ridership"] = _numeric(out["ridership"])
    out["amount_charged"] = _numeric(out["amount_charged"])
    out["key"] = out["key_ttp_raw"].apply(normalize_key)
    out["ttp"] = out["key_ttp_raw"].apply(normalize_ttp)
    out["identifier"] = out.apply(
        lambda r: f"KEY {r['key']}" if pd.notna(r["key"]) and r["key"] else (f"TTP {r['ttp']}" if pd.notna(r["ttp"]) and r["ttp"] else str(r.get("product") or "UNKNOWN")),
        axis=1,
    )
    for c in ["route", "run", "trip", "bus", "driver"]:
        out[c] = out[c].astype(str).str.replace(r"\.0$", "", regex=True).replace({"nan": "", "<NA>": ""})

    return ParseResult(
        kind="GFL_RIDERSHIP_RAW",
        data=out,
        totals={"ridership": float(out["ridership"].sum()), "amount_charged": float(out["amount_charged"].sum()), "rows": float(len(out))},
        metadata={"detected_ridership_column": rid_col, "detected_key_ttp_column": keyttp_col or "not found", "detected_amount_column": amount_col or "not found"},
        warnings=[] if amount_col else ["No Amount Charged column found; independent GFL revenue file will be required."],
        confidence=100 if keyttp_col else 90,
    )


def parse_gfl_revenue(file_obj) -> ParseResult:
    df = read_tabular_csv(file_obj)
    rev_col = find_column(df, ["Revenue", "Net Revenue", "Amount Charged"])
    route_col = find_column(df, ["Route", "Route Number", "Route ID"])
    if not rev_col or not route_col:
        raise ValueError("This does not look like a GFL revenue raw-data export (Revenue/Route columns not found).")

    out = pd.DataFrame(index=df.index)
    mapping = {
        "transaction_time": ["Transaction Time", "Date Time", "Timestamp"],
        "product_type": ["Product Type"],
        "product": ["Product"],
        "organization": ["Organization", "Agency"],
        "media_type": ["Media Type"],
        "bus": ["Bus Number", "Bus"],
        "route": ["Route", "Route Number", "Route ID"],
        "run": ["Run"],
        "trip": ["Trip"],
        "fareset": ["Fareset ID", "FS", "Fareset"],
        "revenue": ["Revenue", "Net Revenue", "Amount Charged"],
    }
    for std, cands in mapping.items():
        c = find_column(df, cands)
        out[std] = df[c] if c else pd.NA
    out["revenue"] = _numeric(out["revenue"])
    for c in ["route", "run", "trip", "bus"]:
        out[c] = out[c].astype(str).str.replace(r"\.0$", "", regex=True).replace({"nan": "", "<NA>": ""})
    return ParseResult(kind="GFL_REVENUE_RAW", data=out, totals={"revenue": float(out["revenue"].sum()), "rows": float(len(out))}, confidence=100)


def parse_legacy_transaction_detail(file_obj) -> ParseResult:
    b = _bytes(file_obj)
    text = _text_from_bytes(b)
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines[:40]):
        low = line.lower()
        if "transaction type" in low and "date time" in low and "route" in low and "run" in low and "trip" in low:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not locate the Legacy Transaction Detail CSV header.")
    df = pd.read_csv(io.StringIO(text), skiprows=header_idx, engine="python")
    # Drop trailing search-criteria/report footer rows by requiring Transaction Type or Date Time.
    tx_col = find_column(df, ["Transaction Type", "Type"])
    dt_col = find_column(df, ["Date Time", "DateTime", "Transaction Time"])
    if not tx_col or not dt_col:
        raise ValueError("Legacy Transaction Detail columns were not recognized.")
    df = df[df[tx_col].notna() & df[dt_col].notna()].copy()

    out = pd.DataFrame(index=df.index)
    mapping = {
        "transaction_type": ["Transaction Type", "Type"],
        "product": ["Product"],
        "transaction_time": ["Date Time", "DateTime"],
        "bus": ["Bus"],
        "driver": ["Driver"],
        "route": ["Route"],
        "run": ["Run"],
        "trip": ["Trip"],
        "fareset": ["FS", "Fareset"],
        "fare_box": ["Fare Box", "Farebox", "Fbox"],
        "keys_raw": ["Keys", "Key"],
        "ttp_raw": ["TTP"],
        "amount_charged": ["Amt Chrg", "Amount Charged", "Amount"],
    }
    for std, cands in mapping.items():
        c = find_column(df, cands)
        out[std] = df[c] if c else pd.NA
    out["amount_charged"] = _numeric(out["amount_charged"])
    out["key"] = out["keys_raw"].apply(normalize_key)
    out["ttp"] = out["ttp_raw"].apply(normalize_ttp)

    # Some exports place a human-readable key in Product while Keys is blank.
    missing_key = out["key"].isna()
    out.loc[missing_key, "key"] = out.loc[missing_key, "product"].apply(normalize_key)

    out["identifier"] = out.apply(
        lambda r: f"KEY {r['key']}" if pd.notna(r["key"]) and r["key"] else (f"TTP {r['ttp']}" if pd.notna(r["ttp"]) and r["ttp"] else ""),
        axis=1,
    )
    out["is_fare_event"] = out["identifier"].ne("")
    for c in ["route", "run", "trip", "bus", "driver"]:
        out[c] = out[c].astype(str).str.replace(r"\.0$", "", regex=True).replace({"nan": "", "<NA>": ""})

    return ParseResult(
        kind="LEGACY_TRANSACTION_DETAIL",
        data=out,
        totals={"rows": float(len(out)), "fare_identifier_rows": float(out["is_fare_event"].sum()), "amount_charged": float(out["amount_charged"].sum())},
        metadata={"header_row": str(header_idx + 1)},
        confidence=100,
    )


def _clean_cell(x) -> str:
    return str(x or "").strip().strip('"')


def parse_legacy_routesum_csv(file_obj) -> ParseResult:
    b = _bytes(file_obj)
    text = _text_from_bytes(b)
    rows = list(csv.reader(io.StringIO(text)))
    title = _clean_cell(rows[0][0]) if rows and rows[0] else ""
    metadata = {"title": title}
    warnings: List[str] = []

    section_idx = None
    for i, row in enumerate(rows):
        if any("Revenue and Ridership By Route" in _clean_cell(c) for c in row):
            section_idx = i
            break
    if section_idx is None:
        raise ValueError("Revenue and Ridership By Route section was not found in the Legacy ROUTESUM CSV.")

    header_idx = None
    for i in range(section_idx + 1, min(section_idx + 8, len(rows))):
        cells = [_clean_cell(c) for c in rows[i]]
        if "Ridership" in cells and any("Current Revenue" == c for c in cells):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not identify the Legacy ROUTESUM Revenue/Ridership header row.")

    headers = [_clean_cell(c) for c in rows[header_idx]]
    def idx_of(label):
        for j, h in enumerate(headers):
            if h.lower() == label.lower():
                return j
        return None

    route_i = idx_of("Route")
    date_i = idx_of("Date")
    current_i = idx_of("Current Revenue")
    unclass_i = idx_of("Unclassified Revenue")
    rider_i = idx_of("Ridership")
    if route_i is None or current_i is None or rider_i is None:
        raise ValueError("Legacy ROUTESUM required columns were not recognized.")

    records = []
    total_row = None
    for row in rows[header_idx + 1:]:
        if not row or not any(_clean_cell(c) for c in row):
            if records:
                break
            continue
        first = _clean_cell(row[route_i]) if route_i < len(row) else ""
        if first.upper() == "TOTAL":
            total_row = row
            break
        if first.upper() in {"ROUTE TOTAL", "LOCATION TOTAL", "GRAND TOTAL", "SEARCH CRITERIA:"}:
            break
        if not re.match(r"^-?\d+(?:\.0)?$", first):
            continue
        def get(i, default=""):
            return _clean_cell(row[i]) if i is not None and i < len(row) else default
        records.append({
            "route": re.sub(r"\.0$", "", first),
            "date": get(date_i),
            "current_revenue": float(get(current_i, "0").replace(",", "") or 0),
            "unclassified_revenue": float(get(unclass_i, "0").replace(",", "") or 0) if unclass_i is not None else 0.0,
            "ridership": float(get(rider_i, "0").replace(",", "") or 0),
        })
    out = pd.DataFrame(records)
    if out.empty:
        warnings.append("ROUTESUM Revenue/Ridership section was recognized but contained no data rows.")

    totals = {
        "ridership": float(out["ridership"].sum()) if not out.empty else 0.0,
        "current_revenue": float(out["current_revenue"].sum()) if not out.empty else 0.0,
        "unclassified_revenue": float(out["unclassified_revenue"].sum()) if not out.empty else 0.0,
    }
    if total_row is not None:
        def total_get(i):
            try:
                return float(_clean_cell(total_row[i]).replace(",", "") or 0)
            except Exception:
                return 0.0
        totals["ridership"] = total_get(rider_i)
        totals["current_revenue"] = total_get(current_i)
        totals["unclassified_revenue"] = total_get(unclass_i) if unclass_i is not None else 0.0
    totals["total_revenue"] = totals["current_revenue"] + totals["unclassified_revenue"]

    # Detect presence of other sections; detailed parsing can use Transaction Detail as preferred evidence.
    joined = "\n".join(",".join(r) for r in rows)
    metadata["has_key_count_section"] = str("Key Count By Route" in joined)
    metadata["has_ttp_section"] = str("Token, Ticket, and Pass Count" in joined)
    if "Token, Ticket, and Pass Count" in joined:
        warnings.append("TTP summary section detected. V1 uses Legacy Transaction Detail CSV as the preferred source for TTP/key drill-down because ROUTESUM CSV matrices vary by agency.")
    return ParseResult(kind="LEGACY_ROUTESUM_CSV", data=out, totals=totals, metadata=metadata, warnings=warnings, confidence=100)


def parse_legacy_routesum_pdf(file_obj) -> ParseResult:
    if PdfReader is None:
        raise ValueError("PDF support requires pypdf.")
    b = _bytes(file_obj)
    reader = PdfReader(io.BytesIO(b))
    pages = [p.extract_text() or "" for p in reader.pages]
    text = "\n".join(pages)
    if "EVENT SUMMARY (ROUTESUM)" not in text.upper():
        raise ValueError("This PDF does not look like a Legacy ROUTESUM report.")

    warnings = [
        "PDF fallback mode: route/revenue/ridership summary is supported, but CSV is strongly preferred for reliable detailed key/TTP extraction."
    ]
    records = []
    # Known BY ROUTE layout: route + current rev + unclassified rev + several counts + ridership.
    in_summary = False
    for page in pages:
        if "Revenue and Ridership By Route" not in page:
            continue
        for line in page.splitlines():
            s = line.strip()
            if re.match(r"^\d+\s", s):
                parts = s.split()
                if len(parts) >= 10 and re.match(r"^\d+$", parts[0]):
                    try:
                        records.append({
                            "route": parts[0],
                            "date": "",
                            "current_revenue": float(parts[1].replace(",", "")),
                            "unclassified_revenue": float(parts[2].replace(",", "")),
                            "ridership": float(parts[-1].replace(",", "")),
                        })
                    except Exception:
                        pass
    out = pd.DataFrame(records)

    totals = {}
    m = re.search(r"TOTAL\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+.*?\s([\d,]+)\s*$", text, re.M)
    if m:
        totals["current_revenue"] = float(m.group(1).replace(",", ""))
        totals["unclassified_revenue"] = float(m.group(2).replace(",", ""))
        totals["ridership"] = float(m.group(3).replace(",", ""))
    else:
        totals["current_revenue"] = float(out["current_revenue"].sum()) if not out.empty else 0.0
        totals["unclassified_revenue"] = float(out["unclassified_revenue"].sum()) if not out.empty else 0.0
        totals["ridership"] = float(out["ridership"].sum()) if not out.empty else 0.0
        warnings.append("Could not confidently locate the PDF TOTAL row; totals were summed from parsed route rows.")
    totals["total_revenue"] = totals["current_revenue"] + totals["unclassified_revenue"]
    return ParseResult(kind="LEGACY_ROUTESUM_PDF", data=out, totals=totals, warnings=warnings, confidence=85)


def parse_legacy_routesum(file_obj, filename: str = "") -> ParseResult:
    name = (filename or getattr(file_obj, "name", "") or "").lower()
    if name.endswith(".pdf"):
        return parse_legacy_routesum_pdf(file_obj)
    return parse_legacy_routesum_csv(file_obj)
