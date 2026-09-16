from __future__ import annotations

import io
import re
import pandas as pd
import streamlit as st

from parsers import (
    parse_gfl_ridership,
    parse_gfl_revenue,
    parse_legacy_routesum,
    parse_legacy_transaction_detail,
)
from reconciliation import reconcile
from exports import to_csv_bytes, to_excel_bytes

st.set_page_config(page_title="Genfare Report Reconciler", page_icon="🚌", layout="wide")

st.title("Genfare Report Reconciler")
st.caption("Compare GenfareLink raw data against Legacy reports, drill down to keys/TTPs/routes/runs/trips, and flag likely causes of discrepancies.")

with st.expander("Preferred reports — read this first", expanded=True):
    st.markdown("""
**GenfareLink (GFL) — upload both CSVs**

1. **Ridership Raw Data CSV — required.** Preferred fields include `Ridership`, `Key/TTP`, `Route`, `Run`, `Trip`, and `Amount Charged`.
2. **Revenue Raw Data CSV — required for full validation.** Preferred fields include `Revenue`, `Route`, `Run`, and `Trip`.

**Important:** In the sample GFL files used to build this tool, the Ridership Raw Data file *already contains* `Amount Charged`, and its total and route/run/trip revenue exactly matched the separate Revenue Raw Data file. The app still asks for **both** so the revenue export acts as an independent validation check and so future report/filter differences are caught.

**Legacy — CSV strongly preferred**

1. **EVENT SUMMARY (ROUTESUM) — required.** CSV is preferred. `BY ROUTE` is best for route-level reconciliation; `BY ROUTE-DATE` is also accepted. PDF is accepted as a fallback, but detailed extraction is less reliable.
2. **TRANSACTION DETAIL REPORT — strongly recommended.** CSV is preferred. This is what enables key/TTP, bus, route, run, trip, and likely Data Edit/reassignment analysis.

Reports with no data are okay. The app will label them rather than silently treating an unreadable report as a valid zero.
""")

st.subheader("1. Upload reports")
c1, c2 = st.columns(2)
with c1:
    st.markdown("#### GenfareLink")
    gfl_rid_file = st.file_uploader("GFL Ridership Raw Data (.csv)", type=["csv"], key="gflrid")
    gfl_rev_file = st.file_uploader("GFL Revenue Raw Data (.csv)", type=["csv"], key="gflrev")
with c2:
    st.markdown("#### Legacy")
    legacy_routesum_file = st.file_uploader("Legacy EVENT SUMMARY / ROUTESUM (.csv preferred, .pdf accepted)", type=["csv", "pdf"], key="routesum")
    legacy_tx_file = st.file_uploader("Legacy TRANSACTION DETAIL (.csv strongly recommended)", type=["csv"], key="legacytx")

st.subheader("2. Ridership business rules")
profile = st.selectbox("Agency profile", ["COLTS - current test rules", "Custom"], index=0)
if profile == "COLTS - current test rules":
    default_keys = "2, 3, 4"
    default_ttps = "48"
    st.caption("COLTS profile: Keys 2, 3, and 4 do not count as ridership. Keys 8 and D do count. TTP48/CHANGE does not count as ridership.")
else:
    default_keys = ""
    default_ttps = ""

r1, r2 = st.columns(2)
with r1:
    nonrid_keys_text = st.text_input("Keys that should NOT count as ridership", value=default_keys, help="Comma-separated, e.g. 2, 3, 4")
with r2:
    nonrid_ttps_text = st.text_input("TTPs that should NOT count as ridership", value=default_ttps, help="Comma-separated, e.g. 48")

def parse_set(s):
    return {x.strip().upper().replace("KEY", "").replace("TTP", "").strip() for x in re.split(r"[,;\s]+", s) if x.strip()}

run = st.button("Compare reports", type="primary", use_container_width=True)

