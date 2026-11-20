# NCT Pipeline â€” Release Notes

**Repo:** https://github.com/PranayBhumagouni283/nct-pipeline  
**Entry points:** `python run_all.py` Â· `python combined_pipeline.py <dept>`

---

## 2026-11-20

### Repo Cleanup â€” Extract Clinical Concepts Moved to Root
**Type:** Refactor

**What changed:**
- `_local_scripts/__extract_clinical_concepts.py` moved to repo root as `extract_clinical_concepts.py` â€” it is a runtime dependency of `concept_extraction.py`, not a one-off utility script.
- `concept_extraction.py` import path updated accordingly.
- Previously gitignored under `_local_scripts/` blanket exclusion â€” the pipeline would crash on concept extraction on any fresh clone.

**Files changed:**
```
extract_clinical_concepts.py  (new â€” moved from _local_scripts/)
concept_extraction.py
```

---

### Repo Cleanup â€” Post-Pipeline Scripts Added + CT.gov Dependencies Synced
**Type:** Refactor / Sync

**What changed:**
- `change_parser.py` and `incremental_processor.py` removed from `.gitignore` and pushed to GitHub â€” these run after every pipeline execution to populate `trial_change_events`, `trial_status_history`, `trial_phase_history`, `trial_date_history`.
- `CT_PULL.py`, `NCT_Changes_Tracker.py`, `all_versions_compare.py` synced from local source (`NCT_Record_History_Changes/`) â€” GitHub copies were stale.
- `PIPELINE_COMMANDS.md` deleted â€” commands already documented in `README.md`.
- `README.md` updated: added all new modules to project structure, removed reference to deleted file.
- `.gitignore` updated to unignore `change_parser.py` and `incremental_processor.py`.

**Files changed:**
```
change_parser.py              (added)
incremental_processor.py      (added)
CT_PULL.py                    (synced)
NCT_Changes_Tracker.py        (synced)
all_versions_compare.py       (synced)
README.md
.gitignore
PIPELINE_COMMANDS.md          (deleted)
```

---

## 2026-11-19

### Auto-Approve Already-Tracked Trials at Pipeline Start
**Type:** Feature

**What changed:**
- Added `auto_approve_already_tracked()` in `db.py`: at the start of each pipeline run, any trial sitting in `new_candidates_log` as `Pending` that is already in the `tracking_list` for the same dept+indication is automatically approved and removed from the pending queue.
- Prevents stale "Pending" entries accumulating for trials that have already been accepted.
- Called per-indication inside the pipeline loop so each indication's tracking list is checked independently.

**Files changed:**
```
db.py
combined_pipeline.py
```

---

### Fix â€” Indication Pipelines Use Their Own Tracking List
**Type:** Fix

**What changed:**
- `load_tracking_list` for indication pipelines (e.g. CMV, Influenza) was previously loading the full dept-level tracking list, causing cross-indication duplicates.
- Fixed to filter strictly by `(dept, indication)` so each indication pipeline sees only its own tracked NCTs.

**Files changed:**
```
db.py
```

---

### Fix â€” Duplicate Rows in new_candidates_log Across Runs
**Type:** Fix

**What changed:**
- `insert_new_candidates` was re-inserting `Pending` entries on every run for the same trial, creating duplicate rows.
- Fixed by skipping NCTs that already have any decision (`Approved`, `Rejected`, or `Pending`) in `new_candidates_log` for the same `(dept, indication)`.

**Files changed:**
```
db.py
```

---

## 2026-11-17

### OpenAI API Key Loaded from Database When Not in Environment
**Type:** Feature

**What changed:**
- `concept_extraction.py` now falls back to `CT.app_credentials` DB table to load `OPENAI_API_KEY` when the key is not present in the `.env` file.
- Allows the pipeline to run concept extraction on the server without storing the key in a local `.env`.

**Files changed:**
```
concept_extraction.py
```

---

### Fix â€” Liver Diseases / Infectious Diseases Tracking List Cross-Dept Duplicates
**Type:** Fix

**What changed:**
- `load_tracking_list` for Liver Diseases and Infectious Diseases was using a dept-only filter, causing it to load NCTs from all indications instead of the asset-level list only.
- Fixed to use the correct `(dept, indication='')` filter for the asset pipeline.

**Files changed:**
```
db.py
```

---

### Repo Cleanup â€” Non-Pipeline Files Removed, CT.gov Dependencies Added
**Type:** Cleanup

