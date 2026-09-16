# Genfare Report Reconciler

Streamlit app for comparing GenfareLink raw ridership/revenue exports against Legacy Genfare reports.

## Preferred reports

### GenfareLink
Upload **both**:
1. Ridership Raw Data CSV
2. Revenue Raw Data CSV

The sample GFL ridership export contains `Amount Charged` and can reproduce the sample revenue total, but the app intentionally asks for the separate revenue export as an independent cross-check.

### Legacy
1. **EVENT SUMMARY (ROUTESUM)** — CSV strongly preferred; PDF accepted as fallback.
2. **TRANSACTION DETAIL REPORT** — CSV strongly recommended for Key/TTP and route/run/trip cause analysis.

`BY ROUTE` is preferred when available. `BY ROUTE-DATE` is accepted for route/date totals.

## Local run

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

1. Create a GitHub repository.
2. Upload all files from this folder, including `app.py`, `parsers.py`, `reconciliation.py`, `exports.py`, and `requirements.txt`.
3. In Streamlit Community Cloud, choose **Create app** / **Deploy an app**.
4. Select your repository and branch.
5. Set the main file path to `app.py`.
6. Deploy.

No secrets or API keys are required for V1.

## Current V1 behavior

- Auto-recognizes the supplied GFL ridership and revenue CSV structures.
- Parses Legacy Transaction Detail CSV from its embedded header row.
- Parses Legacy ROUTESUM CSV Revenue/Ridership sections even when title lines precede the table.
- Accepts Legacy ROUTESUM PDF as a lower-confidence fallback for summary route/revenue/ridership parsing.
- Applies editable ridership business rules.
- Compares overall ridership/revenue, Key/TTP event counts, routes, and derived route/run/trip ridership.
- Detects exact systemwide-matching Key/TTP route offsets as possible route reassignment / Data Edit patterns.
- Produces reconciliation CSV and multi-tab Excel output.
- Uses deterministic evidence-based confidence scores; no AI API is required.

## COLTS profile included

- Keys 2, 3, 4: do **not** count as ridership.
- Keys 8 and D: do count as ridership.
- TTP48 / CHANGE: does **not** count as ridership.

Use **Custom** for other agencies.
