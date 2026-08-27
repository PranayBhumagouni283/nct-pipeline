"""Fix UNIQUE constraints that are missing the indication column."""
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
import psycopg2, os

conn = psycopg2.connect(os.environ['DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET search_path TO "CT"')

fixes = [
    # (table, old_constraint, new_constraint_name, new_columns)
    (
        "new_candidates_log",
        "new_candidates_log_nct_id_dept_run_date_key",
        "new_candidates_log_nct_id_dept_indication_run_date_key",
        "(nct_id, dept, indication, run_date)",
    ),
    (
        "unmatched_log",
        "unmatched_log_nct_id_dept_key",
        "unmatched_log_nct_id_dept_indication_key",
        "(nct_id, dept, indication)",
    ),
    (
        "modified_log",
        "modified_log_nct_id_dept_run_date_key",
        "modified_log_nct_id_dept_indication_run_date_key",
        "(nct_id, dept, indication, run_date)",
    ),
]

for table, old_name, new_name, cols in fixes:
    print(f"\n[{table}]")

    # Check if old constraint still exists
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_schema = 'CT' AND table_name = %s AND constraint_name = %s
    """, (table, old_name))
    has_old = cur.fetchone()

    # Check if new constraint already exists
    cur.execute("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_schema = 'CT' AND table_name = %s AND constraint_name = %s
    """, (table, new_name))
    has_new = cur.fetchone()

    if has_old:
        cur.execute(f'ALTER TABLE "CT".{table} DROP CONSTRAINT "{old_name}"')
        print(f"  Dropped old constraint: {old_name}")
    else:
        print(f"  Old constraint not found (already removed): {old_name}")

    if not has_new:
        cur.execute(f'ALTER TABLE "CT".{table} ADD CONSTRAINT "{new_name}" UNIQUE {cols}')
        print(f"  Added new constraint: {new_name} on {cols}")
    else:
        print(f"  New constraint already exists: {new_name}")

cur.close()
conn.close()
print("\nDone.")
