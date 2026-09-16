from __future__ import annotations

import os
import time
import pandas as pd
import streamlit as st

from common import save_uploaded_file
from exports import to_ai_json_bytes as rid_to_ai_json_bytes
from exports import to_ai_prompt_bytes as rid_to_ai_prompt_bytes
from exports import to_csv_bytes as rid_to_csv_bytes
from exports import to_excel_bytes as rid_to_excel_bytes
from large_csv import profile_gfl_ridership, profile_legacy_transaction_detail
from legacy_routesum import parse_routesum
from reconciliation import reconcile
from revenue_engine import (
    match_revenue_transactions,
    profile_gfl_revenue,
    profile_legacy_revenue_transactions,
    reconcile_revenue,
)
from revenue_exports import to_ai_json_bytes as rev_to_ai_json_bytes
from revenue_exports import to_ai_prompt_bytes as rev_to_ai_prompt_bytes
from revenue_exports import to_csv_bytes as rev_to_csv_bytes
from revenue_exports import to_excel_bytes as rev_to_excel_bytes


st.set_page_config(page_title="Genfare Report Reconciler", page_icon="🚌", layout="wide")
st.title("Genfare Report Reconciler")
st.caption("Compare GenfareLink against GDS Legacy using a drill-down workflow. Ridership and Revenue are kept separate so each uses the correct source-of-truth rules.")


# ---------- shared formatting ----------

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


def _fmt_money(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${float(v):,.2f}"


def _fmt_money_diff(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):+,.2f}"


def _problem_only(df: pd.DataFrame, diff_col: str = "Record Difference") -> pd.DataFrame:
    if df is None or df.empty or diff_col not in df.columns:
        return df if df is not None else pd.DataFrame()
    return df[pd.to_numeric(df[diff_col], errors="coerce").fillna(0).abs() > 1e-9]


# ---------- ridership compact displays ----------

