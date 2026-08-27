"""
reclassify_adc.py
-----------------
Fast re-classification of both ADC buckets against updated keywords.
Uses ONE combined regex per trial (vs 7094 separate searches).

Steps:
  4. Re-check unmatched_log (Pending)  -> move matches to new_candidates_log
  5. Re-check new_candidates_log (Pending) -> report match status
"""

import re
import db
from datetime import date

DEPT  = "ADC"
TODAY = str(date.today())


# ── Build keyword list ────────────────────────────────────────────────────────

print("Loading keywords from DB...")
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

print(f"  Total keywords: {len(all_kws)}")

# Build ONE combined regex: \b(?:kw1|kw2|...)\b
# Sort by length descending so longer phrases are tried first
sorted_kws = sorted(all_kws, key=len, reverse=True)
combined_pattern = re.compile(
    r'(?<![A-Za-z0-9])(?:' +
    '|'.join(re.escape(k) for k in sorted_kws) +
    r')(?![A-Za-z0-9])',
    re.IGNORECASE
)
print("  Combined regex compiled.")


def matches(trial: dict) -> bool:
    haystack = ' | '.join([
        trial.get('title', '') or '',
        trial.get('conditions', '') or '',
        trial.get('interventions', '') or '',
        trial.get('sponsor', '') or '',
    ])
    return bool(combined_pattern.search(haystack))


# ── Step 4: Re-check unmatched_log ───────────────────────────────────────────

print("\nStep 4: Re-checking ADC unmatched_log (Pending)...")

with db._cur() as cur:
    cur.execute("""
        SELECT nct_id, title, conditions, interventions, sponsor,
               recruitment_status, last_seen_date, link
        FROM unmatched_log
        WHERE dept = %s AND decision = 'Pending'
    """, (DEPT,))
    unmatched = cur.fetchall()

print(f"  Pending unmatched trials : {len(unmatched)}")

now_match = [t for t in unmatched if matches(t)]
print(f"  Now matching keywords    : {len(now_match)}")

if now_match:
    with db._cur() as cur:
        for t in now_match:
            run_date = str(t['last_seen_date']) if t.get('last_seen_date') else TODAY
            cur.execute("""
                INSERT INTO new_candidates_log
                    (nct_id, dept, indication, run_date, decision, title, conditions,
                     interventions, sponsor, recruitment_status, link)
                VALUES (%s, %s, %s, %s, 'Pending', %s, %s, %s, %s, %s, %s)
                ON CONFLICT (nct_id, dept, indication, run_date) DO NOTHING
            """, (
                t['nct_id'], DEPT, '', run_date,
                t.get('title'), t.get('conditions'), t.get('interventions'),
                t.get('sponsor'), t.get('recruitment_status'), t.get('link'),
            ))
            cur.execute(
                "DELETE FROM unmatched_log WHERE nct_id = %s AND dept = %s",
                (t['nct_id'], DEPT)
            )
    print(f"  Moved {len(now_match)} trial(s): unmatched_log -> new_candidates_log")
    for t in now_match:
        print(f"    {t['nct_id']}  {(t.get('title') or '')[:70]}")
else:
    print("  No reclassification needed — unmatched bucket clean")


# ── Step 5: Re-check new_candidates_log ──────────────────────────────────────

print("\nStep 5: Re-checking ADC new_candidates_log (Pending)...")

with db._cur() as cur:
    cur.execute("""
        SELECT DISTINCT ON (nct_id)
               nct_id, title, conditions, mesh_conditions, interventions,
               sponsor, collaborators, phase, study_type, enrollment,
               recruitment_status, link, run_date
        FROM new_candidates_log
        WHERE dept = %s AND indication = '' AND decision = 'Pending'
        ORDER BY nct_id, run_date DESC
    """, (DEPT,))
    candidates = cur.fetchall()

print(f"  Pending new_candidates_log trials: {len(candidates)}")

still_match     = [t for t in candidates if matches(t)]
no_longer_match = [t for t in candidates if not matches(t)]

print(f"  Still matching keywords   : {len(still_match)}")
print(f"  No longer matching        : {len(no_longer_match)}")

if no_longer_match:
    import psycopg2.extras
    nct_ids_to_move = [t['nct_id'] for t in no_longer_match]
    with db._cur() as cur:
        insert_sql = """
            INSERT INTO unmatched_log
                (nct_id, dept, indication, first_seen_date, last_seen_date,
                 decision, title, conditions, mesh_conditions, interventions,
                 sponsor, collaborators, phase, study_type, enrollment,
                 recruitment_status, link)
            VALUES %s
            ON CONFLICT (nct_id, dept, indication)
            DO UPDATE SET last_seen_date = EXCLUDED.last_seen_date
        """
        rows = [
            (
                t['nct_id'], DEPT, '',
                t.get('run_date') or TODAY, t.get('run_date') or TODAY,
                'Pending',
                t.get('title'), t.get('conditions'), t.get('mesh_conditions'),
                t.get('interventions'), t.get('sponsor'), t.get('collaborators'),
                t.get('phase'), t.get('study_type'), t.get('enrollment'),
                t.get('recruitment_status'), t.get('link'),
            )
            for t in no_longer_match
        ]
        psycopg2.extras.execute_values(cur, insert_sql, rows)
        cur.execute("""
            DELETE FROM new_candidates_log
            WHERE dept = %s AND indication = '' AND decision = 'Pending'
              AND nct_id IN %s
        """, (DEPT, tuple(nct_ids_to_move)))
    print(f"  Moved {len(no_longer_match)} stale candidates → unmatched_log")
else:
    print("  All Pending candidates still match — nothing to move")

print("\nDone!")
