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

st.set_page_config(page_title="Genfare Ridership Reconciler", page_icon="🚌", layout="wide")

st.title("Genfare Ridership Reconciler")
st.caption("Ridership-only reconciliation. Start with the total, then open only the day you want to investigate.")

with st.expander("Upload guidance", expanded=False):
    st.markdown(
        """
**Required**

- **GenfareLink Ridership Raw Data CSV** — GFL ridership export with Transaction Time, Ridership, Key/TTP, Route, Run, Bus, and Trip when available.
- **Legacy EVENT SUMMARY / ROUTESUM** — PDF preferred because it preserves route totals, Key counts, TTP counts, and the Legacy key-ridership display.

**Strongly recommended**

- **Legacy TRANSACTION DETAIL CSV** — enables daily reconstruction, fare-category comparison, route/run/bus localization, equal-and-opposite movement detection, and targeted transaction matching.

**Scope:** ridership only. Revenue is intentionally excluded.
"""
    )

left, right = st.columns(2)
with left:
    gfl_rid_file = st.file_uploader("GFL Ridership Raw Data — required", type=["csv"], key="gflrid")
with right:
    legacy_routesum_file = st.file_uploader("Legacy ROUTESUM — required", type=["pdf", "csv"], key="routesum")
    legacy_tx_file = st.file_uploader("Legacy Transaction Detail — recommended", type=["csv"], key="legacytx")

run = st.button("Reconcile ridership", type="primary", use_container_width=True)


def _problem_only(df: pd.DataFrame, diff_col: str = "Record Difference") -> pd.DataFrame:
    if df is None or df.empty or diff_col not in df.columns:
        return df if df is not None else pd.DataFrame()
    return df[pd.to_numeric(df[diff_col], errors="coerce").fillna(0).abs() > 1e-9]


def _summary_box(title: str, cause: str, evidence: str, kind: str = "info"):
    text = f"**{title}**  \n**Likely cause:** {cause}  \n**Evidence:** {evidence}"
    getattr(st, kind)(text)


def _fmt_num(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):,.0f}"


def _fmt_diff(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):+,.0f}"


def _key_ttp_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    wanted = [
        ("Category", "Type", "Type"),
        ("Category", "Identifier", "Identifier"),
        ("Legacy", "Fare-use events", "Legacy Events"),
        ("Legacy", "Expected riders", "Legacy Expected Riders"),
        ("Legacy", "Counts ridership", "Legacy Counts Ridership"),
        ("GenfareLink", "Ridership records", "GFL Ridership Records"),
        ("GenfareLink", "Riders", "GFL Riders"),
        ("GenfareLink", "Riders / record", "GFL Riders/Record"),
        ("Difference", "Records", "Record Difference"),
        ("Difference", "Riders", "Rider Difference"),
        ("Analysis", "Problem", "Problem Summary"),
        ("Analysis", "Likely cause", "Likely Cause"),
        ("Analysis", "Evidence", "Evidence"),
    ]
    data, cols = {}, []
    for group, label, source in wanted:
        if source in df.columns:
            key = (group, label)
            cols.append(key)
            data[key] = df[source].values
    out = pd.DataFrame(data)
    if cols:
        out.columns = pd.MultiIndex.from_tuples(cols)
    return out


def _location_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    wanted = []
    for c in ["Route", "Run", "Bus", "Identifier"]:
        if c in df.columns:
            wanted.append(("Location", c, c))
    wanted += [
        ("Legacy", "Rider events", "Legacy Events"),
        ("GenfareLink", "Ridership records", "GFL Ridership Records"),
        ("GenfareLink", "Riders", "GFL Riders"),
        ("Difference", "Records", "Record Difference"),
    ]
    data, cols = {}, []
    for group, label, source in wanted:
        if source in df.columns:
            key = (group, label)
            cols.append(key)
            data[key] = df[source].values
    out = pd.DataFrame(data)
    if cols:
        out.columns = pd.MultiIndex.from_tuples(cols)
    return out


