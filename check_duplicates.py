import db

with db._cur() as cur:
    # Tracking list NCTs also in new_candidates_log
    cur.execute("""
        SELECT tl.dept, tl.nct_id, nc.run_date, nc.decision
        FROM tracking_list tl
        JOIN new_candidates_log nc ON nc.nct_id = tl.nct_id AND nc.dept = tl.dept
        WHERE tl.dept IN ('ADC', 'ASMB')
        ORDER BY tl.dept, tl.nct_id
    """)
    nc_overlaps = cur.fetchall()

    # Tracking list NCTs also in unmatched_log
    cur.execute("""
        SELECT tl.dept, tl.nct_id, ul.last_seen_date, ul.decision
        FROM tracking_list tl
        JOIN unmatched_log ul ON ul.nct_id = tl.nct_id AND ul.dept = tl.dept
        WHERE tl.dept IN ('ADC', 'ASMB')
        ORDER BY tl.dept, tl.nct_id
    """)
    um_overlaps = cur.fetchall()

    cur.execute("SELECT dept, COUNT(*) FROM tracking_list WHERE dept IN ('ADC','ASMB') GROUP BY dept ORDER BY dept")
    tl_counts = cur.fetchall()

    cur.execute("SELECT dept, COUNT(DISTINCT nct_id) FROM new_candidates_log WHERE dept IN ('ADC','ASMB') GROUP BY dept ORDER BY dept")
    nc_counts = cur.fetchall()

    cur.execute("SELECT dept, COUNT(DISTINCT nct_id) FROM unmatched_log WHERE dept IN ('ADC','ASMB') GROUP BY dept ORDER BY dept")
    um_counts = cur.fetchall()

print("=== TRACKING LIST SIZES ===")
for r in tl_counts:
    print(f"  {r['dept']}: {r['count']} NCTs")

print("\n=== NEW CANDIDATES (distinct NCTs ever seen) ===")
for r in nc_counts:
    print(f"  {r['dept']}: {r['count']} NCTs")

print("\n=== UNMATCHED (distinct NCTs ever seen) ===")
for r in um_counts:
    print(f"  {r['dept']}: {r['count']} NCTs")

print(f"\n=== TRACKING LIST NCTs ALSO IN NEW CANDIDATES ({len(nc_overlaps)} rows) ===")
if nc_overlaps:
    for r in nc_overlaps:
        print(f"  [{r['dept']}] {r['nct_id']} | run_date={r['run_date']} | decision={r['decision']}")
else:
    print("  None — clean!")

print(f"\n=== TRACKING LIST NCTs ALSO IN UNMATCHED ({len(um_overlaps)} rows) ===")
if um_overlaps:
    for r in um_overlaps:
        print(f"  [{r['dept']}] {r['nct_id']} | last_seen={r['last_seen_date']} | decision={r['decision']}")
else:
    print("  None — clean!")
