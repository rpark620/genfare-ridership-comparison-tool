# Genfare Report Reconciler V5

One Railway-hosted Streamlit application with two top-level workflows:

- **Ridership** — the V4.4 drill-down workflow, including automatic Legacy ridership-behavior inference, daily comparison, Key/TTP investigation, route/run/bus movement analysis, targeted transaction matching, and external-AI evidence export.
- **Revenue** — a new revenue reconciliation workflow using authoritative Legacy ROUTESUM revenue and the dedicated GenfareLink Revenue raw-data export.

## Revenue source-of-truth rules

The Revenue tab intentionally separates authoritative accounting totals from investigative evidence:

1. **Legacy authoritative revenue:** `Current Revenue + Unclassified Revenue` from GDS Legacy Event Summary / ROUTESUM.
2. **GenfareLink authoritative revenue:** the dedicated GFL **Revenue** raw-data export.
3. **Legacy Transaction Detail `Amt Chrg`:** investigative only. It is useful for product/run/bus and transaction evidence, but it is not assumed to sum to ROUTESUM revenue.

A **BY ROUTE-DATE** ROUTESUM is strongly preferred for Revenue because it provides authoritative Legacy daily revenue and route revenue by day. A plain BY ROUTE report still supports overall and period-level route reconciliation.

## Uploads

### Ridership

- **Genfare Link Raw Data** *(Ridership tab)* — required.
- **GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)* — required.
- **GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)* — strongly recommended.

### Revenue

- **Genfare Link Raw Data** *(Revenue tab)* — required.
- **GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)* — required; BY ROUTE-DATE preferred.
- **GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)* — strongly recommended for investigation only.

## Revenue drill-down

The Revenue tab follows the same interaction pattern as Ridership:

1. Overall Legacy vs GFL revenue.
2. Compact date cards across the page; more than six days get a dedicated horizontal scroll strip.
3. Open one date to show its full-width drill-down below the cards.
4. Authoritative route comparison when daily ROUTESUM data is available.
5. Product, run, bus, and Route+Run+Bus investigative evidence from Legacy Transaction Detail.
6. Equal-and-opposite authoritative route movements to flag likely allocation/Data Edit behavior.
7. Targeted transaction signature matching on authoritative problem days.
8. CSV, Excel, AI-evidence JSON, and AI-review-prompt downloads.

## Railway deployment

This remains a Streamlit application hosted on Railway. Railway runs the included Dockerfile.

Push the V5 files to the existing GitHub repository and Railway should redeploy automatically.

The included Railway start command is effectively:

```bash
streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT:-8501}
```

No OpenAI/API key is required. AI review is external: the app exports a JSON evidence package and a prompt.
