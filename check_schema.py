import psycopg2
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")
conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True

TABLES = [
    "tracking_list", "modified_log", "field_changes_log",
    "pipeline_state", "run_history", "rejected_trials",
    "new_candidates_log", "unmatched_log",
]

with conn.cursor() as cur:
    # Constraints
    cur.execute("""
        SELECT tc.table_name, tc.constraint_name, tc.constraint_type,
               string_agg(kcu.column_name, ', ' ORDER BY kcu.ordinal_position) AS columns
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'CT'
          AND tc.table_name = ANY(%s)
        GROUP BY tc.table_name, tc.constraint_name, tc.constraint_type
        ORDER BY tc.table_name, tc.constraint_type
    """, (TABLES,))
    print("=== CONSTRAINTS ===")
    print(f"{'Table':<25} {'Constraint':<45} {'Type':<12} {'Columns'}")
    print("-" * 100)
    for row in cur.fetchall():
        print(f"{row[0]:<25} {row[1]:<45} {row[2]:<12} {row[3]}")

    # Columns
    cur.execute("""
        SELECT table_name, column_name, data_type, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'CT'
          AND table_name = ANY(%s)
        ORDER BY table_name, ordinal_position
    """, (TABLES,))
    print("\n=== COLUMNS ===")
    print(f"{'Table':<25} {'Column':<25} {'Type':<20} {'Default':<20} Nullable")
    print("-" * 100)
    for row in cur.fetchall():
        print(f"{row[0]:<25} {row[1]:<25} {row[2]:<20} {str(row[3]):<20} {row[4]}")

conn.close()
