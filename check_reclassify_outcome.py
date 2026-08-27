import db

# We moved 2904 from unmatched. Let's find out where they ended up.
with db._cur() as cur:
    # All decisions in new_candidates_log for ADC asset (including Tracked)
    cur.execute("""
        SELECT decision, COUNT(DISTINCT nct_id) as cnt
        FROM new_candidates_log
        WHERE dept = 'ADC' AND indication = ''
        GROUP BY decision ORDER BY cnt DESC
    """)
    print("new_candidates_log decisions (ADC, indication=''):")
    total = 0
    for r in cur.fetchall():
        print(f"  {r['decision']}: {r['cnt']}")
        total += r['cnt']
    print(f"  TOTAL distinct NCTs: {total}")

    # How many of the 2904 reclassified are in tracking_list?
    # (reclassify moved from unmatched using indication='', so check tracking_list)
    cur.execute("""
        SELECT COUNT(*) as cnt FROM unmatched_log
        WHERE dept = 'ADC' AND decision = 'Pending'
    """)
    print("\nunmatched_log Pending (ADC, all indications) after reclassify:", cur.fetchone()['cnt'])

    # Check if any of the reclassified NCTs are in tracking_list
    # by counting how many NCTs now in new_candidates (Pending) are already tracked
    cur.execute("""
        SELECT COUNT(*) as cnt
        FROM new_candidates_log nc
        JOIN tracking_list tl ON nc.nct_id = tl.nct_id AND tl.dept = nc.dept AND tl.indication = nc.indication
        WHERE nc.dept = 'ADC' AND nc.indication = '' AND nc.decision = 'Pending'
    """)
    print("Pending new_candidates also in tracking_list:", cur.fetchone()['cnt'])

    # Check rejected_trials for ADC asset
    cur.execute("""
        SELECT COUNT(DISTINCT nct_id) as cnt FROM rejected_trials
        WHERE dept = 'ADC' AND indication = ''
    """)
    print("Rejected trials (ADC, indication=''):", cur.fetchone()['cnt'])

    # Total rows (not distinct) in new_candidates_log for ADC asset
    cur.execute("""
        SELECT COUNT(*) as cnt FROM new_candidates_log
        WHERE dept = 'ADC' AND indication = ''
    """)
    print("Total rows in new_candidates_log (ADC, indication=''):", cur.fetchone()['cnt'])
