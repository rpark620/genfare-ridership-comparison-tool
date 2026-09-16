from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import pandas as pd
from pypdf import PdfReader


@dataclass
class LegacyRouteSum:
    kind: str
    route_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    key_counts: pd.DataFrame = field(default_factory=pd.DataFrame)
    key_display_ridership: pd.DataFrame = field(default_factory=pd.DataFrame)
    ttp_counts: pd.DataFrame = field(default_factory=pd.DataFrame)
    totals: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    confidence: int = 100


def _bytes(file_obj) -> bytes:
    if isinstance(file_obj, (bytes, bytearray)):
        return bytes(file_obj)
    if isinstance(file_obj, str):
        return Path(file_obj).read_bytes()
    if hasattr(file_obj, "getvalue"):
        return file_obj.getvalue()
    if hasattr(file_obj, "read"):
        pos = None
        try:
            pos = file_obj.tell()
        except Exception:
            pass
        b = file_obj.read()
        try:
            if pos is not None:
                file_obj.seek(pos)
        except Exception:
            pass
        return b
    raise TypeError("Unsupported ROUTESUM file")


def _text(b: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="replace")


def _empty_long(value_name="count"):
    return pd.DataFrame(columns=["route", "identifier", value_name])


def parse_routesum_pdf(file_obj) -> LegacyRouteSum:
    reader = PdfReader(io.BytesIO(_bytes(file_obj)))
    pages = [p.extract_text() or "" for p in reader.pages]
    whole = "\n".join(pages)
    if "EVENT SUMMARY (ROUTESUM)" not in whole.upper():
        raise ValueError("This PDF does not look like an EVENT SUMMARY (ROUTESUM) report.")

    route_rows = []
    for page in pages:
        if "Revenue and Ridership By Route" not in page:
            continue
        for line in page.splitlines():
            parts = line.strip().split()
            if not parts or not re.fullmatch(r"\d+", parts[0]):
                continue
            try:
                if len(parts) >= 11 and re.fullmatch(r"\d{2}/\d{2}/\d{4}", parts[1]):
                    # BY ROUTE-DATE layout
                    route, date = parts[0], parts[1]
                    current, unclass = float(parts[2].replace(",", "")), float(parts[3].replace(",", ""))
                    dump, preset = int(parts[4]), int(parts[5])
                    ridership = float(parts[-1].replace(",", ""))
                elif len(parts) >= 10:
                    route, date = parts[0], ""
                    current, unclass = float(parts[1].replace(",", "")), float(parts[2].replace(",", ""))
                    dump, preset = int(parts[3]), int(parts[4])
                    ridership = float(parts[-1].replace(",", ""))
                else:
                    continue
                route_rows.append({
                    "route": route, "date": date, "current_revenue": current,
                    "unclassified_revenue": unclass, "dump_count": dump,
                    "preset": preset, "ridership": ridership,
                })
            except Exception:
                continue
    route = pd.DataFrame(route_rows)
    if not route.empty:
        route = route.groupby("route", as_index=False).agg(
            current_revenue=("current_revenue", "sum"),
            unclassified_revenue=("unclassified_revenue", "sum"),
            dump_count=("dump_count", "sum"),
            preset=("preset", "sum"),
            ridership=("ridership", "sum"),
        )

    # Key Count By Route. The report's PDF text extraction often joins Key C and Preset.
    # We avoid relying on that joined token by deriving Key C from the row's Total key count.
    key_records = []
    first12 = [f"KEY {x}" for x in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "A", "B"]]
    for page in pages:
        if "Key Count By Route" not in page or "Preset and Key Count" in page:
            continue
        for line in page.splitlines():
            parts = line.strip().split()
            if len(parts) < 10 or not re.fullmatch(r"\d+", parts[0]):
                continue
            route_id = parts[0]
            vals = parts[1:]
            try:
                total = int(vals[-1])
                key_d = int(vals[-2])
                first_vals = list(map(int, vals[:12]))
            except Exception:
                continue
            if len(first_vals) != 12:
                continue
            key_c = total - sum(first_vals) - key_d
            if key_c < 0:
                continue
            for ident, value in zip(first12, first_vals):
                key_records.append({"route": route_id, "identifier": ident, "count": value})
            key_records.append({"route": route_id, "identifier": "KEY C", "count": key_c})
            key_records.append({"route": route_id, "identifier": "KEY D", "count": key_d})
    key_counts = pd.DataFrame(key_records) if key_records else _empty_long()
    if not key_counts.empty:
        key_counts = key_counts.groupby(["route", "identifier"], as_index=False)["count"].sum()

    # "Preset and Key Count with Riderships" rows are numerically laid out as:
    # Preset, Key1..Key9, Key*, KeyA..KeyD, Total.
    key_rid_records = []
    key_order = [f"KEY {x}" for x in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "A", "B", "C", "D"]]
    for page in pages:
        if "Preset and Key Count with Riderships by Route" not in page:
            continue
        for line in page.splitlines():
            parts = line.strip().split()
            if len(parts) != 17 or not re.fullmatch(r"\d+", parts[0]):
                continue
            route_id = parts[0]
            try:
                values = list(map(int, parts[1:]))
            except Exception:
                continue
            # values[0] is preset; values[1:15] are Key1..D; values[15] is Total.
            for ident, value in zip(key_order, values[1:15]):
                key_rid_records.append({"route": route_id, "identifier": ident, "displayed_ridership": value})
    key_display = pd.DataFrame(key_rid_records) if key_rid_records else pd.DataFrame(columns=["route", "identifier", "displayed_ridership"])
    if not key_display.empty:
        key_display = key_display.groupby(["route", "identifier"], as_index=False)["displayed_ridership"].sum()

    # TTP pages have a stable property that survives PDF extraction: the ordered TTP IDs and
    # one numeric value per TTP followed by Total.
    ttp_records = []
    ttp_names: Dict[str, str] = {}
    for page in pages:
        if "Token, Ticket, and Pass Count" not in page:
            continue
        ids = []
        for ident in re.findall(r"TTP\s*(\d+)", page, flags=re.I):
            if ident not in ids:
                ids.append(ident)
        if not ids:
            continue
        # Best-effort labels for diagnostics only.
        lines = page.splitlines()
        for i, line in enumerate(lines):
            m = re.fullmatch(r"\s*TTP\s*(\d+)\s*", line, flags=re.I)
            if m and i + 1 < len(lines):
                label = lines[i + 1].strip()
                if label and not label.upper().startswith("TTP") and label.upper() != "TOTAL":
                    ttp_names[f"TTP {m.group(1)}"] = label
        for line in lines:
            parts = line.strip().split()
            if not parts or not re.fullmatch(r"\d+", parts[0]):
                continue
            # BY ROUTE: route + N TTP counts + total.
            # BY ROUTE-DATE: route + date + N TTP counts.
            route_id = parts[0]
            start = 1
            if len(parts) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
                start = 2
            numeric = parts[start:]
            if len(numeric) == len(ids) + 1:
                numeric = numeric[:-1]
            if len(numeric) != len(ids):
                continue
            try:
                values = list(map(int, numeric))
            except Exception:
                continue
            for ident, value in zip(ids, values):
                ttp_records.append({"route": route_id, "identifier": f"TTP {ident}", "count": value})
    ttp_counts = pd.DataFrame(ttp_records) if ttp_records else _empty_long()
    if not ttp_counts.empty:
        ttp_counts = ttp_counts.groupby(["route", "identifier"], as_index=False)["count"].sum()

    totals = {
        "ridership": float(route["ridership"].sum()) if not route.empty else 0.0,
        "current_revenue": float(route["current_revenue"].sum()) if not route.empty else 0.0,
        "unclassified_revenue": float(route["unclassified_revenue"].sum()) if not route.empty else 0.0,
    }
    totals["total_revenue"] = totals["current_revenue"] + totals["unclassified_revenue"]

    warnings = []
    if key_counts.empty:
        warnings.append("No Key Count By Route matrix was parsed from this ROUTESUM report.")
    if ttp_counts.empty:
        warnings.append("No Token/Ticket/Pass TTP matrix was parsed from this ROUTESUM report.")
    if key_display.empty:
        warnings.append("No detailed key-ridership display matrix was parsed; internal legacy display checks will be limited.")

    return LegacyRouteSum(
        kind="LEGACY_ROUTESUM_PDF",
        route_summary=route,
        key_counts=key_counts,
        key_display_ridership=key_display,
        ttp_counts=ttp_counts,
        totals=totals,
        metadata={"pages": str(len(pages)), "ttp_names": repr(ttp_names)},
        warnings=warnings,
        confidence=98 if not route.empty else 70,
    )


