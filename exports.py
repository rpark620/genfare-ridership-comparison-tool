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
        f = result["findings"]
        for level, name in [("Key", "Keys"), ("TTP", "TTPs"), ("Route", "Routes"), ("Route/Run/Trip", "Runs_Trips")]:
            sub = f[f["Level"] == level]
            if not sub.empty:
                sub.to_excel(writer, sheet_name=name[:31], index=False)
        if not result["route_reassignments"].empty:
            result["route_reassignments"].to_excel(writer, sheet_name="Possible_Data_Edits", index=False)
        if not result["run_trip"].empty:
            result["run_trip"].to_excel(writer, sheet_name="Run_Trip_Detail", index=False)
        if diagnostics is not None and not diagnostics.empty:
            diagnostics.to_excel(writer, sheet_name="Diagnostics", index=False)

        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col_cells in ws.columns:
                max_len = 0
                for cell in col_cells[:200]:
                    v = "" if cell.value is None else str(cell.value)
                    max_len = max(max_len, min(len(v), 60))
                ws.column_dimensions[col_cells[0].column_letter].width = max(10, min(max_len + 2, 45))
    return output.getvalue()
