from __future__ import annotations

import csv
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

WORD_TO_KEY = {
    "ZERO": "0", "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4",
    "FIVE": "5", "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9",
}


def normalize_col_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).strip().lower())


def find_header_column(headers: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    headers = list(headers)
    norm = {normalize_col_name(c): c for c in headers}
    for cand in candidates:
        n = normalize_col_name(cand)
        if n in norm:
            return norm[n]
    for cand in candidates:
        n = normalize_col_name(cand)
        if not n:
            continue
        for hn, original in norm.items():
            if n in hn or hn in n:
                return original
    return None


def normalize_key(value) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    s = str(value).strip().upper()
    if not s or s in {"NAN", "NONE", "NULL"} or "TTP" in s:
        return None
    s = s.replace("KEY", " ").strip()
    for word, digit in WORD_TO_KEY.items():
        if re.search(rf"\b{word}\b", s):
            return digit
    m = re.search(r"\b([0-9A-D*])\b", s)
    if m:
        return m.group(1)
    if s in set("ABCD*"):
        return s
    return None


def normalize_ttp(value) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    s = str(value).strip().upper()
    if not s or s in {"NAN", "NONE", "NULL", "0", "0.0"}:
        return None
    m = re.search(r"TTP\s*([0-9]+)", s)
    if not m:
        m = re.fullmatch(r"\s*([0-9]+)(?:\.0)?\s*", s)
    if not m:
        return None
    n = int(m.group(1))
    if n in {0, 255}:
        return None
    return str(n)


def canonical_identifier(key_raw=None, ttp_raw=None, product=None, product_type=None, key_ttp_raw=None) -> str:
    # Explicit combined Key/TTP field takes priority when available.
    if key_ttp_raw is not None and not pd.isna(key_ttp_raw):
        k = normalize_key(key_ttp_raw)
        if k:
            return f"KEY {k}"
        t = normalize_ttp(key_ttp_raw)
        if t:
            return f"TTP {t}"

    k = normalize_key(key_raw)
    if not k:
        k = normalize_key(product)
    if k:
        return f"KEY {k}"

    t = normalize_ttp(ttp_raw)
    if t:
        return f"TTP {t}"

    ptype = "" if product_type is None or pd.isna(product_type) else str(product_type).strip().upper()
    p = "" if product is None or pd.isna(product) else str(product).strip()
    if "PRESET" in ptype or p.upper() == "PRESET":
        return "PRESET"
    return ""


def clean_dimension(value) -> str:
    if value is None or pd.isna(value):
        return ""
    s = str(value).strip()
    if s.lower() in {"nan", "none", "null", "<na>"}:
        return ""
    return re.sub(r"\.0$", "", s)


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def quote_sql_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def save_uploaded_file(uploaded, suffix: str = "") -> str:
    if uploaded is None:
        raise ValueError("No file supplied")
    original = getattr(uploaded, "name", "") or "upload"
    ext = Path(original).suffix or suffix
    fd, path = tempfile.mkstemp(prefix="genfare_reconcile_", suffix=ext)
    os.close(fd)
    try:
        uploaded.seek(0)
    except Exception:
        pass
    with open(path, "wb") as out:
        shutil.copyfileobj(uploaded, out, length=1024 * 1024 * 8)
    try:
        uploaded.seek(0)
    except Exception:
        pass
    return path


def read_csv_header(path: str, skip_rows: int = 0) -> list[str]:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for _ in range(skip_rows):
            next(f, None)
        return next(csv.reader(f))


def find_legacy_tx_header_row(path: str, max_lines: int = 80) -> int:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            low = line.lower()
            if "transaction type" in low and "date time" in low and "route" in low and "run" in low and "trip" in low:
                return i
    raise ValueError("Could not locate the Legacy Transaction Detail CSV header row.")