def parse_routesum_csv(file_obj) -> LegacyRouteSum:
    rows = list(csv.reader(io.StringIO(_text(_bytes(file_obj)))))
    section = next((i for i, r in enumerate(rows) if any("Revenue and Ridership By Route" in str(c) for c in r)), None)
    if section is None:
        raise ValueError("Revenue and Ridership By Route section was not found in the Legacy ROUTESUM CSV.")
    header_idx = None
    for i in range(section + 1, min(section + 8, len(rows))):
        cleaned = [str(c).strip().strip('"') for c in rows[i]]
        if "Ridership" in cleaned and "Current Revenue" in cleaned:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not identify the ROUTESUM CSV summary header.")
    headers = [str(c).strip().strip('"') for c in rows[header_idx]]
    def idx(label):
        return next((j for j, h in enumerate(headers) if h.lower() == label.lower()), None)
    route_i, date_i = idx("Route"), idx("Date")
    cur_i, un_i, rid_i = idx("Current Revenue"), idx("Unclassified Revenue"), idx("Ridership")
    preset_i = next((j for j, h in enumerate(headers) if "preset" in h.lower()), None)
    dump_i = next((j for j, h in enumerate(headers) if "dump" in h.lower()), None)
    if route_i is None or cur_i is None or rid_i is None:
        raise ValueError("ROUTESUM CSV required columns were not recognized.")
    records = []
    for row in rows[header_idx + 1:]:
        if route_i >= len(row):
            continue
        r = str(row[route_i]).strip().strip('"')
        if r.upper() == "TOTAL":
            break
        if not re.fullmatch(r"\d+(?:\.0)?", r):
            continue
        def get(i, default="0"):
            return str(row[i]).strip().strip('"') if i is not None and i < len(row) else default
        def num(i):
            try:
                return float(get(i).replace(",", ""))
            except Exception:
                return 0.0
        records.append({
            "route": re.sub(r"\.0$", "", r), "date": get(date_i, ""),
            "current_revenue": num(cur_i), "unclassified_revenue": num(un_i),
            "dump_count": num(dump_i), "preset": num(preset_i), "ridership": num(rid_i),
        })
    route = pd.DataFrame(records)
    if not route.empty:
        route = route.groupby("route", as_index=False).agg(
            current_revenue=("current_revenue", "sum"), unclassified_revenue=("unclassified_revenue", "sum"),
            dump_count=("dump_count", "sum"), preset=("preset", "sum"), ridership=("ridership", "sum")
        )
    totals = {
        "ridership": float(route["ridership"].sum()) if not route.empty else 0.0,
        "current_revenue": float(route["current_revenue"].sum()) if not route.empty else 0.0,
        "unclassified_revenue": float(route["unclassified_revenue"].sum()) if not route.empty else 0.0,
    }
    totals["total_revenue"] = totals["current_revenue"] + totals["unclassified_revenue"]
    return LegacyRouteSum(
        kind="LEGACY_ROUTESUM_CSV",
        route_summary=route,
        totals=totals,
        warnings=[
            "ROUTESUM CSV summary totals were parsed, but Legacy CSV TTP/key matrices are not consistently labeled across exports. "
            "For automatic ridership-rule inference, ROUTESUM PDF is preferred; Transaction Detail CSV can provide fallback evidence."
        ],
        confidence=95,
    )


def parse_routesum(file_obj, filename: str = "") -> LegacyRouteSum:
    name = (filename or getattr(file_obj, "name", "") or "").lower()
    return parse_routesum_pdf(file_obj) if name.endswith(".pdf") else parse_routesum_csv(file_obj)
