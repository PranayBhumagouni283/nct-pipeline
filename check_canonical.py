import db

conn = db._db()
cur = conn.cursor()

# 1. Recent canonical fixes
cur.execute("""
    SELECT run_date, requested_nct, canonical_nct, already_tracked
    FROM canonical_changes_log
    WHERE dept = 'ADC'
    ORDER BY run_date DESC
    LIMIT 20
""")
rows = cur.fetchall()
print("=== Recent Canonical Fixes (ADC) ===")
for r in rows:
    flag = " <-- ALREADY TRACKED (old ID removed)" if r["already_tracked"] else " (new ID added)"
    print(f"  {r['run_date']}  {r['requested_nct']} -> {r['canonical_nct']}{flag}")

# 2. Tracking list counts
cur.execute("SELECT COUNT(*) AS cnt FROM tracking_list WHERE dept = 'ADC'")
print(f"\n=== Tracking List (ADC) ===")
print(f"  Total rows (nct+indication combos): {cur.fetchone()['cnt']}")

cur.execute("SELECT COUNT(DISTINCT nct_id) AS cnt FROM tracking_list WHERE dept = 'ADC'")
print(f"  Distinct NCT IDs: {cur.fetchone()['cnt']}")

# 3. Check if canonical NCTs from today's run are now in tracking list
cur.execute("""
    SELECT c.run_date, c.requested_nct, c.canonical_nct, c.already_tracked,
           t_old.nct_id AS old_still_in_list,
           t_new.nct_id AS new_in_list
    FROM canonical_changes_log c
    LEFT JOIN tracking_list t_old ON t_old.nct_id = c.requested_nct AND t_old.dept = 'ADC'
    LEFT JOIN tracking_list t_new ON t_new.nct_id = c.canonical_nct AND t_new.dept = 'ADC'
    WHERE c.dept = 'ADC'
    ORDER BY c.run_date DESC
    LIMIT 20
""")
rows = cur.fetchall()
print("\n=== Canonical Fixes — Tracking List Status ===")
print(f"  {'Run Date':<12} {'Old NCT':<15} {'New NCT':<15} {'Old in list?':<14} {'New in list?'}")
print(f"  {'-'*12} {'-'*14} {'-'*14} {'-'*13} {'-'*12}")
for r in rows:
    old_present = "YES" if r["old_still_in_list"] else "removed"
    new_present = "YES" if r["new_in_list"] else "not tracked"
    print(f"  {str(r['run_date']):<12} {r['requested_nct']:<15} {r['canonical_nct']:<15} {old_present:<14} {new_present}")
