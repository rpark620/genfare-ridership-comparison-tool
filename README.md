# Genfare Ridership Reconciler V4

Ridership-only reconciliation for GenfareLink versus Legacy Genfare reports.

V4 changes the output from a flat findings list into a progressive investigation workflow:

1. **Raw overall difference** — authoritative Legacy ROUTESUM ridership versus raw GFL ridership.
2. **By-day drill-down** — identifies which date(s) contain the discrepancy.
3. **Key/TTP/Preset by day** — compares Legacy fare events with GFL ridership-export records and rider contribution. This distinguishes non-rider categories, configuration/ridership-rule differences, and Legacy display inconsistencies.
4. **Route → Run → Bus localization** — shows where the category difference lands operationally and detects equal-and-opposite movements consistent with route/run/bus reassignment or Legacy Data Edit.
5. **Targeted transaction matching** — scans only discrepant day/category pairs and matches on timestamp + bus + fare category to distinguish assignment changes from missing/extra riders.
6. **External-AI export** — JSON evidence package plus a ready-to-use AI review prompt. There is no AI agent inside the app.

## Preferred inputs

### Required
- **GFL Ridership Raw Data CSV**
- **Legacy EVENT SUMMARY / ROUTESUM** — PDF preferred

### Strongly recommended
- **Legacy TRANSACTION DETAIL CSV**

Transaction Detail is needed for the complete by-day Key/TTP, route/run/bus, and transaction-match workflow.

## Ridership behavior

There are no manual ridership-rule fields. The app infers Legacy ridership behavior from ROUTESUM totals/category counts. The detailed Legacy key-ridership display is treated as evidence, not absolute truth. This allows the app to flag cases such as a key showing 0 on the detailed ridership page while overall/route totals prove that key contributes riders.

## Large-file design

- DuckDB scans and aggregates the large GFL and Legacy CSVs.
- Full raw transaction files are not loaded into pandas.
- Day/category/route/run/bus aggregate tables are retained in memory.
- The final transaction-matching pass uses chunked pandas reads and only keeps rows belonging to already-discrepant day/category pairs.
- Upload limit is configured to 1 GB per file in `.streamlit/config.toml`.

## Local run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

On macOS/Linux use `source .venv/bin/activate`.

## Deploy on Railway

V4 includes a `Dockerfile` and `railway.toml`.

1. Push the extracted project files to GitHub.
2. In Railway, create a new project from the GitHub repo.
3. Railway will build using the included Dockerfile.
4. Generate a public domain for the service.
5. For large report processing, use a paid Railway plan and set sensible per-replica CPU/RAM limits rather than relying on the smallest free allocation.

No API keys or secrets are required.

## External AI workflow

After reconciliation, download:

- `ridership_ai_evidence_v4.json`
- `ridership_ai_review_prompt_v4.md`

Upload both to ChatGPT or another approved AI tool. The deterministic app remains the source of truth for arithmetic; the AI is asked only to interpret the evidence and propose next checks.
