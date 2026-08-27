"""
add_to_tracking.py
------------------
For the given list of NCT/CTR IDs (ADC dept):
  1. Report which exist in new_candidates_log or unmatched_log
  2. Remove them from those tables
  3. Upsert all into tracking_list (skip if already there)
"""

import db
from datetime import date

NCT_IDS = list(dict.fromkeys([   # deduplicates while preserving order
    "NCT07497074","NCT07502300","NCT07503756","NCT07504588","NCT07490301",
    "NCT07507526","NCT07502872","NCT07512583","CTR20261261","NCT07514975",
    "NCT07514533","CTR20261200","NCT07518160","NCT07503808","NCT07476729",
    "NCT07490119","NCT07489703","NCT07518979","NCT07519655","NCT07403136",
    "NCT07524348","NCT07522879","NCT07524413","NCT07521670","NCT07528898",
    "NCT07529808","NCT07531979","NCT07539311","NCT07488676","NCT07533123",
    "CTR20261445","NCT07539285","NCT07542717","CTR20261416","CTR20261456",
    "NCT07545083","NCT07545356","NCT07548060","NCT07418749","NCT07549412",
    "NCT07502300","NCT07418749","CTR20261261","NCT07556848","NCT07560124",
    "NCT07564466","NCT07537881","NCT07573436","CTR20261806","NCT07578571",
    "CTR20261258","CTR20261855","NCT07581574","NCT07579598","CTR20261808",
    "NCT07584135","NCT07492914","CTR20261908","NCT07591090","NCT07590531",
    "NCT07484022","NCT07589205","NCT07589049","NCT07509515","NCT07110883",
    "NCT07272590","NCT06735144","NCT07241767","NCT07598318","NCT07597629",
    "NCT07604766","NCT07602777","NCT07609706","NCT07612176","NCT07612189",
    "NCT06953089","NCT07612787","NCT07618260","NCT07584499","NCT07619365",
    "NCT07621601","NCT07617727","NCT07618923","NCT07619521","NCT07622524",
    "NCT07622186","NCT07612137","NCT07623356","NCT07625644","NCT07630974",
    "NCT07630103","NCT07633873","NCT07632339","CTR20262204","NCT07636772",
    "CTR20262216","NCT07638891","NCT07639086","NCT07643103","NCT07641855",
    "NCT07640789","NCT07621159","NCT07645222","NCT07644650","NCT07638722",
    "NCT07572123","NCT07647263","NCT07647289","NCT07642024","NCT07649980",
    "NCT07654426","NCT07439653","NCT07657806","NCT07648914","NCT07661797",
    "NCT07662863","NCT07662473","NCT07665190","NCT07664150","NCT07633873",
    "NCT07668154","NCT07669610","NCT07667335","NCT07669779","NCT07673029",
    "NCT07674615","NCT07672782","NCT07676162","NCT07518147","NCT07677475",
    "NCT07678957","NCT07680790","NCT07672223","NCT07683507","NCT07684612",
    "NCT07684599","NCT07615660","NCT07686445","NCT07686588","NCT07686068",
    "NCT07670312","NCT07409246","NCT07692334","NCT07692048","NCT07693959",
    "NCT07691489","NCT07693751","NCT07694765","CTR20262690","NCT07697586",
    "NCT07697339","NCT07701122","NCT07703228","NCT07702305","NCT07702851",
]))

DEPT  = "ADC"
TODAY = str(date.today())

# Build IN-clause placeholders  e.g. %s,%s,%s,...
def ph(n): return ','.join(['%s'] * n)

print(f"Total unique IDs: {len(NCT_IDS)}")

conn = db._db()
with conn.cursor() as cur:

    # ── 1. Check new_candidates_log ───────────────────────────────────────────
    cur.execute(f"""
        SELECT DISTINCT nct_id, decision
        FROM new_candidates_log
        WHERE dept = %s AND nct_id IN ({ph(len(NCT_IDS))})
        ORDER BY nct_id
    """, [DEPT] + NCT_IDS)
    in_nc = cur.fetchall()

    print(f"\n=== IN new_candidates_log ({len(in_nc)}) ===")
    if in_nc:
        for r in in_nc:
            print(f"  {r['nct_id']}  decision={r['decision']}")
    else:
        print("  None")

    # ── 2. Check unmatched_log ────────────────────────────────────────────────
    cur.execute(f"""
        SELECT DISTINCT nct_id, decision
        FROM unmatched_log
        WHERE dept = %s AND nct_id IN ({ph(len(NCT_IDS))})
        ORDER BY nct_id
    """, [DEPT] + NCT_IDS)
    in_um = cur.fetchall()

    print(f"\n=== IN unmatched_log ({len(in_um)}) ===")
    if in_um:
        for r in in_um:
            print(f"  {r['nct_id']}  decision={r['decision']}")
    else:
        print("  None")

    # ── 3. Already in tracking_list ──────────────────────────────────────────
    cur.execute(f"""
        SELECT nct_id FROM tracking_list
        WHERE dept = %s AND nct_id IN ({ph(len(NCT_IDS))})
    """, [DEPT] + NCT_IDS)
    already_tracked = {r['nct_id'] for r in cur.fetchall()}
    print(f"\n=== Already in tracking_list: {len(already_tracked)} ===")
    if already_tracked:
        for n in sorted(already_tracked):
            print(f"  {n}")

    # ── 4. Remove from new_candidates_log ────────────────────────────────────
    nc_ids = [r['nct_id'] for r in in_nc]
    if nc_ids:
        cur.execute(f"""
            DELETE FROM new_candidates_log
            WHERE dept = %s AND nct_id IN ({ph(len(nc_ids))})
        """, [DEPT] + nc_ids)
        print(f"\n  Deleted {cur.rowcount} rows from new_candidates_log")
        conn.commit()

    # ── 5. Remove from unmatched_log ─────────────────────────────────────────
    um_ids = [r['nct_id'] for r in in_um]
    if um_ids:
        cur.execute(f"""
            DELETE FROM unmatched_log
            WHERE dept = %s AND nct_id IN ({ph(len(um_ids))})
        """, [DEPT] + um_ids)
        print(f"  Deleted {cur.rowcount} rows from unmatched_log")
        conn.commit()

    # ── 6. Insert into tracking_list (skip existing) ─────────────────────────
    to_add = [n for n in NCT_IDS if n not in already_tracked]
    print(f"\n=== Adding {len(to_add)} new NCTs to tracking_list ===")
    if to_add:
        for nct in to_add:
            cur.execute("""
                INSERT INTO tracking_list (nct_id, dept, added_date, added_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (nct_id, dept) DO NOTHING
            """, (nct, DEPT, TODAY, 'manual'))
        conn.commit()
        print(f"  Done — {len(to_add)} inserted")
    else:
        print("  All already in tracking_list")

    # ── 7. Final count ────────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM tracking_list WHERE dept = %s", (DEPT,))
    total = cur.fetchone()['count']
    print(f"\n=== ADC tracking_list total now: {total} NCTs ===")
