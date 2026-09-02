# NCT Combined Pipeline

A Python pipeline that discovers, classifies, and tracks clinical trials from [ClinicalTrials.gov](https://clinicaltrials.gov). Feeds the [NCT Tracking Dashboard](https://github.com/PranayBhumagouni283/nct-dashboard).

## What It Does

Each pipeline run:
1. Queries the CT.gov REST API for trials matching department-specific drug keywords
2. Classifies trials into buckets: **new candidates**, **modified**, **unmatched**, **field changes**
3. Diffs every tracked trial field-by-field against the previous version
4. Writes results to PostgreSQL
5. Sends email alert with run summary

## Departments

| Department | Asset Keywords | Tracked Trials | Indications |
|---|---|---|---|
| ADC | 2,076 | 2,725 | Breast Cancer, Cervical Cancer, Endometrial Cancer, HNSCC, NSCLC, Ovarian Cancer, Pancreatic Cancer, Prostate Cancer, SCLC |
| Infectious Diseases | 541 | 832 | CMV, HSV, Influenza, RSV, VZV |
| Liver Diseases | 454 | 530 | HBV, HDV, PBC, PSC |

The pipeline is fully department-agnostic — keywords and indications are loaded from the database by department name.

## Setup

### Prerequisites

- Python 3.10+
- PostgreSQL database with the NCT schema (see `schema.sql`)
- ClinicalTrials.gov API access (public, no key needed)

### Install

```bash
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env — add your DATABASE_URL

cp smtp_config.json.example smtp_config.json
# Edit smtp_config.json — add your SMTP credentials for email alerts
```

## Running the Pipeline

```bash
# Run all departments sequentially
python run_all.py

# Run a specific department (asset + all indications — single CT.gov fetch)
python combined_pipeline.py ADC
python combined_pipeline.py "Liver Diseases"
python combined_pipeline.py "Infectious Diseases"

# Run one specific indication only (debug mode)
python combined_pipeline.py ADC --indication "Breast Cancer"
python combined_pipeline.py "Infectious Diseases" --indication "CMV"
```

> **Windows tip:** Set `$env:PYTHONUTF8=1` before running to prevent Unicode errors.

## Project Structure

```
combined_pipeline.py    # Main orchestrator — CT.gov fetch, classify, diff
db.py                   # PostgreSQL interface (all reads/writes)
norm.py                 # Normalization — org aliases, condition cleanup
run_all.py              # Run all departments sequentially
preflight.py            # Pre-run validation checks
resend_alert.py         # Resend email alert for a past run
list_indications.py     # List all indications configured in DB
master_dashboard.py     # Generate Excel analytics report

schema.sql              # Full database schema (run once to set up)
complete_schema.sql     # Schema with all indexes and constraints
rebuild_schema.sql      # Drop + recreate schema
```

## Database Schema

The pipeline writes to a PostgreSQL schema named `"CT"`. Key tables:

| Table | Purpose |
|---|---|
| `organized_trials` | Full trial data snapshot — 62 CT.gov fields per NCT ID |
| `tracking_list` | Per-department watchlist: `(nct_id, dept, indication)` |
| `new_candidates_log` | Newly discovered trials awaiting review |
| `unmatched_log` | Trials with no keyword match — reviewed for keyword gaps |
| `modified_log` | Tracked trials whose CT.gov update date changed |
| `field_changes_log` | Append-only log of every field diff between versions |
| `version_cache` | Latest version state per NCT ID |
| `run_history` | One row per pipeline run with timing and counts |
| `dept_keywords` | Drug keyword list with aliases per department |
| `dept_indications` | Indication-specific keyword configuration |
| `dept_general_terms` | General (non-drug) matching terms per department |

Run `schema.sql` once against your PostgreSQL database to create all tables.

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |

## Department Data (local-only, gitignored)

Each department has its own folder under `_local_data/` with:
- `output/` — generated Excel reports per run
- `alert_config.json` — email recipients for that department
- `state.json` — last run date and ETag (auto-created)

These folders are excluded from the repository.
