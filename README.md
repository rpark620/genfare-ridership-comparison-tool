# Genfare Ridership Reconciler V4.6

Ridership-only Legacy vs GenfareLink reconciliation for Railway/Streamlit.

## What changed in V4.6

V4.6 makes the difference between **missing riders** and **assignment-only differences** much clearer.

- A day drill-down now starts with **What is actually different**.
- It separately shows:
  - riders missing from GFL;
  - extra riders in GFL;
  - the net systemwide count difference;
  - riders that exist in both systems but have a different route/run/trip/driver assignment.
- True count issues are summarized by **fare category** and by **bus**.
- The transaction matcher now labels every row as **Missing from GFL**, **Extra in GFL**, or **Assignment difference**.
- Assignment differences also say whether the **route, run, trip, or driver** differs.
- Location tables are explicitly labeled as location differences so a route deficit is not mistaken for a systemwide missing rider.
- The old equal-and-opposite view is now **Aggregate offsets (unconfirmed)**.
- Aggregate route offsets are paired only inside the **same bus**. A deficit on Bus 327 will no longer be paired with a surplus on Bus 335 and presented as one likely move.
- Aggregate run offsets are paired inside the same route + bus; aggregate bus offsets are paired inside the same route + run.
- Exact date + timestamp + bus + fare-category transaction matching remains the confirmation layer.

Example interpretation:

```text
Missing from GFL: 8
Extra in GFL: 0
Net GFL - Legacy: -8
Same riders, different assignment: 39
```

The first three numbers affect systemwide ridership. The assignment number does not.

## Supported reports

Required:

1. **Genfare Link Raw Data** *(Ridership tab)* — CSV
2. **GDS Legacy Event Summary Data** *(Event Report > RouteSum Report)* — PDF, CSV, or TXT

Strongly recommended:

3. **GDS Legacy Transaction Detail** *(Transaction Report > Transaction Detail Report)* — CSV

GDS EventSum TXT exports are supported, including labeled route, Key, Key-ridership, and TTP sections.

Revenue is intentionally excluded.

## Investigation flow

1. Overall Legacy vs GFL ridership.
2. Day cards across the page; more than six dates use a horizontal date strip.
3. Open one day to see the actual missing/extra vs assignment-only summary.
4. Compare Keys/TTPs/Presets.
5. Inspect route/run/bus location differences.
6. Review aggregate offsets as **unconfirmed clues only**.
7. Use exact transaction matching to confirm true missing/extra riders or assignment differences.

## Large-file design

DuckDB performs the main CSV scans and aggregations. Transaction matching performs a targeted second scan only for discrepant day/category pairs, so raw files do not need to be fully loaded into pandas.

## Railway deployment

The project includes `Dockerfile` and `railway.toml`.

```bash
git add .
git commit -m "Clarify missing vs assignment differences V4.6"
git push
```

If Railway is linked to the GitHub repository, it should redeploy automatically.

## External AI review

No AI agent runs inside the app. V4.6 exports JSON evidence and a review prompt. The deterministic comparison remains the source of truth for arithmetic, and the AI instructions explicitly distinguish true count issues from assignment-only differences.