def _movement_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    wanted = [
        ("Category", "Identifier", "Identifier"),
        ("Movement", "Level", "Level"),
        ("Movement", "Legacy location", "Legacy Location"),
        ("Movement", "GFL location", "GFL Location"),
        ("Movement", "Count", "Count"),
        ("Analysis", "Likely cause", "Likely Cause"),
        ("Analysis", "Evidence score", "Confidence %"),
        ("Analysis", "Evidence", "Evidence"),
    ]
    data, cols = {}, []
    for group, label, source in wanted:
        if source in df.columns:
            key = (group, label)
            cols.append(key)
            data[key] = df[source].values
    out = pd.DataFrame(data)
    if cols:
        out.columns = pd.MultiIndex.from_tuples(cols)
    return out


def _tx_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    wanted = [
        ("Match", "Timestamp", "Timestamp"),
        ("Match", "Bus", "Bus"),
        ("Match", "Identifier", "Identifier"),
        ("Legacy", "Count", "Legacy Count"),
        ("Legacy", "Assignments", "Legacy Assignments"),
        ("GenfareLink", "Riders", "GFL Riders"),
        ("GenfareLink", "Assignments", "GFL Assignments"),
        ("Difference", "Count", "Count Difference"),
        ("Difference", "Assignment", "Assignment Difference"),
        ("Match", "Exact assignments", "Exact Assignment Matches"),
        ("Analysis", "Likely cause", "Likely Cause"),
        ("Analysis", "Evidence score", "Confidence %"),
        ("Analysis", "Evidence", "Evidence"),
    ]
    data, cols = {}, []
    for group, label, source in wanted:
        if source in df.columns:
            key = (group, label)
            cols.append(key)
            data[key] = df[source].values
    out = pd.DataFrame(data)
    if cols:
        out.columns = pd.MultiIndex.from_tuples(cols)
    return out



