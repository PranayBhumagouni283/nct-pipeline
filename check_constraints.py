"""Verify all indication-related constraints are correct before running pipeline."""
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
import psycopg2, psycopg2.extras, os

conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute('SET search_path TO "CT"')

# Expected: table → expected columns in PK or UNIQUE constraint
EXPECTED = {
    "tracking_list":       ("PRIMARY KEY", {"nct_id", "dept", "indication"}),
    "rejected_trials":     ("PRIMARY KEY", {"nct_id", "dept", "indication"}),
    "pipeline_state":      ("PRIMARY KEY", {"dept", "indication"}),
    "unmatched_log":       ("PRIMARY KEY", {"nct_id", "dept", "indication"}),
    "new_candidates_log":  ("UNIQUE",      {"nct_id", "dept", "indication", "run_date"}),
    "modified_log":        ("UNIQUE",      {"nct_id", "dept", "indication", "run_date"}),
}

cur.execute("""
    SELECT
        tc.table_name,
        tc.constraint_name,
        tc.constraint_type,
        array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS columns
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
        AND tc.table_schema   = kcu.table_schema
    WHERE tc.table_schema = 'CT'
      AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
      AND tc.table_name = ANY(%s)
    GROUP BY tc.table_name, tc.constraint_name, tc.constraint_type
    ORDER BY tc.table_name, tc.constraint_type
""", (list(EXPECTED.keys()),))

rows = cur.fetchall()

# Build actual map: table → list of (type, set of columns)
actual = {}
for r in rows:
    cols = r["columns"]
    # psycopg2 may return array_agg as a string "{col1,col2}" — parse it
    if isinstance(cols, str):
        cols = cols.strip('{}').split(',')
    actual.setdefault(r["table_name"], []).append(
        (r["constraint_type"], set(cols), r["constraint_name"])
    )

print(f"{'Table':<25} {'Status':<8} {'Detail'}")
print("-" * 80)

all_ok = True
for table, (exp_type, exp_cols) in EXPECTED.items():
    constraints = actual.get(table, [])
    # Check if any constraint matches expected type + columns
    match = next(
        ((t, c, n) for t, c, n in constraints if t == exp_type and c == exp_cols),
        None
    )
    if match:
        print(f"  {table:<25} {'OK':>6}   {exp_type} on {sorted(exp_cols)}")
    else:
        all_ok = False
        print(f"  {table:<25} {'WRONG':>6}   Expected {exp_type} on {sorted(exp_cols)}")
        for t, c, n in constraints:
            print(f"  {'':25}          Found: {t} on {sorted(c)}  [{n}]")

print()
if all_ok:
    print("All constraints correct — safe to run pipeline.")
else:
    print("Fix the constraints above before running pipeline.")

cur.close()
conn.close()
