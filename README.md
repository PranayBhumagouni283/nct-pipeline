# NCT Combined Pipeline

A Python pipeline that discovers, classifies, and tracks clinical trials from [ClinicalTrials.gov](https://clinicaltrials.gov). Feeds the [NCT Tracking Dashboard](https://github.com/PranayBhumagouni283/nct-dashboard).

## What It Does

Each pipeline run:
1. Queries the CT.gov REST API for trials matching department-specific drug keywords
2. Classifies trials into buckets: **new candidates**, **modified**, **unmatched**, **field changes**
3. Diffs every tracked trial field-by-field against the previous version
4. Writes results to PostgreSQL and generates Excel summary reports
5. Sends email alert with run summary

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

### Test connection

```bash
python test_conn.py
```

## Running the Pipeline

```bash
# Run all departments sequentially
python run_all.py

# Run a specific department
python combined_pipeline.py ADC

# Run a specific department + indication
python combined_pipeline.py ADC --indication "Breast Cancer"

# Run all indications for a department
python run_all_indications.py ADC
```

## Project Structure

```
combined_pipeline.py    # Main orchestrator — CT.gov fetch, classify, diff
db.py                   # PostgreSQL interface (all reads/writes)
norm.py                 # Normalization — org aliases, condition cleanup
run_all.py              # Run all departments sequentially

schema.sql              # Full database schema (run once to set up)
rebuild_schema.sql      # Drop + recreate schema

Backfill scripts/       # One-time data repair operations
  backfill_*.py

Keyword management/
  update_adc_keywords.py
  manage_aliases.py
  load_indication_keywords.py

Migration scripts/      # Historical data migrations (one-time use)
  migrate_*.py

Validation scripts/
  check_*.py            # Schema, constraint, and data quality checks
  preflight.py          # Pre-run validation

Utilities/
  add_to_tracking.py    # Manually add NCT IDs to watchlist
  cleanup_stale_candidates.py
  reclassify_adc.py
  master_dashboard.py   # Generate Excel analytics report
  resend_alert.py       # Resend email alert for a run
```

## Database Schema

The pipeline writes to a PostgreSQL schema named `"CT"`. Key tables:

| Table | Purpose |
|---|---|
| `organized_trials` | Full trial data snapshot — 62 CT.gov fields per NCT ID |
| `tracking_list` | Per-department watchlist: `(nct_id, dept, indication)` |
| `new_candidates_log` | Newly discovered trials awaiting review |
| `unmatched_log` | Trials that no longer match any keyword |
| `field_changes_log` | Append-only log of every field diff between runs |
| `version_cache` | Latest version state per NCT ID |
| `trial_history` | Full version history per NCT |
| `run_history` | One row per pipeline run with timing and counts |
| `dept_keywords` | Drug keyword list with aliases per department |
| `dept_indications` | Indication-specific keyword configuration |

Run `schema.sql` once against your PostgreSQL database to create all tables.

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |

## Department Data

Each department (`ADC`, `ASMB`, etc.) has its own folder with:
- `keywords/` — drug keyword Excel files
- `output/` — generated Excel reports per run
- `alert_config.json` — email recipients for that department

These folders are local-only and excluded from the repository.
