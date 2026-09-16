from __future__ import annotations

import os
import time
import pandas as pd
import streamlit as st

from common import save_uploaded_file
from exports import to_ai_json_bytes, to_ai_prompt_bytes, to_csv_bytes, to_excel_bytes
from large_csv import profile_gfl_ridership, profile_legacy_transaction_detail
from legacy_routesum import parse_routesum
from reconciliation import reconcile

st.set_page_config(page_title="Genfare Ridership Reconciler V4", page_icon="🚌", layout="wide")

st.title("Genfare Ridership Reconciler V4")
st.caption("Ridership-only reconciliation with progressive drill-down: overall → day → fare category → route/run/bus → transaction match.")

with st.expander("Preferred reports — upload guidance", expanded=True):
    st.markdown("""
**Required**

- **GenfareLink Ridership Raw Data CSV** — raw GFL ridership export with Transaction Time, Ridership, Key/TTP, Route, Run, Bus, and Trip when available.
- **Legacy EVENT SUMMARY / ROUTESUM** — PDF preferred because it preserves route totals, Key counts, TTP counts, and the Legacy key-ridership display used for automatic ridership-behavior inference.

**Strongly recommended for the full drill-down**

- **Legacy TRANSACTION DETAIL CSV** — enables daily Key/TTP comparison, route/run/bus localization, equal-and-opposite movement detection, and targeted transaction matching.

**Scope:** ridership only. Revenue is intentionally excluded.
""")

st.subheader("Upload reports")
left, right = st.columns(2)
with left:
    gfl_rid_file = st.file_uploader("GFL Ridership Raw Data — required (.csv)", type=["csv"], key="gflrid")
with right:
    legacy_routesum_file = st.file_uploader("Legacy ROUTESUM — required (.pdf preferred, .csv accepted)", type=["pdf", "csv"], key="routesum")
    legacy_tx_file = st.file_uploader("Legacy Transaction Detail — strongly recommended (.csv)", type=["csv"], key="legacytx")

run = st.button("Reconcile ridership", type="primary", use_container_width=True)


def _problem_only(df: pd.DataFrame, diff_col: str = "Record Difference") -> pd.DataFrame:
    if df is None or df.empty or diff_col not in df.columns:
        return df if df is not None else pd.DataFrame()
    return df[pd.to_numeric(df[diff_col], errors="coerce").fillna(0).abs() > 1e-9]


def _show_summary_box(title: str, cause: str, evidence: str, kind: str = "info"):
    text = f"**{title}**  \n**Likely cause:** {cause}  \n**Evidence:** {evidence}"
    getattr(st, kind)(text)


