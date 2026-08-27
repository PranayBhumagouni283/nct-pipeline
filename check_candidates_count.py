import db

with db._cur() as cur:
    cur.execute("""
        SELECT COUNT(DISTINCT nct_id) as cnt
        FROM new_candidates_log
        WHERE dept = 'ADC' AND indication = '' AND decision = 'Pending'
    """)
    print("Distinct Pending (indication=''):", cur.fetchone()['cnt'])

    cur.execute("""
        SELECT decision, COUNT(DISTINCT nct_id) as cnt
        FROM new_candidates_log
        WHERE dept = 'ADC' AND indication = ''
        GROUP BY decision ORDER BY cnt DESC
    """)
    print("All decisions in new_candidates_log (ADC asset):")
    for r in cur.fetchall():
        print(f"  {r['decision']}: {r['cnt']}")

    cur.execute("""
        SELECT COUNT(*) as cnt FROM tracking_list
        WHERE dept = 'ADC' AND indication = ''
    """)
    print("Tracking list (ADC asset):", cur.fetchone()['cnt'])

    cur.execute("""
        SELECT COUNT(*) as cnt FROM rejected_trials
        WHERE dept = 'ADC' AND indication = ''
    """)
    print("Rejected trials (ADC asset):", cur.fetchone()['cnt'])
