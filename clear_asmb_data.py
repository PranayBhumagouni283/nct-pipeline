"""
clear_asmb_data.py
Deletes all ASMB pipeline output data from Supabase.
Keeps: tracking_list, dept_keywords, departments.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import db

DEPT = "ASMB"
client = db._db()

tables = [
    "organized_trials",
    "version_cache",
    "nct_version_pairs",
    "field_changes_log",
    "new_candidates_log",
    "unmatched_log",
    "modified_log",
    "canonical_changes_log",
    "run_history",
    "pipeline_state",
]

print(f"Clearing all ASMB pipeline data from Supabase...\n")
for table in tables:
    try:
        resp = client.table(table).delete().eq("dept", DEPT).execute()
        count = len(resp.data) if resp.data else 0
        print(f"  OK: {table} — {count} rows deleted")
    except Exception as e:
        print(f"  ERROR: {table} — {e}")

print(f"\nDone. tracking_list (958 NCTs) and dept_keywords (444) preserved.")
