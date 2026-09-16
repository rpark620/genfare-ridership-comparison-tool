# Genfare Report Reconciler V2

Streamlit tool for reconciling GenfareLink (GFL) raw data against Legacy Genfare reports.

## Preferred inputs

### Required
1. **GFL Ridership Raw Data CSV**
2. **Legacy EVENT SUMMARY / ROUTESUM** — PDF preferred; CSV accepted

### Strongly recommended
3. **Legacy TRANSACTION DETAIL CSV** — enables run/trip drill-down and stronger route-reassignment evidence

### Optional
4. **GFL Revenue Raw Data CSV** — only an independent revenue cross-check. V2 normally uses `Amount Charged` in the GFL Ridership file.

## Major V2 changes

- No manual ridership-rule inputs.
- Automatically infers which Legacy Key/TTP categories contribute ridership by fitting category counts to Legacy route ridership totals.
- Detects Legacy internal display inconsistencies, such as a key showing zero on the detailed key-ridership page while overall/route totals require that key to contribute ridership.
- Uses DuckDB aggregate scans for large CSVs instead of loading full transaction files into pandas.
- Keeps large raw files on disk and brings only aggregate result tables into memory.
- GFL Revenue Raw Data is optional.
- Route/run/trip and likely Data Edit/reassignment diagnostics retained.
- Upload limit configured to 1 GB per file for large datasets.

## Local run

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

Deploy the GitHub repository with `app.py` as the main file.

No API keys or secrets are required.

## Large-file design

V2 saves uploads to temporary files and uses DuckDB to scan/group CSVs. This is substantially more memory-efficient than V1's full pandas load. Streamlit still receives the uploaded file itself, so practical limits depend on Streamlit hosting RAM, upload bandwidth, and file size. The code is designed for million-row-class comparisons and can spill DuckDB work to disk rather than retaining every raw transaction as a dataframe.

## Important interpretation note

Confidence percentages are evidence scores generated from deterministic matching/reconciliation logic. They are not AI-generated probabilities.