def _key_ttp_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    wanted = [
        ("Category", "Identifier", "Identifier"),
        ("Legacy", "Events", "Legacy Events"),
        ("Legacy", "Expected riders", "Legacy Expected Riders"),
        ("GenfareLink", "Records", "GFL Ridership Records"),
        ("GenfareLink", "Riders", "GFL Riders"),
        ("Difference", "Records", "Record Difference"),
        ("Difference", "Riders", "Rider Difference"),
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


def _render_problem_summaries(df: pd.DataFrame, max_items: int = 12):
    if df is None or df.empty:
        return
    st.markdown("**Problem summaries**")
    for _, r in df.head(max_items).iterrows():
        ident = str(r.get("Identifier", "Issue"))
        problem = str(r.get("Problem Summary", ""))
        cause = str(r.get("Likely Cause", ""))
        evidence = str(r.get("Evidence", ""))
        st.markdown(f"**{ident}:** {problem}  \n**Likely cause:** {cause}  \n**Evidence:** {evidence}")


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
        ("Evidence", "Score", "Confidence %"),
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
        ("GenfareLink", "Riders", "GFL Riders"),
        ("Difference", "Count", "Count Difference"),
        ("Difference", "Assignment", "Assignment Difference"),
        ("Match", "Exact assignments", "Exact Assignment Matches"),
        ("Evidence", "Score", "Confidence %"),
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
def _render_ridership_day_drilldown(
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
    st.divider()
    st.markdown("## By day")
    st.caption("Each date is a compact column. Open one day to show the detailed investigation below the date strip.")

    if day is None or day.empty:
        st.info("Daily reconciliation is unavailable. Upload GDS Legacy Transaction Detail or a RouteSum report with daily detail.")
        return

    selected_key = "selected_ridership_day_v5"
    valid_dates = day["Date"].astype(str).tolist()
    if st.session_state.get(selected_key) not in valid_dates:
        st.session_state[selected_key] = None

    def _select_day(date: str):
        current = st.session_state.get(selected_key)
        st.session_state[selected_key] = None if current == date else date

    def _render_day_card(drow):
        date = str(drow["Date"])
        try:
            date_heading = pd.to_datetime(date).strftime("%b %d").replace(" 0", " ")
        except Exception:
            date_heading = date
        legacy_value = drow.get("Legacy Ridership")
        gfl_raw = drow.get("GFL Raw Ridership")
        gfl_norm = drow.get("GFL Normalized Ridership")
        raw_day_diff = drow.get("Raw Difference")
        norm_day_diff = drow.get("Normalized Difference")
        authoritative = bool(drow.get("Legacy Daily Authoritative", False))
        status = str(drow.get("Status", ""))
        is_selected = st.session_state.get(selected_key) == date

        st.markdown(f"### {date_heading}")
        st.markdown("**Legacy**")
        st.metric("Ridership", _fmt_num(legacy_value))
        st.markdown("**GenfareLink**")
        st.metric("Raw", _fmt_num(gfl_raw))
        st.metric("Adjusted", _fmt_num(gfl_norm))
        st.markdown("**Difference**")
        st.metric("Raw" if authoritative else "Derived", _fmt_diff(raw_day_diff))
        st.metric("Adjusted", _fmt_diff(norm_day_diff))
        if status == "Reconciled after behavior normalization" or (not pd.isna(norm_day_diff) and abs(float(norm_day_diff)) < 1e-9):
            st.caption("✅ Reconciled")
        elif status == "Raw match":
            st.caption("✅ Raw match")
        elif status == "Legacy daily total unavailable":
            st.caption("ℹ️ Daily total unavailable")
        else:
            st.caption("⚠️ Needs drill-down")
        st.button(
            "Close" if is_selected else "Open",
            key=f"rid_select_day_{date}",
            on_click=_select_day,
            args=(date,),
            use_container_width=True,
            type="primary" if is_selected else "secondary",
        )

    if len(day) <= 6:
        cols = st.columns(len(day), gap="small")
        for col, (_, drow) in zip(cols, day.iterrows()):
            with col:
                with st.container(border=True):
                    _render_day_card(drow)
    else:
        st.caption("Scroll horizontally within this date strip to view additional days.")
        with st.container(horizontal=True, wrap=False, gap="small", key="rid_day_strip_v5"):
            for _, drow in day.iterrows():
                with st.container(border=True, width=250):
                    _render_day_card(drow)

    selected_date = st.session_state.get(selected_key)
    if not selected_date:
        st.info("Select **Open** under a date to display its detailed drill-down here.")
        return

    dmatch = day[day["Date"].astype(str) == str(selected_date)]
    if dmatch.empty:
        return
    drow = dmatch.iloc[0]
    try:
        heading = pd.to_datetime(selected_date).strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:
        heading = str(selected_date)

    with st.container(border=True):
        st.markdown(f"## {heading} drill-down")
        legacy_value = drow.get("Legacy Ridership")
        gfl_raw = drow.get("GFL Raw Ridership")
        gfl_norm = drow.get("GFL Normalized Ridership")
        raw_day_diff = drow.get("Raw Difference")
        norm_day_diff = drow.get("Normalized Difference")
        authoritative = bool(drow.get("Legacy Daily Authoritative", False))
        source = str(drow.get("Legacy Source", ""))

        lc, gc, dc = st.columns(3)
        with lc:
            st.markdown("**Legacy**")
            st.metric("Ridership", _fmt_num(legacy_value))
            st.caption(("Authoritative daily total" if authoritative else "Derived daily total") + f" · {source}")
        with gc:
            st.markdown("**GenfareLink**")
            g1, g2 = st.columns(2)
            g1.metric("Raw ridership", _fmt_num(gfl_raw))
            g2.metric("After Legacy behavior", _fmt_num(gfl_norm))
        with dc:
            st.markdown("**Difference**")
            d1, d2 = st.columns(2)
            d1.metric("Raw" if authoritative else "Derived", _fmt_diff(raw_day_diff))
            d2.metric("After behavior", _fmt_diff(norm_day_diff))

        date = str(selected_date)
        current = day_ident[day_ident["Date"].astype(str) == date].copy() if day_ident is not None and not day_ident.empty else pd.DataFrame()
        st.markdown("### Fare categories")
        if current.empty:
            st.info("No day-level Key/TTP comparison is available. GDS Legacy Transaction Detail is required for this view.")
            selected_identifier = None
        else:
            rider_diff = pd.to_numeric(current["Rider Difference"], errors="coerce").fillna(0)
            problems = current[(rider_diff.abs() > 1e-9) | current["Likely Cause"].str.contains("inconsistency|unresolved|difference", case=False, na=False)].copy()
            ptab, ktab, ttab, atab = st.tabs(["Problems", "Keys", "TTPs", "All categories"])
            with ptab:
                if problems.empty:
                    st.success("No fare-category rider problems for this day.")
                else:
                    st.dataframe(_key_ttp_display(problems), width="stretch", hide_index=True)
                    _render_problem_summaries(problems)
            with ktab:
                st.dataframe(_key_ttp_display(current[current["Type"] == "Key"]), width="stretch", hide_index=True)
            with ttab:
                st.dataframe(_key_ttp_display(current[current["Type"] == "TTP"]), width="stretch", hide_index=True)
            with atab:
                st.dataframe(_key_ttp_display(current), width="stretch", hide_index=True)
            candidates = problems["Identifier"].astype(str).tolist() if not problems.empty else current["Identifier"].astype(str).tolist()
            candidates = list(dict.fromkeys(candidates))
            selected_identifier = None
            if candidates:
                selected_identifier = st.selectbox("Trace one category deeper", candidates, key=f"rid_identifier_{date}")
                crow = current[current["Identifier"].astype(str) == str(selected_identifier)]
                if not crow.empty:
                    r = crow.iloc[0]
                    _summary_box(selected_identifier, str(r["Likely Cause"]), str(r["Evidence"]), "success" if str(r["Likely Cause"]) == "Match" else "warning")

        st.markdown("### Where it moved")

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
            st.success("No route-level rider movement for the current selection.") if x.empty else st.dataframe(_location_display(x), width="stretch", hide_index=True)
        with runtab:
            x = _problem_only(filt(run_cmp))
            st.success("No run-level rider movement for the current selection.") if x.empty else st.dataframe(_location_display(x), width="stretch", hide_index=True)
        with bustab:
            x = _problem_only(filt(bus_cmp))
            st.success("No bus-level rider movement for the current selection.") if x.empty else st.dataframe(_location_display(x), width="stretch", hide_index=True)
        with movetab:
            x = filt(movements)
            if x.empty:
                st.info("No exact equal-and-opposite movement was detected for the current day/category.")
            else:
                st.dataframe(_movement_display(x), width="stretch", hide_index=True)
                for _, r in x.head(5).iterrows():
                    st.info(f"**{r['Likely Cause']}** — {r['Evidence']} (evidence score {int(r['Confidence %'])}%)")
        with exacttab:
            x = _problem_only(filt(exact_cmp))
            st.success("No Route + Run + Bus rider difference for the current selection.") if x.empty else st.dataframe(_location_display(x), width="stretch", hide_index=True)

        st.markdown("### Transaction match")
        if not has_legacy_tx:
            st.info("Upload GDS Legacy Transaction Detail to enable transaction matching.")
        elif txm is None or txm.empty:
            st.success("No remaining transaction-signature discrepancy was identified by the targeted matcher.")
        else:
            txview = txm[txm["Date"].astype(str) == date].copy()
            if selected_identifier is not None:
                txview = txview[txview["Identifier"].astype(str) == str(selected_identifier)]
            if txview.empty:
                st.success("No remaining transaction mismatch for the current day/category.")
            else:
                st.dataframe(_tx_display(txview), width="stretch", hide_index=True)
                top = txview.iloc[0]
                _summary_box("Most specific remaining issue", str(top["Likely Cause"]), str(top["Evidence"]), "warning")
                with st.expander("Show assignment details for the top mismatch"):
                    st.markdown(f"**Legacy assignments:** {top.get('Legacy Assignments', '—')}")
                    st.markdown(f"**GenfareLink assignments:** {top.get('GFL Assignments', '—')}")


# ---------- revenue displays ----------

def _revenue_route_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    cols = {}
    for c in ["route"]:
        if c in df.columns:
            cols[("Location", c.title())] = df[c].values
    for source, group, label in [
        ("current_revenue", "Legacy", "Current"),
        ("unclassified_revenue", "Legacy", "Unclassified"),
        ("legacy_revenue", "Legacy", "Total"),
        ("gfl_revenue", "GenfareLink", "Revenue"),
        ("difference", "Difference", "GFL - Legacy"),
    ]:
        if source in df.columns:
            cols[(group, label)] = df[source].values
    out = pd.DataFrame(cols)
    if len(out.columns):
        out.columns = pd.MultiIndex.from_tuples(out.columns)
    return out


def _revenue_investigative_display(df: pd.DataFrame, location_cols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    cols = {}
    for c in location_cols:
        if c in df.columns:
            cols[("Location", c.replace("_", " ").title())] = df[c].values
    mapping = [
        ("legacy_detail_records", "Legacy detail", "Records"),
        ("legacy_detail_amount", "Legacy detail", "Net Amt Chrg"),
        ("positive_amount", "Legacy detail", "Positive Amt Chrg"),
        ("negative_amount", "Legacy detail", "Negative Amt Chrg"),
        ("gfl_records", "GenfareLink", "Records"),
        ("gfl_revenue", "GenfareLink", "Revenue"),
        ("investigative_difference", "Investigative only", "GFL - Detail net"),
    ]
    for source, group, label in mapping:
        if source in df.columns:
            cols[(group, label)] = df[source].values
    out = pd.DataFrame(cols)
    if len(out.columns):
        out.columns = pd.MultiIndex.from_tuples(out.columns)
    return out


def _revenue_movement_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    return df.rename(columns={
        "legacy_location": "Legacy location",
        "gfl_location": "GFL location",
        "amount": "Amount",
        "confidence": "Evidence score",
        "evidence": "Evidence",
    })[["Legacy location", "GFL location", "Amount", "Evidence score", "Evidence"]]


def _revenue_tx_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    wanted = [
        "Timestamp", "Bus", "Category", "Amount", "Legacy Records", "GFL Records",
        "Count Difference", "Assignment Difference", "Confidence %",
    ]
    return df[[c for c in wanted if c in df.columns]].copy()


@st.fragment
def _render_revenue_day_drilldown(result: dict, has_legacy_tx: bool):
    drill = result.get("drilldowns", {})
    day = drill.get("day_comparison", pd.DataFrame())
    st.divider()
    st.markdown("## By day")
    st.caption("Authoritative day differences require a GDS Legacy RouteSum report that contains daily detail (BY ROUTE-DATE).")
    if day is None or day.empty:
        st.info("No dated revenue data was detected.")
        return

    selected_key = "selected_revenue_day_v5"
    valid_dates = day["date"].astype(str).tolist()
    if st.session_state.get(selected_key) not in valid_dates:
        st.session_state[selected_key] = None

    def select_day(date: str):
        current = st.session_state.get(selected_key)
        st.session_state[selected_key] = None if current == date else date

    def card(r):
        date = str(r["date"])
        try:
            title = pd.to_datetime(date).strftime("%b %d").replace(" 0", " ")
        except Exception:
            title = date
        auth = bool(r.get("legacy_authoritative", False))
        selected = st.session_state.get(selected_key) == date
        st.markdown(f"### {title}")
        st.markdown("**Legacy**")
        if auth:
            st.metric("Total", _fmt_money(r.get("legacy_revenue")))
            st.caption(f"Current {_fmt_money(r.get('current_revenue'))} · Unclassified {_fmt_money(r.get('unclassified_revenue'))}")
        else:
            st.metric("Total", "—")
            if not pd.isna(r.get("legacy_detail_net")):
                st.caption(f"Transaction Detail net: {_fmt_money(r.get('legacy_detail_net'))} · investigative only")
            else:
                st.caption("Daily ROUTESUM total unavailable")
        st.markdown("**GenfareLink**")
        st.metric("Revenue", _fmt_money(r.get("gfl_revenue")))
        st.markdown("**Difference**")
        st.metric("GFL - Legacy", _fmt_money_diff(r.get("difference")) if auth else "—")
        if auth and abs(float(r.get("difference", 0) or 0)) <= 0.005:
            st.caption("✅ Reconciled")
        elif auth:
            st.caption("⚠️ Needs drill-down")
        else:
            st.caption("ℹ️ Authoritative daily Legacy total unavailable")
        st.button(
            "Close" if selected else "Open",
            key=f"rev_select_day_{date}",
            on_click=select_day,
            args=(date,),
            use_container_width=True,
            type="primary" if selected else "secondary",
        )

    if len(day) <= 6:
        cols = st.columns(len(day), gap="small")
        for col, (_, r) in zip(cols, day.iterrows()):
            with col:
                with st.container(border=True):
                    card(r)
    else:
        st.caption("Scroll horizontally within this date strip to view additional days.")
        with st.container(horizontal=True, wrap=False, gap="small", key="rev_day_strip_v5"):
            for _, r in day.iterrows():
                with st.container(border=True, width=250):
                    card(r)

    selected = st.session_state.get(selected_key)
    if not selected:
        st.info("Select **Open** under a date to display its revenue drill-down here.")
        return
    row = day[day["date"].astype(str) == str(selected)]
    if row.empty:
        return
    r = row.iloc[0]
    try:
        heading = pd.to_datetime(selected).strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:
        heading = str(selected)

    with st.container(border=True):
        st.markdown(f"## {heading} drill-down")
        auth = bool(r.get("legacy_authoritative", False))
        lc, gc, dc = st.columns(3)
        with lc:
            st.markdown("**Legacy**")
            st.metric("Total revenue", _fmt_money(r.get("legacy_revenue")) if auth else "—")
            if auth:
                st.caption(f"Current {_fmt_money(r.get('current_revenue'))} · Unclassified {_fmt_money(r.get('unclassified_revenue'))}")
            elif not pd.isna(r.get("legacy_detail_net")):
                st.caption(f"Transaction Detail net {_fmt_money(r.get('legacy_detail_net'))} · investigative only")
        with gc:
            st.markdown("**GenfareLink**")
            st.metric("Revenue", _fmt_money(r.get("gfl_revenue")))
        with dc:
            st.markdown("**Difference**")
            st.metric("GFL - Legacy", _fmt_money_diff(r.get("difference")) if auth else "—")
            st.caption("Authoritative" if auth else "No authoritative Legacy daily total")

        date = str(selected)
        route = drill.get("day_route_comparison", pd.DataFrame())
        product = drill.get("day_product_investigation", pd.DataFrame())
        run = drill.get("day_run_investigation", pd.DataFrame())
        bus = drill.get("day_bus_investigation", pd.DataFrame())
        exact = drill.get("day_route_run_bus_investigation", pd.DataFrame())
        moves = drill.get("equal_opposite_movements", pd.DataFrame())
        txm = drill.get("transaction_matches", pd.DataFrame())

        st.markdown("### Revenue breakdown")
        rtab, ptab, runtab, bustab, movetab, exacttab = st.tabs(["Routes", "Products", "Runs", "Buses", "Equal & opposite", "Route + Run + Bus"])
        with rtab:
            rv = route[route["date"].astype(str) == date].copy() if route is not None and not route.empty else pd.DataFrame()
            if not auth:
                st.info("Authoritative daily route revenue is unavailable. Use a BY ROUTE-DATE RouteSum report to enable this comparison.")
            elif rv.empty:
                st.success("No route revenue rows were found for this day.")
            else:
                problems = rv[pd.to_numeric(rv["difference"], errors="coerce").abs() > 0.005]
                if problems.empty:
                    st.success("All route revenue totals reconcile for this day.")
                    st.dataframe(_revenue_route_display(rv), width="stretch", hide_index=True)
                else:
                    st.dataframe(_revenue_route_display(problems), width="stretch", hide_index=True)
        with ptab:
            pv = product[product["date"].astype(str) == date].copy() if product is not None and not product.empty else pd.DataFrame()
            if not has_legacy_tx:
                st.info("Upload GDS Legacy Transaction Detail for product-level investigative evidence.")
            elif pv.empty:
                st.info("No product-level investigative evidence was produced for this day.")
            else:
                st.warning("Product amounts from Legacy Transaction Detail are investigative only and are not authoritative accounting totals.")
                st.dataframe(_revenue_investigative_display(pv, ["category"]), width="stretch", hide_index=True)
        with runtab:
            rv = run[run["date"].astype(str) == date].copy() if run is not None and not run.empty else pd.DataFrame()
            if rv.empty:
                st.info("No run-level investigative evidence is available.")
            else:
                st.warning("Legacy run amounts come from Transaction Detail Amt Chrg and are investigative only.")
                st.dataframe(_revenue_investigative_display(rv, ["route", "run"]), width="stretch", hide_index=True)
        with bustab:
            bv = bus[bus["date"].astype(str) == date].copy() if bus is not None and not bus.empty else pd.DataFrame()
            if bv.empty:
                st.info("No bus-level investigative evidence is available.")
            else:
                st.warning("Legacy bus amounts come from Transaction Detail Amt Chrg and are investigative only.")
                st.dataframe(_revenue_investigative_display(bv, ["route", "bus"]), width="stretch", hide_index=True)
        with movetab:
            mv = moves[moves["date"].astype(str) == date].copy() if moves is not None and not moves.empty else pd.DataFrame()
            if mv.empty:
                st.info("No exact equal-and-opposite authoritative route revenue movement was detected for this day.")
            else:
                st.dataframe(_revenue_movement_display(mv), width="stretch", hide_index=True)
                for _, mr in mv.head(5).iterrows():
                    st.info(f"Possible route reassignment / Data Edit — {mr['evidence']}")
        with exacttab:
            ev = exact[exact["date"].astype(str) == date].copy() if exact is not None and not exact.empty else pd.DataFrame()
            if ev.empty:
                st.info("No Route + Run + Bus investigative evidence is available.")
            else:
                st.warning("Legacy Route + Run + Bus amounts come from Transaction Detail and are investigative only.")
                st.dataframe(_revenue_investigative_display(ev, ["route", "run", "bus"]), width="stretch", hide_index=True)

        st.markdown("### Transaction match")
        if not has_legacy_tx:
            st.info("Upload GDS Legacy Transaction Detail to enable targeted revenue transaction matching.")
        elif txm is None or txm.empty:
            if auth and abs(float(r.get("difference", 0) or 0)) > 0.005:
                st.info("No exact non-zero amount signature mismatch was found for this problem day. The remaining difference may come from report semantics, unmatched adjustments, or revenue not represented by Transaction Detail Amt Chrg.")
            else:
                st.success("No targeted revenue transaction mismatch is currently flagged for this day.")
        else:
            tv = txm[txm["Date"].astype(str) == date].copy()
            if tv.empty:
                st.success("No targeted revenue transaction mismatch is currently flagged for this day.")
            else:
                st.warning("Transaction matching is investigative evidence. Legacy Transaction Detail is not the accounting source of truth.")
                st.dataframe(_revenue_tx_display(tv), width="stretch", hide_index=True)
                top = tv.iloc[0]
                _summary_box("Most specific transaction evidence", str(top["Likely Cause"]), str(top["Evidence"]), "warning")
                with st.expander("Show assignment details for the top mismatch"):
                    st.markdown(f"**Legacy assignments:** {top.get('Legacy Assignments', '—')}")
                    st.markdown(f"**GenfareLink assignments:** {top.get('GFL Assignments', '—')}")


# ---------- Ridership tab ----------

def _build_ridership_payload(gfl_file, routesum_file, legacy_tx_file):
    missing = []
    if not gfl_file:
        missing.append("Genfare Link Raw Data")
    if not routesum_file:
        missing.append("GDS Legacy Event Summary Data")
    if missing:
        st.error("Please upload: " + ", ".join(missing))
        return None

    temp_paths, diagnostics = [], []
    start = time.perf_counter()
    try:
        with st.spinner("Scanning reports and building ridership evidence..."):
            gfl_path = save_uploaded_file(gfl_file)
            temp_paths.append(gfl_path)
            gfl = profile_gfl_ridership(gfl_path)
            routesum = parse_routesum(routesum_file, routesum_file.name)
            legacy_tx = None
            if legacy_tx_file:
                ltx_path = save_uploaded_file(legacy_tx_file)
                temp_paths.append(ltx_path)
                legacy_tx = profile_legacy_transaction_detail(ltx_path)
            result = reconcile(gfl, routesum, legacy_tx)
        elapsed = time.perf_counter() - start
        for label, obj, filename in [
            ("Genfare Link Raw Data", gfl, gfl_file.name),
            ("GDS Legacy Event Summary Data", routesum, routesum_file.name),
            ("GDS Legacy Transaction Detail", legacy_tx, legacy_tx_file.name if legacy_tx_file else "Not uploaded"),
        ]:
            if obj is None:
                diagnostics.append({"Report": label, "File": filename, "Detected As": "Not uploaded", "Confidence": "—", "Warnings": "Full drill-down unavailable"})
            else:
                diagnostics.append({"Report": label, "File": filename, "Detected As": obj.kind, "Confidence": f"{obj.confidence}%", "Warnings": " | ".join(getattr(obj, "warnings", []) or []) or "None"})
        return {"result": result, "diagnostics_df": pd.DataFrame(diagnostics), "elapsed": elapsed, "has_legacy_tx": legacy_tx is not None}
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except Exception:
                pass


def render_ridership_tab():
    st.subheader("Ridership reconciliation")
    st.caption("Start with the total, then drill into the day, fare category, route/run/bus, and remaining transaction differences.")
    with st.expander("Upload guidance", expanded=False):
        st.markdown("""
**Required**

- **Genfare Link Raw Data** *(Ridership tab)* — GFL ridership raw-data CSV.
- **GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)* — PDF preferred for Key/TTP rule inference.

**Strongly recommended**

- **GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)* — CSV enables daily reconstruction and detailed drill-down.
""")
    left, right = st.columns(2)
    with left:
        st.markdown("**Genfare Link Raw Data** *(Ridership tab)*")
        gfl_file = st.file_uploader("Genfare Link Raw Data (Ridership tab)", type=["csv"], key="rid_gfl", label_visibility="collapsed")
    with right:
        st.markdown("**GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)*")
        routesum_file = st.file_uploader("GDS Legacy Event Summary Data", type=["pdf", "csv"], key="rid_routesum", label_visibility="collapsed")
        st.markdown("**GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)*")
        legacy_tx_file = st.file_uploader("GDS Legacy Transaction Detail", type=["csv"], key="rid_legacy_tx", label_visibility="collapsed")

    if st.button("Reconcile ridership", type="primary", use_container_width=True, key="run_ridership"):
        try:
            payload = _build_ridership_payload(gfl_file, routesum_file, legacy_tx_file)
            if payload is not None:
                st.session_state["ridership_payload_v5"] = payload
                for key in list(st.session_state.keys()):
                    if str(key).startswith("rid_select_day_") or str(key).startswith("rid_identifier_") or key == "selected_ridership_day_v5":
                        del st.session_state[key]
        except Exception as exc:
            st.exception(exc)

    payload = st.session_state.get("ridership_payload_v5")
    if payload is None:
        return
    result = payload["result"]
    diagnostics_df = payload["diagnostics_df"]
    elapsed = float(payload["elapsed"])
    has_legacy_tx = bool(payload["has_legacy_tx"])
    drill, s = result["drilldowns"], result["summary"]

    st.success(f"Processed {s['gfl_rows']:,} GFL rows" + (f" and {s['legacy_transaction_rows']:,} Legacy transaction rows" if s["legacy_transaction_rows"] else "") + f" in {elapsed:,.1f} seconds.")
    for note in drill.get("notes", []):
        st.warning(note)

    raw_diff = s["gfl_raw_ridership"] - s["legacy_ridership"]
    norm_diff = s["gfl_legacy_rule_normalized_ridership"] - s["legacy_ridership"]
    l, g, d = st.columns(3)
    with l:
        st.markdown("#### Legacy")
        st.metric("Ridership", f"{s['legacy_ridership']:,.0f}")
        st.caption("Authoritative total from ROUTESUM")
    with g:
        st.markdown("#### GenfareLink")
        g1, g2 = st.columns(2)
        g1.metric("Raw ridership", f"{s['gfl_raw_ridership']:,.0f}")
        g2.metric("After Legacy behavior", f"{s['gfl_legacy_rule_normalized_ridership']:,.0f}")
    with d:
        st.markdown("#### Difference")
        d1, d2 = st.columns(2)
        d1.metric("Raw", f"{raw_diff:+,.0f}")
        d2.metric("After behavior", f"{norm_diff:+,.0f}")

    if abs(raw_diff) < 1e-9:
        _summary_box("Overall totals match", "No systemwide raw ridership discrepancy.", "Legacy ROUTESUM total equals the raw GFL Ridership sum.", "success")
    elif abs(norm_diff) < 1e-9:
        _summary_box("The raw gap is fully explained", "Different Key/TTP ridership behavior between the systems.", f"Raw gap={raw_diff:+,.0f}. Applying inferred Legacy behavior removes {s['inferred_rule_adjustment']:,.0f} GFL riders and leaves 0 unexplained.", "info")
    else:
        _summary_box("Ridership still differs after behavior alignment", "At least part of the gap is not explained by fare-category behavior.", f"Raw gap={raw_diff:+,.0f}; remaining gap={norm_diff:+,.0f}. Open the relevant day below.", "warning")

    _render_ridership_day_drilldown(
        drill.get("day_comparison", pd.DataFrame()), drill.get("day_identifier", pd.DataFrame()),
        drill.get("route_comparison_by_day", pd.DataFrame()), drill.get("run_comparison_by_day", pd.DataFrame()),
        drill.get("bus_comparison_by_day", pd.DataFrame()), drill.get("route_run_bus_by_day", pd.DataFrame()),
        drill.get("equal_opposite_movements", pd.DataFrame()), drill.get("transaction_matches", pd.DataFrame()), has_legacy_tx,
    )

    st.divider()
    st.markdown("## Downloads")
    c1, c2 = st.columns(2)
    c1.download_button("Download summary CSV", rid_to_csv_bytes(result["findings"]), "ridership_reconciliation_summary_v5.csv", "text/csv", use_container_width=True)
    c2.download_button("Download full drill-down Excel", rid_to_excel_bytes(result, diagnostics_df), "ridership_reconciliation_full_v5.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    st.markdown("### External AI review")
    st.caption("No AI agent runs inside the app. These files package deterministic evidence for an external AI review.")
    a1, a2 = st.columns(2)
    a1.download_button("Download AI evidence JSON", rid_to_ai_json_bytes(result, diagnostics_df), "ridership_ai_evidence_v5.json", "application/json", use_container_width=True)
    a2.download_button("Download AI review prompt", rid_to_ai_prompt_bytes(), "ridership_ai_review_prompt_v5.md", "text/markdown", use_container_width=True)
    with st.expander("Diagnostics"):
        st.dataframe(diagnostics_df, width="stretch", hide_index=True)
        st.markdown(f"Legacy behavior inference confidence: **{s['rule_confidence']}%**; exact route fit: **{s['rule_exact_fit']}**.")


# ---------- Revenue tab ----------

def _build_revenue_payload(gfl_file, routesum_file, legacy_tx_file):
    missing = []
    if not gfl_file:
        missing.append("Genfare Link Raw Data (Revenue tab)")
    if not routesum_file:
        missing.append("GDS Legacy Event Summary Data")
    if missing:
        st.error("Please upload: " + ", ".join(missing))
        return None

    temp_paths, diagnostics = [], []
    start = time.perf_counter()
    try:
        with st.spinner("Scanning reports and building revenue evidence..."):
            gfl_path = save_uploaded_file(gfl_file)
            temp_paths.append(gfl_path)
            gfl = profile_gfl_revenue(gfl_path)
            routesum = parse_routesum(routesum_file, routesum_file.name)
            legacy_tx = None
            legacy_path = None
            if legacy_tx_file:
                legacy_path = save_uploaded_file(legacy_tx_file)
                temp_paths.append(legacy_path)
                legacy_tx = profile_legacy_revenue_transactions(legacy_path)

            # First pass identifies which authoritative days actually differ.
            initial = reconcile_revenue(gfl, routesum, legacy_tx)
            day = initial["drilldowns"].get("day_comparison", pd.DataFrame())
            problem_dates = []
            if not day.empty:
                tmp = day[day["legacy_authoritative"] == True].copy()  # noqa: E712
                tmp = tmp[pd.to_numeric(tmp["difference"], errors="coerce").abs() > 0.005]
                if not tmp.empty:
                    tmp["_abs"] = pd.to_numeric(tmp["difference"], errors="coerce").abs()
                    problem_dates = tmp.sort_values("_abs", ascending=False)["date"].astype(str).head(14).tolist()
            txm = pd.DataFrame()
            if legacy_path and problem_dates:
                txm = match_revenue_transactions(gfl_path, legacy_path, problem_dates)
            result = reconcile_revenue(gfl, routesum, legacy_tx, transaction_matches=txm)

        elapsed = time.perf_counter() - start
        for label, obj, filename in [
            ("Genfare Link Raw Data (Revenue)", gfl, gfl_file.name),
            ("GDS Legacy Event Summary Data", routesum, routesum_file.name),
            ("GDS Legacy Transaction Detail", legacy_tx, legacy_tx_file.name if legacy_tx_file else "Not uploaded"),
        ]:
            if obj is None:
                diagnostics.append({"Report": label, "File": filename, "Detected As": "Not uploaded", "Confidence": "—", "Warnings": "Detailed investigation limited"})
            else:
                diagnostics.append({"Report": label, "File": filename, "Detected As": obj.kind, "Confidence": f"{obj.confidence}%", "Warnings": " | ".join(getattr(obj, "warnings", []) or []) or "None"})
        return {"result": result, "diagnostics_df": pd.DataFrame(diagnostics), "elapsed": elapsed, "has_legacy_tx": legacy_tx is not None}
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except Exception:
                pass


def render_revenue_tab():
    st.subheader("Revenue reconciliation")
    st.caption("Legacy accounting totals come from ROUTESUM. GenfareLink totals come from the dedicated Revenue raw-data export. Transaction Detail is evidence, not the accounting source of truth.")
    with st.expander("Upload guidance", expanded=False):
        st.markdown("""
**Required**

- **Genfare Link Raw Data** *(Revenue tab)* — dedicated GFL Revenue raw-data CSV.
- **GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)* — authoritative Legacy Current + Unclassified Revenue. **BY ROUTE-DATE is preferred** because it enables true day-by-day revenue reconciliation.

**Strongly recommended**

- **GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)* — used only as investigative evidence for products, runs, buses, and targeted transaction signatures. Its `Amt Chrg` total is **not** treated as authoritative Legacy revenue.
""")

    left, right = st.columns(2)
    with left:
        st.markdown("**Genfare Link Raw Data** *(Revenue tab)*")
        gfl_file = st.file_uploader("Genfare Link Raw Data (Revenue tab)", type=["csv"], key="rev_gfl", label_visibility="collapsed")
    with right:
        st.markdown("**GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)*")
        routesum_file = st.file_uploader("GDS Legacy Event Summary Data Revenue", type=["pdf", "csv"], key="rev_routesum", label_visibility="collapsed")
        st.markdown("**GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)*")
        legacy_tx_file = st.file_uploader("GDS Legacy Transaction Detail Revenue", type=["csv"], key="rev_legacy_tx", label_visibility="collapsed")

    if st.button("Reconcile revenue", type="primary", use_container_width=True, key="run_revenue"):
        try:
            payload = _build_revenue_payload(gfl_file, routesum_file, legacy_tx_file)
            if payload is not None:
                st.session_state["revenue_payload_v5"] = payload
                for key in list(st.session_state.keys()):
                    if str(key).startswith("rev_select_day_") or key == "selected_revenue_day_v5":
                        del st.session_state[key]
        except Exception as exc:
            st.exception(exc)

    payload = st.session_state.get("revenue_payload_v5")
    if payload is None:
        return
    result = payload["result"]
    diagnostics_df = payload["diagnostics_df"]
    elapsed = float(payload["elapsed"])
    has_legacy_tx = bool(payload["has_legacy_tx"])
    s = result["summary"]
    drill = result["drilldowns"]

    st.success(f"Processed {s['gfl_rows']:,} GFL revenue rows" + (f" and {s['legacy_transaction_rows']:,} Legacy transaction rows" if s["legacy_transaction_rows"] else "") + f" in {elapsed:,.1f} seconds.")
    for note in drill.get("notes", []):
        st.warning(note)

    l, g, d = st.columns(3)
    with l:
        st.markdown("#### Legacy")
        st.metric("Total revenue", _fmt_money(s["legacy_total_revenue"]))
        st.caption(f"Current {_fmt_money(s['legacy_current_revenue'])} · Unclassified {_fmt_money(s['legacy_unclassified_revenue'])}")
    with g:
        st.markdown("#### GenfareLink")
        st.metric("Revenue", _fmt_money(s["gfl_revenue"]))
        st.caption("Dedicated Revenue raw-data export")
    with d:
        st.markdown("#### Difference")
        st.metric("GFL - Legacy", _fmt_money_diff(s["difference"]))

    if abs(float(s["difference"])) <= 0.005:
        _summary_box("Overall revenue matches", "No systemwide revenue discrepancy.", "GFL Revenue equals Legacy ROUTESUM Current + Unclassified Revenue.", "success")
    else:
        _summary_box("Revenue totals differ", "The systemwide gap is real at the report-total level; drill down before assigning a cause.", f"Legacy total={_fmt_money(s['legacy_total_revenue'])}; GFL={_fmt_money(s['gfl_revenue'])}; difference={_fmt_money_diff(s['difference'])}.", "warning")

    # Period routes are authoritative even when the RouteSum is not BY ROUTE-DATE.
    pr = drill.get("period_route_comparison", pd.DataFrame())
    with st.expander("Period route reconciliation", expanded=False):
        if pr is None or pr.empty:
            st.info("No route-level revenue comparison is available.")
        else:
            probs = pr[pd.to_numeric(pr["difference"], errors="coerce").abs() > 0.005]
            if probs.empty:
                st.success("All period route revenue totals reconcile.")
            else:
                st.dataframe(_revenue_route_display(probs), width="stretch", hide_index=True)

    _render_revenue_day_drilldown(result, has_legacy_tx)

    st.divider()
    st.markdown("## Downloads")
    c1, c2 = st.columns(2)
    c1.download_button("Download summary CSV", rev_to_csv_bytes(result["findings"]), "revenue_reconciliation_summary_v5.csv", "text/csv", use_container_width=True)
    c2.download_button("Download full drill-down Excel", rev_to_excel_bytes(result, diagnostics_df), "revenue_reconciliation_full_v5.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
    st.markdown("### External AI review")
    st.caption("No AI agent runs inside the app. The export explicitly labels authoritative vs investigative revenue evidence.")
    a1, a2 = st.columns(2)
    a1.download_button("Download AI evidence JSON", rev_to_ai_json_bytes(result, diagnostics_df), "revenue_ai_evidence_v5.json", "application/json", use_container_width=True)
    a2.download_button("Download AI review prompt", rev_to_ai_prompt_bytes(), "revenue_ai_review_prompt_v5.md", "text/markdown", use_container_width=True)
    with st.expander("Diagnostics"):
        st.dataframe(diagnostics_df, width="stretch", hide_index=True)
        if s.get("legacy_transaction_detail_net_amount") is not None:
            st.caption(f"Legacy Transaction Detail net Amt Chrg = {_fmt_money(s['legacy_transaction_detail_net_amount'])}. This is shown for diagnostics only and is not the Legacy accounting total.")


rid_tab, rev_tab = st.tabs(["Ridership", "Revenue"])
with rid_tab:
    render_ridership_tab()
with rev_tab:
    render_revenue_tab()
