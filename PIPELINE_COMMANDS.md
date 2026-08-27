# NCT Pipeline — Commands Reference

> Working directory for all commands: `C:\Users\LAPTOP\Downloads\NCT_Combined_Pipeline`
>
> Open a terminal there first:
> ```powershell
> cd C:\Users\LAPTOP\Downloads\NCT_Combined_Pipeline
> $env:PYTHONUTF8=1   # prevents Unicode errors on Windows — run this once per session
> ```

---

## 1. Run Pipelines

### Run everything (ADC asset + all ADC indications) — most common weekly run
```powershell
python combined_pipeline.py ADC
```
- Fetches CT.gov once, classifies trials across asset pipeline + all indication pipelines
- Writes to: `new_candidates_log`, `unmatched_log`, `modified_log`, `field_changes_log`, `run_history`

### Run all departments (ADC + ASMB) sequentially
```powershell
python run_all.py
```

### Run specific departments only
```powershell
python run_all.py ADC ASMB
```

### Run a single indication only (for debugging)
```powershell
python combined_pipeline.py ADC --indication "Breast Cancer"
python combined_pipeline.py ADC --indication "NSCLC"
python combined_pipeline.py ADC --indication "Ovarian Cancer"
python combined_pipeline.py ADC --indication ""    # asset pipeline only
```

### Run ASMB department
```powershell
python combined_pipeline.py ASMB
```

---

## 2. Keyword Management

### Replace all ADC keywords from Excel (full replacement)
```powershell
python replace_keywords.py
```
- Reads `ADC_DB_Asset List-21Aug2026.xlsx` → "Matched DB & Excel (2066)" sheet
- Deletes all existing ADC `dept_keywords`, inserts fresh rows

### Retag Primary Drug field on all organized trials
```powershell
python retag_primary_drug.py
```
- Re-runs drug keyword matching on every trial in `organized_trials` for ADC
- Run after replacing keywords

### Reclassify unmatched trials against new keywords
```powershell
python reclassify_adc.py
```
- Scans ADC unmatched bucket against current keywords
- Moves matches → `new_candidates_log` (Pending)
- Moves stale Pending candidates that no longer match → back to `unmatched_log`

### One-time cleanup: move stale Pending candidates to unmatched
```powershell
python cleanup_stale_candidates.py
```

### Load indication keywords into DB from file
```powershell
python load_indication_keywords.py
```

---

## 3. Verification & Checks

### Check how many trials are in each bucket per indication
```powershell
python check_candidates_count.py
```

### Verify unmatched bucket has no keyword matches (should all be 0)
```powershell
python verify_unmatched_clean.py
```

### Check impact of keyword changes on indication pipelines
```powershell
python check_indication_impact.py
```

### Check for duplicate trials
```powershell
python check_duplicates.py
```

### Check DB constraints
```powershell
python check_constraints.py
```

### List all indications for a dept
```powershell
python list_indications.py
```

### Test DB connection
```powershell
python test_conn.py
```

### Pre-flight checks before a run
```powershell
python preflight.py
```

---

## 4. Backfill / Migration (one-time use)

### Backfill field changes for historical NCTs
```powershell
python backfill_field_changes.py
```

### Backfill version pairs
```powershell
python backfill_version_pairs.py
```

---

## 5. Dashboard (GitHub Push)

> Working directory: `C:\Users\LAPTOP\Downloads\nct-dashboard`

### Push specific changed files to GitHub
```powershell
cd C:\Users\LAPTOP\Downloads\nct-dashboard
.\_push_updates.ps1
```

### Push all files (full sync)
```powershell
.\_push_to_github.ps1
```

---

## 6. Pipeline Flow Summary

```
CT.gov REST API scan (LastUpdatePostDate range, ~8,500 NCTs/week)
│
├── Trial in tracking_list + date CHANGED  →  modified_log
├── Trial in tracking_list + date SAME     →  skip (stale)
├── Trial NOT in list + keyword MATCH      →  new_candidates_log (Pending)
└── Trial NOT in list + no match           →  unmatched_log (Pending)

Field-Level Diff (per tracked NCT):
├── Same version   →  skip
├── New version    →  field_changes_log + version_cache update
└── Different ID   →  auto-replace in tracking_list → canonical_changes_log
```

---

## 7. Keyword Matching Rules

| Dept | Asset Pipeline (`indication = ''`) | Indication Pipeline |
|---|---|---|
| ADC | Drug keywords + General terms | Indication-specific keywords only |
| ASMB | Drug keywords + General terms | Indication-specific keywords only |

- **Primary Drug tagging**: Always uses drug keywords, regardless of pipeline
- Matching: exact case-insensitive substring on title / conditions / interventions
- Alias separator: `|` (pipe), also handles `,`
- Alias value `-` is ignored

---

## 8. Common Issues

| Problem | Fix |
|---|---|
| `UnicodeEncodeError` on Windows terminal | Run `$env:PYTHONUTF8=1` first |
| `ON CONFLICT` error on new_candidates_log | Constraint is `(nct_id, dept, indication, run_date)` — include all 4 |
| `ANY(%s)` fails with Python list | Use `IN %s` with `tuple()` instead |
| `execute_values` placeholder error | Use `VALUES %s` (single), not `VALUES (%s, %s, ...)` |
| Version pairs blocked by RLS | Run: `GRANT BYPASS RLS TO pranay` in DB |
| BOM in DB URL file | Open with `encoding='utf-8-sig'` |