@st.fragment
def _render_day_drilldown(
    day: pd.DataFrame,
    day_ident: pd.DataFrame,
    route_cmp: pd.DataFrame,
    run_cmp: pd.DataFrame,
    bus_cmp: pd.DataFrame,
    exact_cmp: pd.DataFrame,
    movements: pd.DataFrame,
    txm: pd.DataFrame,
    has_legacy_tx: bool,
):
    """Interactive drill-down that reruns independently from the rest of the app."""
    st.divider()
    st.markdown("## By day")
    st.caption("Dates are stacked vertically. Open a day only when you want the deeper Key/TTP → route/run/bus → transaction investigation.")

    if day is None or day.empty:
        st.info("Daily reconciliation is unavailable. Upload Legacy Transaction Detail or a ROUTESUM by route-date report.")
        return

    for _, drow in day.iterrows():
        date = str(drow["Date"])
        legacy_value = drow.get("Legacy Ridership")
        gfl_raw = drow.get("GFL Raw Ridership")
        gfl_norm = drow.get("GFL Normalized Ridership")
        raw_day_diff = drow.get("Raw Difference")
        norm_day_diff = drow.get("Normalized Difference")
        authoritative = bool(drow.get("Legacy Daily Authoritative", False))
        source = str(drow.get("Legacy Source", ""))
        status = str(drow.get("Status", ""))

        with st.container(border=True):
            st.markdown(f"### {date}")
            legacy_col, gfl_col, diff_col = st.columns(3)

            with legacy_col:
                st.markdown("**Legacy**")
                st.metric("Ridership", _fmt_num(legacy_value))
                st.caption(("Authoritative daily total" if authoritative else "Derived daily total") + f" · {source}")

            with gfl_col:
                st.markdown("**GenfareLink**")
                gg1, gg2 = st.columns(2)
                gg1.metric("Raw ridership", _fmt_num(gfl_raw))
                gg2.metric("After Legacy behavior", _fmt_num(gfl_norm))

            with diff_col:
                st.markdown("**Difference**")
                dd1, dd2 = st.columns(2)
                first_label = "Raw" if authoritative else "Derived"
                dd1.metric(first_label, _fmt_diff(raw_day_diff))
                dd2.metric("After behavior", _fmt_diff(norm_day_diff))
                if not authoritative:
                    st.caption("Legacy daily ridership is reconstructed from fare-use transactions; ROUTESUM remains the authoritative period total.")

            if status == "Reconciled after behavior normalization" or (not pd.isna(norm_day_diff) and abs(float(norm_day_diff)) < 1e-9):
                st.success("This day reconciles after fare-category behavior is aligned.")
            elif status == "Raw match":
                st.success("This day matches before any behavior adjustment.")
            elif status == "Legacy daily total unavailable":
                st.info("No comparable Legacy daily ridership total is available for this date.")
            else:
                st.warning("This day still has an unexplained ridership difference. Open the drill-down to locate it.")

            if st.toggle("Open day drill-down", key=f"open_day_{date}"):
                current = day_ident[day_ident["Date"].astype(str) == date].copy() if day_ident is not None and not day_ident.empty else pd.DataFrame()

                st.markdown("#### Fare categories")
                if current.empty:
                    st.info("No day-level Key/TTP comparison is available. Legacy Transaction Detail is required for this view.")
                    selected_identifier = None
                else:
                    rider_diff = pd.to_numeric(current["Rider Difference"], errors="coerce").fillna(0)
                    problems = current[(rider_diff.abs() > 1e-9) | current["Likely Cause"].str.contains("inconsistency|unresolved|difference", case=False, na=False)].copy()

                    ptab, ktab, ttab, atab = st.tabs(["Problems", "Keys", "TTPs", "All categories"])
                    with ptab:
                        if problems.empty:
                            st.success("No fare-category rider problems for this day.")
                        else:
                            st.dataframe(_key_ttp_display(problems), use_container_width=True, hide_index=True)
                    with ktab:
                        st.dataframe(_key_ttp_display(current[current["Type"] == "Key"]), use_container_width=True, hide_index=True)
                    with ttab:
                        st.dataframe(_key_ttp_display(current[current["Type"] == "TTP"]), use_container_width=True, hide_index=True)
                    with atab:
                        st.dataframe(_key_ttp_display(current), use_container_width=True, hide_index=True)

                    candidates = problems["Identifier"].astype(str).tolist() if not problems.empty else current["Identifier"].astype(str).tolist()
                    candidates = list(dict.fromkeys(candidates))
                    selected_identifier = None
                    if candidates:
                        selected_identifier = st.selectbox("Trace one category deeper", candidates, key=f"identifier_{date}")
                        crow = current[current["Identifier"].astype(str) == str(selected_identifier)]
                        if not crow.empty:
                            r = crow.iloc[0]
                            _summary_box(
                                selected_identifier,
                                str(r["Likely Cause"]),
                                str(r["Evidence"]),
                                "success" if str(r["Likely Cause"]) == "Match" else "warning",
                            )

                st.markdown("#### Where it moved")

                def filt(df: pd.DataFrame) -> pd.DataFrame:
                    if df is None or df.empty:
                        return pd.DataFrame()
                    x = df[df["Date"].astype(str) == date].copy() if "Date" in df.columns else df.copy()
                    if selected_identifier is not None and "Identifier" in x.columns:
                        x = x[x["Identifier"].astype(str) == str(selected_identifier)]
                    return x

                rtab, runtab, bustab, movetab, exacttab = st.tabs(["Routes", "Runs", "Buses", "Equal & opposite", "Route + Run + Bus"])
                with rtab:
                    x = _problem_only(filt(route_cmp))
                    if x.empty:
                        st.success("No route-level rider movement for the current selection.")
                    else:
                        st.dataframe(_location_display(x), use_container_width=True, hide_index=True)
                with runtab:
                    x = _problem_only(filt(run_cmp))
                    if x.empty:
                        st.success("No run-level rider movement for the current selection.")
                    else:
                        st.dataframe(_location_display(x), use_container_width=True, hide_index=True)
                with bustab:
                    x = _problem_only(filt(bus_cmp))
                    if x.empty:
                        st.success("No bus-level rider movement for the current selection.")
                    else:
                        st.dataframe(_location_display(x), use_container_width=True, hide_index=True)
                with movetab:
                    x = filt(movements)
                    if x.empty:
                        st.info("No exact equal-and-opposite movement was detected for the current day/category.")
                    else:
                        st.dataframe(_movement_display(x), use_container_width=True, hide_index=True)
                        for _, r in x.head(5).iterrows():
                            st.info(f"**{r['Likely Cause']}** — {r['Evidence']} (evidence score {int(r['Confidence %'])}%)")
                with exacttab:
                    x = _problem_only(filt(exact_cmp))
                    if x.empty:
                        st.success("No Route + Run + Bus rider difference for the current selection.")
                    else:
                        st.dataframe(_location_display(x), use_container_width=True, hide_index=True)

                st.markdown("#### Transaction match")
                if not has_legacy_tx:
                    st.info("Upload Legacy Transaction Detail to enable transaction matching.")
                elif txm is None or txm.empty:
                    st.success("No remaining transaction-signature discrepancy was identified by the targeted matcher.")
                else:
                    txview = txm[txm["Date"].astype(str) == date].copy()
                    if selected_identifier is not None:
                        txview = txview[txview["Identifier"].astype(str) == str(selected_identifier)]
                    if txview.empty:
                        st.success("No remaining transaction mismatch for the current day/category.")
                    else:
                        st.dataframe(_tx_display(txview), use_container_width=True, hide_index=True)
                        top = txview.iloc[0]
                        _summary_box("Most specific remaining issue", str(top["Likely Cause"]), str(top["Evidence"]), "warning")

