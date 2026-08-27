import re
import db

DEPT = "ADC"

# Build same keyword regex as reclassify
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

combined_pattern = re.compile(
    r'(?<![A-Za-z0-9])(?:' +
    '|'.join(re.escape(k) for k in sorted(all_kws, key=len, reverse=True)) +
    r')(?![A-Za-z0-9])',
    re.IGNORECASE
)

# Fetch only indication='' unmatched
with db._cur() as cur:
    cur.execute("""
        SELECT nct_id, title, conditions, interventions, sponsor
        FROM unmatched_log
        WHERE dept = %s AND indication = '' AND decision = 'Pending'
    """, (DEPT,))
    rows = cur.fetchall()

print(f"Unmatched (ADC, indication='', Pending): {len(rows)}")

still_match = []
for t in rows:
    haystack = ' | '.join([
        t.get('title', '') or '',
        t.get('conditions', '') or '',
        t.get('interventions', '') or '',
        t.get('sponsor', '') or '',
    ])
    if combined_pattern.search(haystack):
        still_match.append(t)

print(f"Still matching keywords : {len(still_match)}")
if still_match:
    print("Leaking trials:")
    for t in still_match[:10]:
        print(f"  {t['nct_id']}  {(t.get('title') or '')[:70]}")
else:
    print("Clean — no ADC keyword matches in unmatched bucket.")