if run:
    missing = []
    if not gfl_rid_file:
        missing.append("GFL Ridership Raw Data")
    if not legacy_routesum_file:
        missing.append("Legacy ROUTESUM")
    if missing:
        st.error("Please upload: " + ", ".join(missing))
        st.stop()

    temp_paths = []
    diagnostics = []
    start = time.perf_counter()
    try:
        with st.spinner("Scanning reports and building drill-down evidence..."):
            gfl_path = save_uploaded_file(gfl_rid_file)
            temp_paths.append(gfl_path)
            gfl = profile_gfl_ridership(gfl_path)
            routesum = parse_routesum(legacy_routesum_file, legacy_routesum_file.name)

            legacy_tx = None
            if legacy_tx_file:
                ltx_path = save_uploaded_file(legacy_tx_file)
                temp_paths.append(ltx_path)
                legacy_tx = profile_legacy_transaction_detail(ltx_path)

            result = reconcile(gfl, routesum, legacy_tx)

        elapsed = time.perf_counter() - start
        for label, obj, filename in [
            ("GFL Ridership", gfl, gfl_rid_file.name),
            ("Legacy ROUTESUM", routesum, legacy_routesum_file.name),
            ("Legacy Transaction Detail", legacy_tx, legacy_tx_file.name if legacy_tx_file else "Not uploaded"),
        ]:
            if obj is None:
                diagnostics.append({"Report": label, "File": filename, "Detected As": "Not uploaded", "Confidence": "—", "Warnings": "Full drill-down unavailable"})
            else:
                diagnostics.append({
                    "Report": label,
                    "File": filename,
                    "Detected As": obj.kind,
                    "Confidence": f"{obj.confidence}%",
                    "Warnings": " | ".join(getattr(obj, "warnings", []) or []) or "None",
                })
        diagnostics_df = pd.DataFrame(diagnostics)
        drill = result["drilldowns"]
        s = result["summary"]

        st.success(f"Processed {s['gfl_rows']:,} GFL rows" + (f" and {s['legacy_transaction_rows']:,} Legacy transaction rows" if s['legacy_transaction_rows'] else "") + f" in {elapsed:,.1f} seconds.")
        for note in drill.get("notes", []):
            st.warning(note)

        # ------------------------------------------------------------------
        st.header("1. Raw ridership difference")
        raw_diff = s["gfl_raw_ridership"] - s["legacy_ridership"]
        norm_diff = s["gfl_legacy_rule_normalized_ridership"] - s["legacy_ridership"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Legacy ridership", f"{s['legacy_ridership']:,.0f}")
        c2.metric("GFL raw ridership", f"{s['gfl_raw_ridership']:,.0f}", delta=f"{raw_diff:+,.0f}")
        c3.metric("Actual raw difference", f"{raw_diff:+,.0f}")
        c4.metric("Difference after inferred Legacy behavior", f"{norm_diff:+,.0f}")

        if abs(raw_diff) < 1e-9:
            _show_summary_box("Overall totals match", "No systemwide raw ridership discrepancy.", "Legacy ROUTESUM total equals the raw GFL Ridership sum.", "success")
        elif abs(norm_diff) < 1e-9:
            _show_summary_box("Raw totals differ, but inferred ridership behavior explains the full gap", "Key/TTP ridership-rule/configuration behavior.", f"Raw gap is {raw_diff:+,.0f}; automatic Legacy behavior normalization accounts for {s['inferred_rule_adjustment']:,.0f} GFL riders and leaves 0 unexplained.", "info")
        else:
            _show_summary_box("Systemwide ridership remains different", "At least part of the discrepancy is not explained by inferred fare-category behavior.", f"Raw difference={raw_diff:+,.0f}; after inferred Legacy behavior the remaining difference is {norm_diff:+,.0f}. Continue to the day drill-down.", "warning")

        # ------------------------------------------------------------------
        st.header("2. Drill down by day")
        day = drill.get("day_comparison", pd.DataFrame())
        selected_day = None
        if day.empty:
            st.info("Daily reconciliation is unavailable. Upload Legacy Transaction Detail or a ROUTESUM by route-date report.")
        else:
            display_day = day.copy()
            st.dataframe(display_day, use_container_width=True, hide_index=True)
            problem_days = display_day[display_day["Normalized Difference"].abs() > 1e-9]["Date"].astype(str).tolist()
            all_days = display_day["Date"].astype(str).tolist()
            default_days = problem_days or all_days
            selected_day = st.selectbox("Day to investigate", options=all_days, index=all_days.index(default_days[0]) if default_days else 0)
            drow = display_day[display_day["Date"].astype(str) == str(selected_day)].iloc[0]
            if abs(float(drow["Normalized Difference"])) < 1e-9:
                _show_summary_box(f"{selected_day}: reconciled after category behavior", "No unexplained daily ridership gap remains.", f"Raw difference={drow['Raw Difference']:+g}; normalized difference={drow['Normalized Difference']:+g}. Legacy source: {drow['Legacy Source']}", "success")
            else:
                _show_summary_box(f"{selected_day}: daily discrepancy remains", "A Key/TTP count, route allocation, or unmatched transaction needs investigation.", f"Raw difference={drow['Raw Difference']:+g}; normalized difference={drow['Normalized Difference']:+g}. Legacy source: {drow['Legacy Source']}", "warning")

        # ------------------------------------------------------------------
        st.header("3. Key/TTP counts across the whole system by day")
        day_ident = drill.get("day_identifier", pd.DataFrame())
        selected_identifier = None
        if day_ident.empty:
            st.info("Upload Legacy Transaction Detail to compare Key/TTP counts by day.")
        else:
            current = day_ident if selected_day is None else day_ident[day_ident["Date"].astype(str) == str(selected_day)]
            problems = current[(pd.to_numeric(current["Rider Difference"], errors="coerce").fillna(0).abs() > 1e-9) | current["Likely Cause"].str.contains("inconsistency|unresolved", case=False, na=False)]
            tab_problem, tab_keys, tab_ttps, tab_all = st.tabs(["Problems", "Keys", "TTPs", "All categories"])
            with tab_problem:
                if problems.empty:
                    st.success("No Key/TTP-level problems for this day.")
                else:
                    st.dataframe(problems, use_container_width=True, hide_index=True)
            with tab_keys:
                st.dataframe(current[current["Type"] == "Key"], use_container_width=True, hide_index=True)
            with tab_ttps:
                st.dataframe(current[current["Type"] == "TTP"], use_container_width=True, hide_index=True)
            with tab_all:
                st.dataframe(current, use_container_width=True, hide_index=True)

            candidates = problems["Identifier"].astype(str).tolist() if not problems.empty else current["Identifier"].astype(str).tolist()
            candidates = list(dict.fromkeys(candidates))
            if candidates:
                selected_identifier = st.selectbox("Fare category to trace into routes/runs/buses", options=candidates)
                crow = current[current["Identifier"].astype(str) == selected_identifier]
                if not crow.empty:
                    r = crow.iloc[0]
                    _show_summary_box(
                        f"{selected_day or r['Date']} — {selected_identifier}",
                        str(r["Likely Cause"]),
                        str(r["Evidence"]),
                        "warning" if str(r["Likely Cause"]) != "Match" else "success",
                    )

        # ------------------------------------------------------------------
        st.header("4. Locate the difference: route → run → bus")
        route_cmp = drill.get("route_comparison_by_day", pd.DataFrame())
        run_cmp = drill.get("run_comparison_by_day", pd.DataFrame())
        bus_cmp = drill.get("bus_comparison_by_day", pd.DataFrame())
        exact_cmp = drill.get("route_run_bus_by_day", pd.DataFrame())
        movements = drill.get("equal_opposite_movements", pd.DataFrame())

        def filt(df):
            if df is None or df.empty:
                return pd.DataFrame()
            x = df.copy()
            if selected_day is not None and "Date" in x.columns:
                x = x[x["Date"].astype(str) == str(selected_day)]
            if selected_identifier is not None and "Identifier" in x.columns:
                x = x[x["Identifier"].astype(str) == str(selected_identifier)]
            return x

        rtab, runtab, bustab, exacttab, movetab = st.tabs(["Routes", "Runs", "Buses", "Route + Run + Bus", "Equal & opposite movements"])
        with rtab:
            x = filt(route_cmp)
            st.dataframe(_problem_only(x), use_container_width=True, hide_index=True)
        with runtab:
            x = filt(run_cmp)
            st.dataframe(_problem_only(x), use_container_width=True, hide_index=True)
        with bustab:
            x = filt(bus_cmp)
            st.dataframe(_problem_only(x), use_container_width=True, hide_index=True)
        with exacttab:
            x = filt(exact_cmp)
            st.dataframe(_problem_only(x), use_container_width=True, hide_index=True)
        with movetab:
            x = filt(movements)
            if x.empty:
                st.info("No exact equal-and-opposite route/run/bus movement was detected for the current selection.")
            else:
                st.dataframe(x, use_container_width=True, hide_index=True)
                for _, r in x.head(5).iterrows():
                    st.info(f"**{r['Likely Cause']}** — {r['Evidence']} (evidence score {int(r['Confidence %'])}%)")

        # ------------------------------------------------------------------
        st.header("5. Transaction-match remaining discrepancies")
        txm = drill.get("transaction_matches", pd.DataFrame())
        if legacy_tx is None:
            st.info("Upload Legacy Transaction Detail to enable targeted transaction matching.")
        elif txm is None or txm.empty:
            st.success("No remaining transaction-signature discrepancies were found among the day/category combinations selected by the deterministic engine.")
        else:
            txview = txm.copy()
            if selected_day is not None:
                txview = txview[txview["Date"].astype(str) == str(selected_day)]
            if selected_identifier is not None:
                txview = txview[txview["Identifier"].astype(str) == str(selected_identifier)]
            if txview.empty:
                st.success("No remaining transaction mismatch for the current day/category selection.")
            else:
                st.dataframe(txview, use_container_width=True, hide_index=True)
                top = txview.iloc[0]
                _show_summary_box("Most specific remaining issue", str(top["Likely Cause"]), str(top["Evidence"]), "warning")

        # ------------------------------------------------------------------
        st.header("6. Export the reconciliation")
        f = result["findings"]
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("Download summary CSV", data=to_csv_bytes(f), file_name="ridership_reconciliation_summary_v4.csv", mime="text/csv", use_container_width=True)
        with d2:
            st.download_button("Download full drill-down Excel", data=to_excel_bytes(result, diagnostics_df), file_name="ridership_reconciliation_full_v4.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

        st.subheader("Export for external AI investigation")
        st.caption("The app does not contain an AI agent. This export packages the deterministic drill-down findings so an external AI can explain patterns and suggest next checks.")
        a1, a2 = st.columns(2)
        with a1:
            st.download_button("Download AI evidence JSON", data=to_ai_json_bytes(result, diagnostics_df), file_name="ridership_ai_evidence_v4.json", mime="application/json", use_container_width=True)
        with a2:
            st.download_button("Download AI review prompt", data=to_ai_prompt_bytes(), file_name="ridership_ai_review_prompt_v4.md", mime="text/markdown", use_container_width=True)

        with st.expander("Diagnostics"):
            st.dataframe(diagnostics_df, use_container_width=True, hide_index=True)
            st.markdown(f"Legacy behavior inference confidence: **{s['rule_confidence']}%**; exact route fit: **{s['rule_exact_fit']}**.")

    except Exception as exc:
        st.exception(exc)
        st.info("Keep the source report if this is a new layout. V4 is structured so new Legacy/GFL templates can be added without changing the drill-down workflow.")
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except Exception:
                pass
