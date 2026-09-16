from __future__ import annotations

import os
import time
import pandas as pd
import streamlit as st

from common import save_uploaded_file
from exports import to_csv_bytes, to_excel_bytes
from large_csv import profile_gfl_revenue, profile_gfl_ridership, profile_legacy_transaction_detail
from legacy_routesum import parse_routesum
from reconciliation import reconcile

st.set_page_config(page_title="Genfare Report Reconciler V2", page_icon="🚌", layout="wide")

st.title("Genfare Report Reconciler V2")
st.caption("Large-file reconciliation for GenfareLink vs Legacy: ridership, revenue, keys/TTPs, routes, runs/trips, and likely causes — without manually entering ridership rules.")

with st.expander("Preferred reports — upload guidance", expanded=True):
    st.markdown("""
### Required
**1. GenfareLink — Ridership Raw Data CSV**  
This is the main GFL input. In the sample exports used to build V2, it contains `Ridership`, `Amount Charged`, `Key/TTP`, `Route`, `Run`, and `Trip`, so it is sufficient for both ridership and normal revenue comparison.

**2. Legacy — EVENT SUMMARY (ROUTESUM)**  
**PDF is preferred for V2** because the PDF preserves the Key Count, key-ridership display, and TTP headings needed for automatic ridership-rule inference. CSV is accepted for summary totals, but some Legacy CSV exports omit useful TTP/key labels.

### Strongly recommended for full drill-down
**3. Legacy — TRANSACTION DETAIL CSV**  
Use CSV. This adds transaction-level evidence for route/run/trip mismatches and strengthens possible Data Edit / route reassignment explanations.

### Optional
**4. GenfareLink — Revenue Raw Data CSV**  
Not required. V2 normally uses `Amount Charged` from the GFL Ridership Raw Data file. Upload Revenue Raw Data only when you want an independent GFL-vs-GFL revenue cross-check.

**No manual ridership-rule fields are used.** V2 infers Legacy behavior from the relationship between ROUTESUM route totals, presets, TTP counts, and key counts. It can also flag cases where the Legacy detailed key-ridership section appears to show 0 while overall/route ridership proves the key is actually contributing riders.
""")

st.subheader("1. Upload reports")
left, right = st.columns(2)
with left:
    st.markdown("#### GenfareLink")
    gfl_rid_file = st.file_uploader("GFL Ridership Raw Data — required (.csv)", type=["csv"], key="gflrid")
    gfl_rev_file = st.file_uploader("GFL Revenue Raw Data — optional (.csv)", type=["csv"], key="gflrev")
with right:
    st.markdown("#### Legacy")
    legacy_routesum_file = st.file_uploader("Legacy EVENT SUMMARY / ROUTESUM — required (.pdf preferred, .csv accepted)", type=["pdf", "csv"], key="routesum")
    legacy_tx_file = st.file_uploader("Legacy TRANSACTION DETAIL — strongly recommended (.csv)", type=["csv"], key="legacytx")

with st.expander("How V2 determines ridership-rule differences"):
    st.markdown("""
V2 does **not** assume a list of keys/TTPs that should or should not count ridership. It attempts to reconstruct Legacy route ridership from:

`Preset + selected Key counts + selected TTP counts = Legacy route ridership`

A binary optimization chooses the rider/non-rider behavior that best fits every Legacy route simultaneously. The detailed Legacy key-ridership page is used as evidence, but it is **not blindly trusted**. If that page says a key contributes 0 while the route and total ridership can only reconcile when that key counts, V2 flags the Legacy internal display inconsistency separately.
""")

