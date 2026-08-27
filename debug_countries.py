import db
conn = db._db()
with conn.cursor() as cur:
    cur.execute("""
        SELECT DISTINCT trim(c) AS country
        FROM organized_trials,
          LATERAL unnest(string_to_array(COALESCE(countries,''), ' | ')) AS c
        WHERE trim(c) ILIKE '%turkey%'
           OR trim(c) ILIKE '%taiwan%'
           OR trim(c) ILIKE '%hong%'
           OR trim(c) ILIKE '%bosnia%'
           OR trim(c) ILIKE '%dominican%'
           OR trim(c) ILIKE '%gambia%'
           OR trim(c) ILIKE '%macedonia%'
           OR trim(c) ILIKE '%bahamas%'
    """)
    for r in cur.fetchall():
        s = r["country"]
        print(repr(s), "  bytes:", s.encode("utf-8").hex())
