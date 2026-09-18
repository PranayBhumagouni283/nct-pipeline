"""
reset_pipeline_state.py — Reset ETag + last_run for a dept to force a full re-fetch.
Usage: python reset_pipeline_state.py "Liver Diseases"
"""
import os, sys
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

dept = sys.argv[1] if len(sys.argv) > 1 else "Liver Diseases"

SQL = """
UPDATE "CT".pipeline_state
SET etag = NULL, last_run_date = '2026-09-13'
WHERE dept = %s
RETURNING dept, indication, etag, last_run_date
"""

prod_url = os.environ.get("DATABASE_URL")
if not prod_url:
    print("DATABASE_URL not set — check your .env file")
    sys.exit(1)

print(f"Resetting pipeline state for: {dept}\n")
conn = psycopg2.connect(prod_url)
conn.autocommit = True
cur = conn.cursor()
cur.execute(SQL, (dept,))
rows = cur.fetchall()
cur.close()
conn.close()

if rows:
    for r in rows:
        print(f"  dept={r[0]} indication={r[1]} → etag=NULL last_run_date={r[3]}")
else:
    print(f"  No rows found for dept='{dept}'")

print(f"\nDone. Now run: python combined_pipeline.py \"{dept}\"")
