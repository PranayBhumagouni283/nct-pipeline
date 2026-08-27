"""
cleanup_stale_candidates.py
---------------------------
One-time cleanup: move Pending new_candidates (ADC, indication='') that no longer
match the current keyword set back to unmatched_log.

Run AFTER replace_keywords.py and reclassify_adc.py.
Safe to re-run — unmatched_log uses ON CONFLICT DO UPDATE (last_seen_date only).
"""

import re
import db
from datetime import date

DEPT      = "ADC"
INDICATION = ""
TODAY     = str(date.today())

# ── Build keyword regex (same as reclassify_adc.py) ──────────────────────────
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
sorted_kws = sorted(all_kws, key=len, reverse=True)
combined_pattern = re.compile(
    r'(?<![A-Za-z0-9])(?:' +
    '|'.join(re.escape(k) for k in sorted_kws) +
    r')(?![A-Za-z0-9])',
    re.IGNORECASE
)
print("  Regex compiled.\n")


def matches(trial: dict) -> bool:
    haystack = ' | '.join([
        trial.get('title', '') or '',
        trial.get('conditions', '') or '',
        trial.get('interventions', '') or '',
        trial.get('sponsor', '') or '',
    ])
    return bool(combined_pattern.search(haystack))


# ── Fetch distinct Pending new_candidates ─────────────────────────────────────
print("Fetching Pending new_candidates_log (ADC, indication='')...")
with db._cur() as cur:
    cur.execute("""
        SELECT DISTINCT ON (nct_id)
               nct_id, title, conditions, mesh_conditions, interventions,
               sponsor, collaborators, phase, study_type, enrollment,
               recruitment_status, link, run_date
        FROM new_candidates_log
        WHERE dept = %s AND indication = %s AND decision = 'Pending'
        ORDER BY nct_id, run_date DESC
    """, (DEPT, INDICATION))
    candidates = cur.fetchall()

print(f"  Distinct Pending candidates : {len(candidates)}")

no_longer_match = [t for t in candidates if not matches(t)]
print(f"  No longer matching keywords : {len(no_longer_match)}")

if not no_longer_match:
    print("\nNothing to move. Done.")
    exit()

# ── Move to unmatched_log ─────────────────────────────────────────────────────
print(f"\nMoving {len(no_longer_match)} trials → unmatched_log...")

nct_ids_to_move = [t['nct_id'] for t in no_longer_match]

with db._cur() as cur:
    # Insert into unmatched_log (first_seen_date = last_seen_date = run_date)
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
    import psycopg2.extras
    rows = [
        (
            t['nct_id'], DEPT, INDICATION,
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
    print(f"  Inserted/updated {len(rows)} rows in unmatched_log")

    # Delete ALL Pending rows for these nct_ids from new_candidates_log
    cur.execute("""
        DELETE FROM new_candidates_log
        WHERE dept = %s AND indication = %s AND decision = 'Pending'
          AND nct_id IN %s
    """, (DEPT, INDICATION, tuple(nct_ids_to_move)))
    print(f"  Deleted Pending rows from new_candidates_log")

print(f"\nDone. {len(no_longer_match)} stale candidates moved back to unmatched_log.")
