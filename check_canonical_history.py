import db

conn = db._db()
cur = conn.cursor()

cur.execute("SELECT COUNT(*) AS cnt FROM canonical_changes_log WHERE dept = 'ADC'")
print("Total canonical_changes_log rows (ADC):", cur.fetchone()["cnt"])

cur.execute("""
    SELECT run_date, COUNT(*) AS fixes
    FROM canonical_changes_log
    WHERE dept = 'ADC'
    GROUP BY run_date
    ORDER BY run_date DESC
""")
rows = cur.fetchall()
print("\nPer run:")
for r in rows:
    print(f"  {r['run_date']}  {r['fixes']} fix(es)")

cur.execute("""
    SELECT run_date, requested_nct, canonical_nct, already_tracked
    FROM canonical_changes_log
    WHERE dept = 'ADC'
    ORDER BY run_date DESC, requested_nct
""")
rows = cur.fetchall()
print("\nAll records:")
print(f"  {'Run Date':<12} {'Old NCT':<15} {'New NCT':<15} {'Already Tracked'}")
print(f"  {'-'*12} {'-'*14} {'-'*14} {'-'*15}")
for r in rows:
    print(f"  {str(r['run_date']):<12} {r['requested_nct']:<15} {r['canonical_nct']:<15} {r['already_tracked']}")