if run:
    missing = []
    if not gfl_rid_file: missing.append("GFL Ridership Raw Data")
    if not gfl_rev_file: missing.append("GFL Revenue Raw Data")
    if not legacy_routesum_file: missing.append("Legacy ROUTESUM")
    if missing:
        st.error("Please upload: " + ", ".join(missing))
        st.stop()

    diagnostics = []
    try:
        with st.spinner("Recognizing and parsing reports..."):
            gfl_rid = parse_gfl_ridership(gfl_rid_file)
            gfl_rev = parse_gfl_revenue(gfl_rev_file)
            legacy_routesum = parse_legacy_routesum(legacy_routesum_file, legacy_routesum_file.name)
            legacy_tx = parse_legacy_transaction_detail(legacy_tx_file) if legacy_tx_file else None

        for label, p in [
            ("GFL Ridership", gfl_rid), ("GFL Revenue", gfl_rev), ("Legacy ROUTESUM", legacy_routesum),
            ("Legacy Transaction Detail", legacy_tx),
        ]:
            if p is None:
                diagnostics.append({"Report": label, "Detected As": "Not uploaded", "Confidence": "—", "Warnings": "Key/TTP and run/trip cause analysis will be limited."})
            else:
                diagnostics.append({"Report": label, "Detected As": p.kind, "Confidence": f"{p.confidence}%", "Warnings": " | ".join(p.warnings) if p.warnings else "None"})
        diagnostics_df = pd.DataFrame(diagnostics)

        nonrid_keys = parse_set(nonrid_keys_text)
        nonrid_ttps = parse_set(nonrid_ttps_text)
        result = reconcile(gfl_rid, gfl_rev, legacy_routesum, legacy_tx,
                           non_ridership_keys=nonrid_keys, non_ridership_ttps=nonrid_ttps)

        st.success("Reports parsed and compared.")
        s = result["summary"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Legacy Ridership", f"{s['legacy_ridership']:,.0f}")
        m2.metric("GFL Raw Ridership", f"{s['gfl_raw_ridership']:,.0f}", delta=f"{s['gfl_raw_ridership']-s['legacy_ridership']:+,.0f} vs Legacy")
        m3.metric("GFL Adjusted Ridership", f"{s['gfl_adjusted_ridership']:,.0f}", delta=f"{s['gfl_adjusted_ridership']-s['legacy_ridership']:+,.0f} vs Legacy")
        m4.metric("Revenue Difference", f"${s['gfl_revenue']-s['legacy_revenue']:+,.2f}")

        rev_cross = s['gfl_revenue'] - s['gfl_ridership_amount_charged']
        if abs(rev_cross) < 0.01:
            st.info(f"GFL internal revenue cross-check: Revenue Raw Data and Ridership Raw Data `Amount Charged` match (${s['gfl_revenue']:,.2f}).")
        else:
            st.warning(f"GFL internal revenue cross-check differs by ${rev_cross:+,.2f}. Check report filters/date ranges before comparing to Legacy.")

        tabs = st.tabs(["All Findings", "Unresolved", "Keys", "TTPs", "Routes", "Possible Data Edits", "Run/Trip", "Diagnostics"])
        f = result["findings"].copy()
        with tabs[0]:
            st.dataframe(f, use_container_width=True, hide_index=True)
        with tabs[1]:
            u = f[(f["Difference"].abs() > 1e-9) & (~f["Likely Cause"].isin(["Matched", "Matched total revenue", "Independent GFL exports reconcile"]))]
            st.dataframe(u, use_container_width=True, hide_index=True)
        with tabs[2]:
            st.dataframe(f[f["Level"] == "Key"], use_container_width=True, hide_index=True)
        with tabs[3]:
            st.dataframe(f[f["Level"] == "TTP"], use_container_width=True, hide_index=True)
        with tabs[4]:
            st.dataframe(f[f["Level"] == "Route"], use_container_width=True, hide_index=True)
        with tabs[5]:
            if result["route_reassignments"].empty:
                st.info("No exact route-reassignment patterns were detected with the available transaction-level evidence.")
            else:
                st.dataframe(result["route_reassignments"], use_container_width=True, hide_index=True)
        with tabs[6]:
            if legacy_tx is None:
                st.info("Upload Legacy Transaction Detail CSV to enable run/trip analysis.")
            else:
                rt = result["run_trip"]
                st.dataframe(rt[rt["difference"].abs() > 1e-9] if not rt.empty else rt, use_container_width=True, hide_index=True)
        with tabs[7]:
            st.dataframe(diagnostics_df, use_container_width=True, hide_index=True)
            st.caption("A low-confidence or warning result means the app recognized the report but recommends reviewing the source/filter assumptions.")

        st.subheader("3. Download reconciliation")
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("Download reconciliation CSV", data=to_csv_bytes(f), file_name="reconciliation_summary.csv", mime="text/csv", use_container_width=True)
        with d2:
            st.download_button("Download full Excel workbook", data=to_excel_bytes(result, diagnostics_df), file_name="reconciliation_full.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

    except Exception as e:
        st.exception(e)
        st.info("If a report was exported in an unfamiliar layout, keep the file and use the Diagnostics/error text to add that layout as another recognized template.")
