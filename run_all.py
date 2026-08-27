"""
run_all.py
----------
Run the NCT pipeline for every department in the Supabase `departments` table.

Usage:
    python run_all.py              # run all departments
    python run_all.py ADC ASMB    # run specific departments only
"""

import sys
import subprocess
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db


def main():
    # Determine which depts to run
    if len(sys.argv) > 1:
        depts = sys.argv[1:]
        print(f"Running pipeline for specified departments: {', '.join(depts)}")
    else:
        depts = db.load_departments()
        print(f"Running pipeline for all departments: {', '.join(depts)}")

    if not depts:
        print("No departments found. Exiting.")
        return

    results = {}
    overall_start = datetime.now()

    for dept in depts:
        print(f"\n{'=' * 60}")
        print(f"  Starting: {dept}  ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        print(f"{'=' * 60}\n")

        start = datetime.now()
        proc = subprocess.run(
            [sys.executable, "combined_pipeline.py", dept],
            cwd=Path(__file__).parent,
        )
        elapsed = datetime.now() - start

        status = "SUCCESS" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
        results[dept] = {"status": status, "elapsed": elapsed}

        print(f"\n  [{dept}] {status} — {elapsed}")

        # Small gap between departments to avoid rate limiting
        if dept != depts[-1]:
            print("  Waiting 10s before next department...")
            time.sleep(10)

    # Summary
    total_elapsed = datetime.now() - overall_start
    print(f"\n{'=' * 60}")
    print(f"  Run All — Summary  ({total_elapsed})")
    print(f"{'=' * 60}")
    for dept, r in results.items():
        print(f"  {dept:10s}  {r['status']:30s}  {r['elapsed']}")
    print()


if __name__ == "__main__":
    main()