run = st.button("Compare reports", type="primary", use_container_width=True)

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
        with st.spinner("Scanning and aggregating reports with the large-file engine..."):
            gfl_path = save_uploaded_file(gfl_rid_file); temp_paths.append(gfl_path)
            gfl = profile_gfl_ridership(gfl_path)

            routesum = parse_routesum(legacy_routesum_file, legacy_routesum_file.name)

            legacy_tx = None
            if legacy_tx_file:
                ltx_path = save_uploaded_file(legacy_tx_file); temp_paths.append(ltx_path)
                legacy_tx = profile_legacy_transaction_detail(ltx_path)

            gfl_rev = None
            if gfl_rev_file:
                grev_path = save_uploaded_file(gfl_rev_file); temp_paths.append(grev_path)
                gfl_rev = profile_gfl_revenue(grev_path)

            result = reconcile(gfl, routesum, legacy_tx, gfl_rev)

        elapsed = time.perf_counter() - start
        for label, obj in [
            ("GFL Ridership", gfl), ("Legacy ROUTESUM", routesum),
            ("Legacy Transaction Detail", legacy_tx), ("GFL Revenue", gfl_rev),
        ]:
            if obj is None:
                diagnostics.append({"Report": label, "Detected As": "Not uploaded", "Confidence": "—", "Warnings": "Optional/limited drill-down"})
            else:
                diagnostics.append({
                    "Report": label, "Detected As": obj.kind, "Confidence": f"{obj.confidence}%",
                    "Warnings": " | ".join(getattr(obj, "warnings", []) or []) or "None"
                })
        diagnostics_df = pd.DataFrame(diagnostics)

        st.success(f"Comparison completed in {elapsed:,.1f} seconds.")
        s = result["summary"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Legacy Ridership", f"{s['legacy_ridership']:,.0f}")
        m2.metric("GFL Raw Ridership", f"{s['gfl_raw_ridership']:,.0f}", delta=f"{s['gfl_raw_ridership']-s['legacy_ridership']:+,.0f} vs Legacy")
        m3.metric("GFL using inferred Legacy rules", f"{s['gfl_legacy_rule_normalized_ridership']:,.0f}", delta=f"{s['gfl_legacy_rule_normalized_ridership']-s['legacy_ridership']:+,.0f} vs Legacy")
        m4.metric("Revenue Difference", f"${s['gfl_revenue']-s['legacy_revenue']:+,.2f}")

        st.caption(
            f"Processed {s['gfl_rows']:,} GFL rows" +
            (f" and {s['legacy_transaction_rows']:,} Legacy transaction rows" if s['legacy_transaction_rows'] else "") +
            f". Rule inference confidence: {s['rule_confidence']}% ({'exact route fit' if s['rule_exact_fit'] else 'non-exact fit'})."
        )

        if gfl_rev is None:
            st.info("GFL Revenue Raw Data was not uploaded. Revenue comparison is using `Amount Charged` from the GFL Ridership Raw Data file, as intended in V2.")
        else:
            internal_diff = float(gfl_rev.totals.get("revenue", 0)) - s["gfl_amount_charged"]
            if abs(internal_diff) < 0.01:
                st.info("Optional GFL revenue cross-check passed: Revenue Raw Data matches Ridership Raw Data `Amount Charged`.")
            else:
                st.warning(f"Optional GFL revenue cross-check differs by ${internal_diff:+,.2f}. Check filters/date ranges.")

        inf = result["rule_inference"]
        if inf.warnings:
            for warning in inf.warnings:
                st.warning(warning)

        tabs = st.tabs(["Unresolved", "All Findings", "Inferred Rules", "Legacy Internal", "Keys", "TTPs", "Routes", "Possible Data Edits", "Run/Trip", "Diagnostics"])
        f = result["findings"]
        with tabs[0]:
            unresolved = f[(f["Status"] == "Unresolved") | (f["Severity"] == "Error")]
            st.dataframe(unresolved, use_container_width=True, hide_index=True)
        with tabs[1]:
            st.dataframe(f, use_container_width=True, hide_index=True)
        with tabs[2]:
            if inf.rules.empty:
                st.info("Automatic rule inference was unavailable for these report formats.")
            else:
                st.dataframe(inf.rules, use_container_width=True, hide_index=True)
        with tabs[3]:
            st.dataframe(f[f["Level"] == "Legacy Internal"], use_container_width=True, hide_index=True)
            if not inf.route_fit.empty:
                st.markdown("**Route-level Legacy ridership reconstruction**")
                st.dataframe(inf.route_fit, use_container_width=True, hide_index=True)
        with tabs[4]:
            st.dataframe(f[f["Level"] == "Key"], use_container_width=True, hide_index=True)
        with tabs[5]:
            st.dataframe(f[f["Level"] == "TTP"], use_container_width=True, hide_index=True)
        with tabs[6]:
            st.dataframe(f[f["Level"] == "Route"], use_container_width=True, hide_index=True)
        with tabs[7]:
            if result["route_reassignments"].empty:
                st.info("No exact route-offset patterns were detected.")
            else:
                st.dataframe(result["route_reassignments"], use_container_width=True, hide_index=True)
        with tabs[8]:
            if legacy_tx is None:
                st.info("Upload Legacy Transaction Detail CSV to enable run/trip-level derived comparison and stronger Data Edit evidence.")
            else:
                rt = result["run_trip"]
                st.dataframe(rt[rt["difference"].abs() > 1e-9] if not rt.empty else rt, use_container_width=True, hide_index=True)
        with tabs[9]:
            st.dataframe(diagnostics_df, use_container_width=True, hide_index=True)

        st.subheader("2. Download reconciliation")
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("Download reconciliation CSV", data=to_csv_bytes(f), file_name="reconciliation_summary_v2.csv", mime="text/csv", use_container_width=True)
        with d2:
            st.download_button("Download full Excel workbook", data=to_excel_bytes(result, diagnostics_df), file_name="reconciliation_full_v2.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    except Exception as exc:
        st.exception(exc)
        st.info("Keep the source report if this is a new layout. V2 is designed so additional Legacy/GFL templates can be added without changing the reconciliation engine.")
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except Exception:
                pass
