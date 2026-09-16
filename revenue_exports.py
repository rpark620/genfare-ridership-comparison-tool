from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(result: dict, diagnostics: pd.DataFrame | None = None) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame([result["summary"]]).to_excel(writer, sheet_name="Summary", index=False)
        result["findings"].to_excel(writer, sheet_name="Findings", index=False)
        drill = result.get("drilldowns", {})
        sheet_map = [
            ("day_comparison", "By_Day"),
            ("period_route_comparison", "Period_Routes"),
            ("day_route_comparison", "Day_Routes"),
            ("day_product_investigation", "Day_Products_Investigative"),
            ("day_run_investigation", "Day_Runs_Investigative"),
            ("day_bus_investigation", "Day_Buses_Investigative"),
            ("day_route_run_bus_investigation", "Day_Route_Run_Bus_Inv"),
            ("equal_opposite_movements", "Equal_Opposite_Moves"),
            ("transaction_matches", "Transaction_Matches"),
        ]
        for key, sheet in sheet_map:
            df = drill.get(key, pd.DataFrame())
            if df is not None and not df.empty:
                df.to_excel(writer, sheet_name=sheet[:31], index=False)
        if diagnostics is not None and not diagnostics.empty:
            diagnostics.to_excel(writer, sheet_name="Diagnostics", index=False)

        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col_cells in ws.columns:
                max_len = 0
                for cell in list(col_cells)[:250]:
                    value = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, min(len(value), 60))
                ws.column_dimensions[col_cells[0].column_letter].width = max(10, min(max_len + 2, 45))
    return output.getvalue()


def _json_scalar(value):
    if value is None:
        return None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if pd.isna(value) or np.isinf(value):
            return None
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict]:
    if df is None or df.empty:
        return []
    work = df.head(limit).copy() if limit is not None else df.copy()
    return [{str(k): _json_scalar(v) for k, v in row.items()} for row in work.to_dict(orient="records")]


def build_ai_evidence_package(result: dict, diagnostics: pd.DataFrame | None = None) -> dict:
    drill = result.get("drilldowns", {})
    findings = result.get("findings", pd.DataFrame()).copy()
    if not findings.empty:
        findings["_abs"] = pd.to_numeric(findings.get("Difference"), errors="coerce").abs().fillna(0)
        findings = findings.sort_values("_abs", ascending=False).drop(columns="_abs")
    return {
        "package_type": "Genfare revenue reconciliation evidence",
        "package_version": "5.0",
        "scope": "REVENUE ONLY",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_instructions": [
            "Use ROUTESUM Current Revenue + Unclassified Revenue as the authoritative Legacy revenue total.",
            "Use the dedicated GenfareLink Revenue raw-data export as the authoritative GFL revenue total.",
            "Only treat day-level and route-level Legacy revenue as authoritative when it comes from ROUTESUM / ROUTESUM BY ROUTE-DATE.",
            "Legacy Transaction Detail Amt Chrg is investigative evidence only. Do not assume it should sum to ROUTESUM revenue.",
            "Distinguish accounting-total differences from allocation differences.",
            "Equal-and-opposite route movements may indicate route reassignment / Data Edit when systemwide revenue is unchanged.",
            "Do not invent product-level Legacy revenue if the evidence package labels it investigative.",
        ],
        "summary": {k: _json_scalar(v) for k, v in result["summary"].items()},
        "diagnostics": _records(diagnostics if diagnostics is not None else pd.DataFrame()),
        "drilldown": {
            "by_day": _records(drill.get("day_comparison", pd.DataFrame()), 1000),
            "period_routes": _records(drill.get("period_route_comparison", pd.DataFrame()), 1000),
            "day_routes": _records(drill.get("day_route_comparison", pd.DataFrame()), 1000),
            "day_products_investigative": _records(drill.get("day_product_investigation", pd.DataFrame()), 1000),
            "day_runs_investigative": _records(drill.get("day_run_investigation", pd.DataFrame()), 1000),
            "day_buses_investigative": _records(drill.get("day_bus_investigation", pd.DataFrame()), 1000),
            "equal_and_opposite_movements": _records(drill.get("equal_opposite_movements", pd.DataFrame()), 1000),
            "transaction_matches": _records(drill.get("transaction_matches", pd.DataFrame()), 1000),
            "notes": list(drill.get("notes", [])),
        },
        "largest_findings": _records(findings, 300),
        "privacy_note": "The package contains aggregate reconciliation evidence and does not include card IDs, Track 2, or ticket/sequence identifiers.",
    }


def to_ai_json_bytes(result: dict, diagnostics: pd.DataFrame | None = None) -> bytes:
    return json.dumps(build_ai_evidence_package(result, diagnostics), indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")


def to_ai_prompt_bytes() -> bytes:
    text = """# Genfare Revenue Reconciliation V5 — AI Review Prompt

I am attaching `revenue_ai_evidence_v5.json`, generated by a deterministic Genfare reconciliation tool.

Analyze REVENUE ONLY.

Source-of-truth rules:
- Legacy authoritative revenue = ROUTESUM Current Revenue + Unclassified Revenue.
- GenfareLink authoritative revenue = dedicated Revenue raw-data export.
- Legacy Transaction Detail `Amt Chrg` is investigative evidence only and may not sum to ROUTESUM revenue.
- Do not convert investigative product/run/bus amounts into authoritative accounting conclusions.

Follow this order:
1. Overall revenue difference.
2. Day-level differences where ROUTESUM BY ROUTE-DATE provides authoritative daily totals.
3. Authoritative route-level differences.
4. Product/run/bus investigative evidence for the affected day(s).
5. Equal-and-opposite route movements that may indicate allocation / Data Edit rather than missing revenue.

For each important issue, state:
- what differs,
- whether the difference is authoritative or investigative,
- likely cause,
- confidence (High / Medium / Low),
- evidence,
- whether it affects total revenue or only allocation,
- next check to perform.

Return:
- Executive summary
- Overall reconciliation
- Problem days
- Route findings
- Product/run/bus investigative findings
- Allocation/Data Edit findings
- Remaining unresolved issues
- Recommended next checks
"""
    return text.encode("utf-8")
