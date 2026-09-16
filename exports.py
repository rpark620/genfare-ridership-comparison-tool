from __future__ import annotations

import io
import pandas as pd


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def to_excel_bytes(result: dict, diagnostics: pd.DataFrame | None = None) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        result["findings"].to_excel(writer, sheet_name="Findings", index=False)
        pd.DataFrame([result["summary"]]).to_excel(writer, sheet_name="Summary", index=False)
        inf = result["rule_inference"]
        if not inf.rules.empty:
            inf.rules.to_excel(writer, sheet_name="Inferred_Rules", index=False)
        if not inf.route_fit.empty:
            inf.route_fit.to_excel(writer, sheet_name="Legacy_Ridership_Fit", index=False)
        if not result["route_reassignments"].empty:
            result["route_reassignments"].to_excel(writer, sheet_name="Possible_Data_Edits", index=False)
        if not result["run_trip"].empty:
            result["run_trip"].to_excel(writer, sheet_name="Run_Trip", index=False)
        if diagnostics is not None and not diagnostics.empty:
            diagnostics.to_excel(writer, sheet_name="Diagnostics", index=False)
        for level, name in [("Key", "Keys"), ("TTP", "TTPs"), ("Route", "Routes")]:
            sub = result["findings"][result["findings"]["Level"] == level]
            if not sub.empty:
                sub.to_excel(writer, sheet_name=name, index=False)
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
