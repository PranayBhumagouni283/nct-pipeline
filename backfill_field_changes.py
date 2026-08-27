"""
backfill_field_changes.py
--------------------------
Find NCTs missing from field_changes_log for a given run_date and copy
their data from nct_version_pairs (which already has all NCTs) into
field_changes_log — without re-running the full pipeline.

Usage:
    python backfill_field_changes.py ASMB 2026-07-14
    python backfill_field_changes.py ASMB          # defaults to today
    python backfill_field_changes.py --all 2026-07-14
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db


def backfill(dept: str, run_date: str):
    print(f"\n[{dept}] Backfilling field_changes_log for run_date={run_date}")

    # 1. Tracking list
    tracking_ncts = set(db.load_tracking_list(dept))
    print(f"  Tracking list      : {len(tracking_ncts)} NCTs")

    # 2. Already in field_changes_log for this run_date
    with db._cur() as cur:
        cur.execute(
            "SELECT nct_id FROM field_changes_log WHERE dept = %s AND run_date = %s",
            (dept, run_date),
        )
        already = {r["nct_id"] for r in cur.fetchall()}
    print(f"  Already logged     : {len(already)}")

    missing = sorted(tracking_ncts - already)
    print(f"  Missing            : {len(missing)}")

    if not missing:
        print("  Nothing to backfill.")
        return

    # 3. For each missing NCT, grab its latest version pair from nct_version_pairs
    rows_to_insert = []
    for nct_id in missing:
        with db._cur() as cur:
            cur.execute(
                """
                SELECT * FROM nct_version_pairs
                WHERE dept = %s AND nct_id = %s
                ORDER BY curr_version DESC
                LIMIT 1
                """,
                (dept, nct_id),
            )
            row = cur.fetchone()

        if not row:
            print(f"  WARNING: {nct_id} not in nct_version_pairs — skipping")
            continue

        rows_to_insert.append({
            "nct_id":             row["nct_id"],
            "note":               row.get("note") or "",
            "total_versions":     row.get("total_versions") or 0,
            "prev_version":       row.get("prev_version"),
            "curr_version":       row.get("curr_version"),
            "prev_date":          row.get("prev_date") or "",
            "curr_date":          row.get("curr_date") or "",
            "curr_status":        row.get("curr_status") or "",
            "modules_changed":    row.get("modules_changed") or "",
            "field_change_count": row.get("field_change_count") or 0,
            "field_changes":      row.get("field_changes") or "",
            "curr_full_data":     row.get("curr_full_data"),
        })

    if not rows_to_insert:
        print("  No rows to insert (all missing NCTs absent from nct_version_pairs).")
        return

    print(f"  Inserting {len(rows_to_insert)} rows...")
    db.insert_field_changes(dept, run_date, rows_to_insert)
    print(f"  Done — field_changes_log now has {len(already) + len(rows_to_insert)} entries for {dept} on {run_date}.")


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python backfill_field_changes.py <DEPT> [run_date]")
        print("       python backfill_field_changes.py --all [run_date]")
        sys.exit(1)

    run_date = None
    dept_args = []
    for a in args:
        if a.startswith("20"):
            run_date = a
        else:
            dept_args.append(a)

    run_date = run_date or str(date.today())

    if "--all" in dept_args:
        depts = db.load_departments()
    else:
        depts = dept_args

    for dept in depts:
        backfill(dept, run_date)


if __name__ == "__main__":
    main()
