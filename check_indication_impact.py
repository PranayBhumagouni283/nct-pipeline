import re
import db

DEPT = "ADC"

# Build keyword regex
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

def matches(t):
    haystack = ' | '.join([t.get('title','') or '', t.get('conditions','') or '',
                            t.get('interventions','') or '', t.get('sponsor','') or ''])
    return bool(combined_pattern.search(haystack))

with db._cur() as cur:
    # 1. Unmatched per indication — how many still have keyword-matching trials?
    print("=== Unmatched bucket per indication ===")
    cur.execute("""
        SELECT indication, COUNT(*) as cnt
        FROM unmatched_log
        WHERE dept = %s AND decision = 'Pending' AND indication != ''
        GROUP BY indication ORDER BY cnt DESC
    """, (DEPT,))
    indications = cur.fetchall()
    for r in indications:
        print(f"  {r['indication']}: {r['cnt']} rows")

    print()

    # 2. new_candidates_log Pending — per indication
    print("=== Pending new_candidates per indication ===")
    cur.execute("""
        SELECT indication, COUNT(DISTINCT nct_id) as cnt
        FROM new_candidates_log
        WHERE dept = %s AND decision = 'Pending'
        GROUP BY indication ORDER BY cnt DESC
    """, (DEPT,))
    for r in cur.fetchall():
        ind = r['indication'] or "'' (asset)"
        print(f"  {ind}: {r['cnt']}")

    print()

    # 3. Check if any indication-specific unmatched still keyword-match
    print("=== Checking if indication unmatched buckets still have keyword matches ===")
    cur.execute("""
        SELECT indication, COUNT(DISTINCT nct_id) as cnt
        FROM unmatched_log
        WHERE dept = %s AND decision = 'Pending' AND indication != ''
        GROUP BY indication ORDER BY cnt DESC LIMIT 3
    """, (DEPT,))
    top_indications = [r['indication'] for r in cur.fetchall()]

    for ind in top_indications:
        cur.execute("""
            SELECT nct_id, title, conditions, interventions, sponsor
            FROM unmatched_log
            WHERE dept = %s AND indication = %s AND decision = 'Pending'
        """, (DEPT, ind))
        rows = cur.fetchall()
        matched = [t for t in rows if matches(t)]
        print(f"  {ind}: {len(rows)} unmatched, {len(matched)} still keyword-match (should be 0)")
