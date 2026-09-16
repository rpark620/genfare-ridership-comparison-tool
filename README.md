# Genfare Ridership Reconciler V4.4

Ridership-only Legacy vs GenfareLink reconciliation with a vertical, day-first investigation interface.

## What changed in V4.4

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

1. Genfare Link Raw Data CSV *(Ridership tab)*
2. Legacy EVENT SUMMARY / ROUTESUM (PDF preferred)

Strongly recommended:

3. GDS Legacy Transaction Detail *(Transaction Report > Transaction Detail Report)*

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
git commit -m "Update ridership reconciler V4.4"
git push
```

If the Railway service is linked to the GitHub repository, it should redeploy automatically.

## Streamlit

The app can still run on Streamlit Community Cloud for smaller tests. Railway is preferable when testing larger multi-day transaction files or when you want more control over CPU/RAM.

## External AI review

No AI agent runs inside the app. The app exports a structured JSON evidence package and a review prompt that can be uploaded to an external AI for interpretation. The deterministic reconciliation engine remains the source of truth for arithmetic.


## V4.4 day layout

- Dates are displayed side-by-side as compact columns so a normal weekly comparison fits in one view.
- Each date card keeps Legacy, GenfareLink, and Difference values grouped vertically.
- Only one day is opened at a time. Its full Key/TTP -> route/run/bus -> transaction drill-down renders in one full-width panel below all date cards.
- Selecting a different day or fare category reruns only the drill-down fragment; the overall reconciliation above remains visible and the raw reports are not reprocessed.


## V4.4 UI changes
- Upload names now match the operator-facing report names and show the GDS navigation path beside each title.
- Up to six dates share the page width; more than six dates stay on one horizontally scrollable date strip.
- Drill-down tables are intentionally compact. Long problem/cause/evidence text is rendered below the numeric comparison instead of forcing the table wider than the page.