def _build_payload():
    missing = []
    if not gfl_rid_file:
        missing.append("GFL Ridership Raw Data")
    if not legacy_routesum_file:
        missing.append("Legacy ROUTESUM")
    if missing:
        st.error("Please upload: " + ", ".join(missing))
        return None

    temp_paths = []
    diagnostics = []
    start = time.perf_counter()
    try:
        with st.spinner("Scanning reports and building ridership evidence..."):
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
        return {
            "result": result,
            "diagnostics_df": pd.DataFrame(diagnostics),
            "elapsed": elapsed,
            "has_legacy_tx": legacy_tx is not None,
        }
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except Exception:
                pass


if run:
    try:
        payload = _build_payload()
        if payload is not None:
            st.session_state["reconciliation_payload_v4_2"] = payload
            # Close any day drill-downs from the previous reconciliation.
            for key in list(st.session_state.keys()):
                if str(key).startswith("open_day_") or str(key).startswith("identifier_"):
                    if key != "reconciliation_payload_v4_2":
                        del st.session_state[key]
    except Exception as exc:
        st.exception(exc)

payload = st.session_state.get("reconciliation_payload_v4_2")
if payload is not None:
    result = payload["result"]
    diagnostics_df = payload["diagnostics_df"]
    elapsed = float(payload["elapsed"])
    has_legacy_tx = bool(payload["has_legacy_tx"])
    drill = result["drilldowns"]
    s = result["summary"]

    st.success(
        f"Processed {s['gfl_rows']:,} GFL rows"
        + (f" and {s['legacy_transaction_rows']:,} Legacy transaction rows" if s["legacy_transaction_rows"] else "")
        + f" in {elapsed:,.1f} seconds."
    )
    for note in drill.get("notes", []):
        st.warning(note)

    raw_diff = s["gfl_raw_ridership"] - s["legacy_ridership"]
    norm_diff = s["gfl_legacy_rule_normalized_ridership"] - s["legacy_ridership"]

    overall_legacy, overall_gfl, overall_diff = st.columns(3)
    with overall_legacy:
        st.markdown("#### Legacy")
        st.metric("Ridership", f"{s['legacy_ridership']:,.0f}")
        st.caption("Authoritative total from ROUTESUM")
    with overall_gfl:
        st.markdown("#### GenfareLink")
        g1, g2 = st.columns(2)
        g1.metric("Raw ridership", f"{s['gfl_raw_ridership']:,.0f}")
        g2.metric("After Legacy behavior", f"{s['gfl_legacy_rule_normalized_ridership']:,.0f}")
    with overall_diff:
        st.markdown("#### Difference")
        d1, d2 = st.columns(2)
        d1.metric("Raw", f"{raw_diff:+,.0f}")
        d2.metric("After behavior", f"{norm_diff:+,.0f}")

    if abs(raw_diff) < 1e-9:
        _summary_box("Overall totals match", "No systemwide raw ridership discrepancy.", "Legacy ROUTESUM total equals the raw GFL Ridership sum.", "success")
    elif abs(norm_diff) < 1e-9:
        _summary_box(
            "The raw gap is fully explained",
            "Different Key/TTP ridership behavior between the systems.",
            f"Raw gap={raw_diff:+,.0f}. Applying the automatically inferred Legacy behavior removes {s['inferred_rule_adjustment']:,.0f} GFL riders and leaves 0 unexplained.",
            "info",
        )
    else:
        _summary_box(
            "Ridership still differs after behavior alignment",
            "At least part of the gap is not explained by fare-category behavior.",
            f"Raw gap={raw_diff:+,.0f}; remaining gap={norm_diff:+,.0f}. Open the relevant day below.",
            "warning",
        )

    day = drill.get("day_comparison", pd.DataFrame())
    day_ident = drill.get("day_identifier", pd.DataFrame())
    route_cmp = drill.get("route_comparison_by_day", pd.DataFrame())
    run_cmp = drill.get("run_comparison_by_day", pd.DataFrame())
    bus_cmp = drill.get("bus_comparison_by_day", pd.DataFrame())
    exact_cmp = drill.get("route_run_bus_by_day", pd.DataFrame())
    movements = drill.get("equal_opposite_movements", pd.DataFrame())
    txm = drill.get("transaction_matches", pd.DataFrame())

    _render_day_drilldown(
        day,
        day_ident,
        route_cmp,
        run_cmp,
        bus_cmp,
        exact_cmp,
        movements,
        txm,
        has_legacy_tx,
    )

    st.divider()
    st.markdown("## Downloads")
    f = result["findings"]
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "Download summary CSV",
            data=to_csv_bytes(f),
            file_name="ridership_reconciliation_summary_v4_2.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            "Download full drill-down Excel",
            data=to_excel_bytes(result, diagnostics_df),
            file_name="ridership_reconciliation_full_v4_2.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    st.markdown("### External AI review")
    st.caption("No AI agent runs inside the app. These downloads package the deterministic evidence for an external AI review.")
    a1, a2 = st.columns(2)
    with a1:
        st.download_button(
            "Download AI evidence JSON",
            data=to_ai_json_bytes(result, diagnostics_df),
            file_name="ridership_ai_evidence_v4_2.json",
            mime="application/json",
            use_container_width=True,
        )
    with a2:
        st.download_button(
            "Download AI review prompt",
            data=to_ai_prompt_bytes(),
            file_name="ridership_ai_review_prompt_v4_2.md",
            mime="text/markdown",
            use_container_width=True,
        )

    with st.expander("Diagnostics"):
        st.dataframe(diagnostics_df, use_container_width=True, hide_index=True)
        st.markdown(f"Legacy behavior inference confidence: **{s['rule_confidence']}%**; exact route fit: **{s['rule_exact_fit']}**.")
