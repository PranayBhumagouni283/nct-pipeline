import db

with db._cur() as cur:
    # How many distinct nct_ids were in unmatched_log before reclassify?
    # Check current unmatched_log — how many nct_ids appear under multiple indications?
    cur.execute("""
        SELECT COUNT(*) as total_rows, COUNT(DISTINCT nct_id) as distinct_ncts
        FROM unmatched_log
        WHERE dept = 'ADC' AND decision = 'Pending'
    """)
    r = cur.fetchone()
    print(f"Current unmatched_log (ADC, Pending):")
    print(f"  Total rows     : {r['total_rows']}")
    print(f"  Distinct NCTs  : {r['distinct_ncts']}")
    print(f"  Avg indications per NCT: {r['total_rows']/r['distinct_ncts']:.1f}")

    # How many NCTs appear under 2+ indications?
    cur.execute("""
        SELECT COUNT(*) as multi_indication_ncts FROM (
            SELECT nct_id
            FROM unmatched_log
            WHERE dept = 'ADC' AND decision = 'Pending'
            GROUP BY nct_id
            HAVING COUNT(*) > 1
        ) sub
    """)
    print(f"  NCTs under 2+ indications: {cur.fetchone()['multi_indication_ncts']}")

    # Indication breakdown in unmatched_log
    cur.execute("""
        SELECT indication, COUNT(*) as cnt
        FROM unmatched_log
        WHERE dept = 'ADC' AND decision = 'Pending'
        GROUP BY indication ORDER BY cnt DESC
        LIMIT 10
    """)
    print("\nUnmatched by indication (top 10):")
    for r in cur.fetchall():
        ind = r['indication'] or "'' (asset)"
        print(f"  {ind}: {r['cnt']}")
