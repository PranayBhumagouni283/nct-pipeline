"""
clear_and_reset.py — Wipe PostgreSQL data and reset local state for a fresh 14-day run.

Run ONCE before re-running the pipeline:
    python clear_and_reset.py ADC

What it does:
    1. Deletes all rows from all data tables for the given dept (keeps schema)
    2. Deletes local state.json  -> pipeline auto-uses 14-day lookback on next run
    3. Deletes ADC_Master_Dashboard.xlsx -> rebuilt fresh on next pipeline run
    4. Leaves latest_comparison.xlsx intact (needed for Flow 2 version diff)
    5. Leaves tracking list intact (it is the source of truth)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db


TABLES_BY_DEPT = [
    "tracking_list",
    "rejected_trials",
    "organized_trials",
    "version_cache",
    "new_candidates_log",
    "unmatched_log",
    "modified_log",
    "field_changes_log",
    "canonical_changes_log",
    "run_history",
]


def clear_postgres(dept: str):
    print(f"\n[PostgreSQL] Clearing all data for dept={dept}...")
    conn = db._db()
    with conn.cursor() as cur:
        for table in TABLES_BY_DEPT:
            try:
                cur.execute(f'DELETE FROM "CT".{table} WHERE dept = %s', (dept,))
                conn.commit()
                print(f"  Cleared: {table}")
            except Exception as e:
                conn.rollback()
                print(f"  ERROR on {table}: {e}")

        # Reset pipeline_state row (keep the row, blank the dates)
        try:
            cur.execute(
                'INSERT INTO "CT".pipeline_state (dept, etag, last_run_date) VALUES (%s, NULL, NULL) '
                'ON CONFLICT (dept) DO UPDATE SET etag = NULL, last_run_date = NULL',
                (dept,),
            )
            conn.commit()
            print("  Reset:   pipeline_state")
        except Exception as e:
            conn.rollback()
            print(f"  ERROR on pipeline_state: {e}")


def clear_local(dept: str, pipeline_dir: Path):
    dept_dir = pipeline_dir / dept
    print(f"\n[Local] Clearing local state in {dept_dir}...")

    state_json = dept_dir / "state.json"
    if state_json.exists():
        state_json.unlink()
        print("  Deleted: state.json  (pipeline will use 14-day lookback)")
    else:
        print("  state.json not found — already clean")

    dashboard = dept_dir / f"{dept}_Master_Dashboard.xlsx"
    if dashboard.exists():
        dashboard.unlink()
        print(f"  Deleted: {dept}_Master_Dashboard.xlsx")
    else:
        print(f"  {dept}_Master_Dashboard.xlsx not found — already clean")

    comparison = dept_dir / "latest_comparison.xlsx"
    if comparison.exists():
        print("  Kept:    latest_comparison.xlsx (Flow 2 version baseline)")

    tracking_files = [
        f for f in dept_dir.glob("*.xlsx")
        if not f.name.startswith("~")
        and not f.name.endswith("_Master_Dashboard.xlsx")
        and f.name != "latest_comparison.xlsx"
    ]
    for f in tracking_files:
        print(f"  Kept:    {f.name} (tracking list)")


def main():
    parser = argparse.ArgumentParser(description="Clear PostgreSQL data and reset local state")
    parser.add_argument("dept", help='Department name, e.g. "ADC"')
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()
    dept = args.dept
    here = Path(__file__).parent

    print("=" * 55)
    print("NCT Tracking - Clear & Reset for Fresh 14-Day Run")
    print(f"Department : {dept}")
    print("=" * 55)

    if not args.yes:
        confirm = input("\nThis will DELETE all PostgreSQL data and reset local state.\nType YES to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            sys.exit(0)

    clear_postgres(dept)
    clear_local(dept, here)

    print("\n" + "=" * 55)
    print("Reset complete. Next steps:")
    print(f"  1. Run:  python combined_pipeline.py {dept}")
    print("           (auto-uses last 14 days as from_date)")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
