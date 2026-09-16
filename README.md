# Genfare Ridership Reconciler V4.2

Ridership-only Legacy vs GenfareLink reconciliation with a vertical, day-first investigation interface.

## What changed in V4.2

- Day drill-down controls now run inside a Streamlit fragment. Changing a toggle, Key/TTP dropdown, or drill-down selection refreshes only the investigation area instead of rerunning/resetting the whole app. The upload area and overall reconciliation stay on screen.

- Removed numbered investigation sections from the main UI.
- Replaced the wide day table with vertically stacked date cards.
- Each date shows grouped **Legacy**, **GenfareLink**, and **Difference** metrics.
- Open any day to reveal the deeper investigation for that day only.
- Key/TTP tables use grouped headers so Legacy and GFL fields stay together.
- Route/run/bus tables use the same grouped presentation.
- Daily Legacy reconstruction now uses fare-use / boarding-candidate transaction types instead of counting issuance/admin transactions as riders.
- Targeted transaction matching also excludes Legacy issuance/admin rows.
- Derived daily values are explicitly labeled as derived; ROUTESUM remains the authoritative period total unless a ROUTESUM by route-date report supplies authoritative daily totals.

## Reports

Required:

1. GFL Ridership Raw Data CSV
2. Legacy EVENT SUMMARY / ROUTESUM (PDF preferred)

Strongly recommended:

3. Legacy TRANSACTION DETAIL CSV

Revenue is intentionally excluded.

## Investigation flow

The app first displays the overall Legacy/GFL totals. Dates then appear vertically. Each date card shows Legacy, GFL, and differences in separate grouped columns. Opening a day reveals:

- fare-category problems, Keys, TTPs, and all categories;
- route, run, bus, and exact route+run+bus differences;
- equal-and-opposite movement detection;
- targeted transaction matching for unresolved rider categories.

## Large-file design

DuckDB performs the main CSV scans and aggregations so large raw exports do not need to be loaded into pandas in full. Transaction matching performs a second targeted scan only for discrepant day/category combinations.

## Railway

The project includes `Dockerfile` and `railway.toml` for Railway deployment.

Typical deploy flow:

```bash
git add .
git commit -m "Update ridership reconciler V4.2"
git push
```

If the Railway service is linked to the GitHub repository, it should redeploy automatically.

## Streamlit

The app can still run on Streamlit Community Cloud for smaller tests. Railway is preferable when testing larger multi-day transaction files or when you want more control over CPU/RAM.

## External AI review

No AI agent runs inside the app. The app exports a structured JSON evidence package and a review prompt that can be uploaded to an external AI for interpretation. The deterministic reconciliation engine remains the source of truth for arithmetic.
