"""
update_adc_keywords.py
----------------------
1. Load updated ADC keyword Excel (ALL 5 alias columns, deduped)
2. Replace ADC dept_keywords in DB
3. Re-tag Primary Drug for all ADC organized_trials
4. Re-classify ADC unmatched_log: trials now matching keywords -> new_candidates_log
"""

import re
import db
from pathlib import Path
from datetime import date

DEPT       = "ADC"
TODAY      = str(date.today())
EXCEL_PATH = Path(r"C:\Users\LAPTOP\Downloads\ADC\ADC_Assets_With_Alias_Names__July2026 (1).xlsx")


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_xlsx_all_aliases(path: Path) -> list[dict]:
    """Read Excel with 1 Drug Name column + up to N Alias Name columns.
    Each alias cell may itself be pipe-separated. Deduplicates case-insensitively."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    headers   = [str(h or '').strip() for h in rows[0]]
    drug_col  = 0
    alias_cols = [i for i, h in enumerate(headers) if 'alias' in h.lower()]

    records = []
    for row in rows[1:]:
        drug = str(row[drug_col] or '').strip()
        if not drug or drug.lower() in ('none', 'drug name', '-', 'nan'):
            continue

        seen_lower: set[str] = set()
        unique_aliases: list[str] = []
        for ci in alias_cols:
            cell_val = str(row[ci] or '').strip() if len(row) > ci and row[ci] else ''
            if not cell_val or cell_val.lower() in ('-', 'none', 'nan', ''):
                continue
            for part in cell_val.split('|'):
                part = part.strip()
                if part and part.lower() not in ('-', 'nan', '', drug.lower()) and part.lower() not in seen_lower:
                    seen_lower.add(part.lower())
                    unique_aliases.append(part)

        records.append({
            'drug_name':   drug,
            'alias_names': ' | '.join(unique_aliases),
        })
    return records


def _compile_drug_patterns(drugs: list[tuple]) -> list[tuple]:
    compiled = []
    for drug_name, aliases in drugs:
        terms = [t for t in ([drug_name] + aliases) if t and t != '-']
        if not terms:
            continue
        pattern = '|'.join(
            r'(?<![A-Za-z0-9])' + re.escape(t) + r'(?![A-Za-z0-9])' for t in terms
        )
        compiled.append((drug_name, re.compile(pattern, re.IGNORECASE)))
    return compiled


def tag_primary_drug(interventions: str, compiled: list[tuple]) -> str:
    if not interventions or not compiled:
        return ''
    matched = []
    for drug_name, pat in compiled:
        if pat.search(interventions):
            matched.append(drug_name)
    return ' | '.join(matched)


def matches_keywords(trial: dict, keywords: list[str]) -> bool:
    if not keywords:
        return False
    haystack = ' | '.join([
        trial.get('title', '') or '',
        trial.get('conditions', '') or '',
        trial.get('interventions', '') or '',
        trial.get('sponsor', '') or '',
    ]).lower()
    for kw in keywords:
        if re.search(r'\b' + re.escape(kw) + r'\b', haystack):
            return True
    return False


# ── Step 1: Load Excel ────────────────────────────────────────────────────────

print("=" * 60)
print("Step 1: Loading Excel (all alias columns)...")
records = load_xlsx_all_aliases(EXCEL_PATH)
with_aliases = sum(1 for r in records if r['alias_names'])
print(f"  Loaded {len(records)} drugs")
print(f"  Drugs with at least 1 alias: {with_aliases}")

# Show a few examples
examples = [r for r in records if r['alias_names']][:5]
print("  Sample:")
for r in examples:
    aliases_preview = r['alias_names'][:80] + ('...' if len(r['alias_names']) > 80 else '')
    print(f"    {r['drug_name']}  ->  {aliases_preview}")


# ── Step 2: Replace ADC dept_keywords in DB ──────────────────────────────────

print("\nStep 2: Replacing ADC dept_keywords in DB...")
with db._cur() as cur:
    cur.execute("DELETE FROM dept_keywords WHERE dept = %s", (DEPT,))
print("  Cleared existing ADC keywords")

rows_to_insert = [{'dept': DEPT, 'drug_name': r['drug_name'], 'alias_names': r['alias_names']} for r in records]
db._bulk_insert('dept_keywords', rows_to_insert, batch_size=500)
print(f"  Inserted {len(rows_to_insert)} ADC keyword rows")

# Verify
kws_in_db = db.load_dept_keywords(DEPT)
print(f"  Verified: {len(kws_in_db)} rows now in dept_keywords for ADC")


# ── Step 3: Re-tag Primary Drug in organized_trials ──────────────────────────

print("\nStep 3: Re-tagging Primary Drug for all ADC organized_trials...")
drug_rows = db.load_dept_keywords(DEPT)
drugs: list[tuple] = []
for row in drug_rows:
    name = str(row.get('drug_name', '') or '').strip()
    if not name or name in ('nan', '-'):
        continue
    alias_raw = str(row.get('alias_names', '') or '').strip()
    aliases: list[str] = []
    if alias_raw and alias_raw not in ('-', 'nan', ''):
        for a in alias_raw.split('|'):
            a = a.strip()
            if a and a not in ('-', ''):
                aliases.append(a)
    drugs.append((name, aliases))

compiled = _compile_drug_patterns(drugs)
interventions_map = db.load_all_interventions(DEPT)
updates = {nct_id: tag_primary_drug(iv, compiled) for nct_id, iv in interventions_map.items()}
db.batch_update_primary_drug(DEPT, updates)
tagged = sum(1 for v in updates.values() if v)
print(f"  Tagged {tagged}/{len(updates)} trials with Primary Drug")


# ── Step 4: Re-classify pending unmatched_log trials ─────────────────────────

print("\nStep 4: Re-classifying ADC unmatched_log...")

# Build full keyword list
seen_kw: set[str] = set()
all_kws: list[str] = []
for row in db.load_dept_keywords(DEPT):
    drug = str(row.get('drug_name', '') or '').strip()
    if drug and drug.lower() not in ('nan', '-'):
        t = drug.lower()
        if t not in seen_kw:
            seen_kw.add(t); all_kws.append(t)
    alias_raw = str(row.get('alias_names', '') or '').strip()
    if alias_raw and alias_raw not in ('-', 'nan', ''):
        for a in alias_raw.split('|'):
            a = a.strip().lower()
            if a and a not in ('-', '') and a not in seen_kw:
                seen_kw.add(a); all_kws.append(a)
for term in db.load_dept_general_terms(DEPT):
    t = str(term or '').strip().lower()
    if t and t not in seen_kw:
        seen_kw.add(t); all_kws.append(t)
print(f"  Total keywords (drug names + aliases + general): {len(all_kws)}")

# Load pending unmatched trials
with db._cur() as cur:
    cur.execute("""
        SELECT nct_id, title, conditions, interventions, sponsor,
               recruitment_status, last_seen_date, link
        FROM unmatched_log
        WHERE dept = %s AND decision = 'Pending'
    """, (DEPT,))
    unmatched = cur.fetchall()

print(f"  Pending unmatched trials: {len(unmatched)}")

now_match = [t for t in unmatched if matches_keywords(t, all_kws)]
print(f"  Now matching with updated keywords: {len(now_match)}")

if now_match:
    with db._cur() as cur:
        moved = 0
        for t in now_match:
            run_date = str(t['last_seen_date']) if t.get('last_seen_date') else TODAY
            cur.execute("""
                INSERT INTO new_candidates_log
                    (nct_id, dept, run_date, decision, title, conditions,
                     interventions, sponsor, recruitment_status, link)
                VALUES (%s, %s, %s, 'Pending', %s, %s, %s, %s, %s, %s)
                ON CONFLICT (nct_id, dept, run_date) DO NOTHING
            """, (
                t['nct_id'], DEPT, run_date,
                t.get('title'), t.get('conditions'), t.get('interventions'),
                t.get('sponsor'), t.get('recruitment_status'), t.get('link'),
            ))
            cur.execute(
                "DELETE FROM unmatched_log WHERE nct_id = %s AND dept = %s",
                (t['nct_id'], DEPT)
            )
            moved += 1
    print(f"  Moved {moved} trials: unmatched_log -> new_candidates_log")
    print("  NCTs moved:")
    for t in now_match:
        title_preview = (t.get('title') or '')[:70]
        print(f"    {t['nct_id']}  {title_preview}")
else:
    print("  No reclassification needed — unmatched bucket is clean")


# ── Step 5: Re-check new_candidates_log (Pending) ────────────────────────────

print("\nStep 5: Re-checking ADC new_candidates_log (Pending)...")

with db._cur() as cur:
    cur.execute("""
        SELECT DISTINCT ON (nct_id) nct_id, title, conditions, interventions, sponsor,
               recruitment_status, run_date, link
        FROM new_candidates_log
        WHERE dept = %s AND decision = 'Pending'
        ORDER BY nct_id, run_date DESC
    """, (DEPT,))
    candidates = cur.fetchall()

print(f"  Pending new_candidates_log trials: {len(candidates)}")

still_match   = [t for t in candidates if matches_keywords(t, all_kws)]
no_longer_match = [t for t in candidates if not matches_keywords(t, all_kws)]

print(f"  Still matching keywords   : {len(still_match)}")
print(f"  No longer matching (check): {len(no_longer_match)}")
if no_longer_match:
    print("  Trials that no longer keyword-match (may have been added manually):")
    for t in no_longer_match:
        print(f"    {t['nct_id']}  {(t.get('title') or '')[:70]}")

print("\n" + "=" * 60)
print("Done!")
