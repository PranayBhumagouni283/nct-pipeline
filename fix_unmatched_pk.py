"""Rebuild unmatched_log PRIMARY KEY to include indication."""
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
import psycopg2, os

conn = psycopg2.connect(os.environ['DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET search_path TO "CT"')

# 1. Drop old PK (nct_id, dept)
cur.execute('ALTER TABLE unmatched_log DROP CONSTRAINT IF EXISTS unmatched_log_pkey')
print("Dropped old PK (nct_id, dept)")

# 2. Add new PK (nct_id, dept, indication)
cur.execute('ALTER TABLE unmatched_log ADD PRIMARY KEY (nct_id, dept, indication)')
print("Added new PK (nct_id, dept, indication)")

# 3. Drop the separate UNIQUE constraint — now redundant since PK covers it
cur.execute('ALTER TABLE unmatched_log DROP CONSTRAINT IF EXISTS "unmatched_log_nct_id_dept_indication_key"')
print("Dropped redundant UNIQUE constraint")

cur.close()
conn.close()
print("Done.")