**What changed:**
- Removed files not required to run the pipeline: `change_parser.py`, `incremental_processor.py`, `conversational_agent.py`, `db_query_handler.py`, `embedding_worker.py`/`v3`, `es_retriever.py`, `import_tracking_ncts.py`, `master_dashboard.py`, `schema.sql`, `rebuild_schema.sql`, `complete_schema.sql`, `create_processing_queue.py`, `chunk_generator_v3.py`, `test_retrieval_v3.py`.
- Added `CT_PULL.py`, `NCT_Changes_Tracker.py`, `all_versions_compare.py` â€” these are direct imports of `combined_pipeline.py` via `sys.path` from `NCT_Record_History_Changes/`.
- `README.md` updated with accurate file structure.

**Files changed:**
```
CT_PULL.py                    (added)
NCT_Changes_Tracker.py        (added)
all_versions_compare.py       (added)
README.md
.gitignore
[13 files removed]
```

---

## 2026-10-19

### Fix â€” KeyError on RealDictCursor Row Access
**Type:** Fix

**What changed:**
- `db.py` was accessing result rows with positional index `r[0]` instead of named key `r['is_new']`, causing a `KeyError` when using `psycopg2.extras.RealDictCursor`.

**Files changed:**
```
db.py
```

---

## 2026-10-09

### Fix â€” upsert_unmatched New Count Always Returning 0
**Type:** Fix

**What changed:**
- `upsert_unmatched` in `db.py` always returned `0` for the new unmatched count due to a logic error in the return value.
- Fixed so the pipeline summary correctly reports how many new unmatched trials were added each run.

**Files changed:**
```
db.py
```

---

## 2026-10-08

### Fix â€” ADC Ref Table Whitespace / Newline in NCT Identifiers
**Type:** Fix

**What changed:**
- ADC `ct.adc_trial_refs` identifiers had trailing whitespace and embedded newlines (`\n`) causing NCT ID lookups to silently fail â€” trials present in the ref table were not being matched.
- Added `trim_adc_ref_identifiers()` using `REGEXP_REPLACE` to strip all whitespace including newlines from ref table identifiers. Called at ADC pipeline startup.
- `load_tracking_list` also strips NCT IDs on load as an additional safety layer.
- `cleanup_removed_adc_trials` fixed to preserve indication-specific tracking rows when removing asset-level entries.

**Files changed:**
```
db.py
combined_pipeline.py
```

---

## 2026-10-07

### ADC Ref Table as Source of Truth for Tracking List
**Type:** Feature

**What changed:**
- `load_tracking_list` for ADC now reads directly from `ct.adc_trial_refs` instead of the SQL `tracking_list` table, making `ct.adc_trial_refs` the single source of truth for all ADC asset-level tracked trials.
- Added `cleanup_removed_adc_trials()`: at the start of each ADC run, NCTs in `organized_trials` that are no longer in `ct.adc_trial_refs` are cascade-deleted from all dependent tables (`tracking_list`, `new_candidates_log`, `modified_log`, `field_changes_log`, etc.).
- `norm.py` updated with improved org alias resolution and unresolved sponsor flushing.
- `run_all.py` updated to load departments dynamically from DB via `db.load_departments()`.
- `requirements.txt` updated.

**Files changed:**
```
db.py
combined_pipeline.py
norm.py
run_all.py
requirements.txt
```

---

## 2026-09-24

### Elasticsearch v3 â€” 6 New Metadata Fields in Index Mapping
**Type:** Feature

**What changed:**
- Added 6 new metadata fields to the `clinical_trials_semantic_v3` ES index mapping.
- `_build_es_doc` updated to populate the new fields.
- Incremental ES metadata backfill script added for existing indexed documents.

**Files changed:**
```
chunk_generator_v3.py
```

---

## 2026-09-18

### Initial Release â€” NCT Combined Pipeline
**Type:** Initial Release

**What changed:**
- Initial commit: `combined_pipeline.py` (unified multi-dept orchestrator), `db.py`, `norm.py`, `concept_extraction.py`, `run_all.py`, `preflight.py`, `list_indications.py`, `resend_alert.py`, `reset_pipeline_state.py`.
- Concept extraction fix: `load_dict_rows` returns grouped dict (keyed by concept), not a flat list â€” caused all regex passes to silently match nothing.
- `concept_extraction.py` updated to use `OPENAI_API_KEY` from `.env`; `.env.example` updated with key reference.
- Non-pipeline one-off scripts removed from repo; kept locally under `_local_scripts/`.

**Files changed:**
```
combined_pipeline.py
db.py
norm.py
concept_extraction.py
run_all.py
preflight.py
list_indications.py
resend_alert.py
reset_pipeline_state.py
requirements.txt
.env.example
.gitignore
README.md
```
