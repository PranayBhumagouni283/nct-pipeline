"""
backfill_version_pairs.py
--------------------------
One-time script to populate nct_version_pairs for all NCTs already in the DB.
Fetches ALL consecutive version pair comparisons from CT.gov for each NCT.

Usage:
    python backfill_version_pairs.py ADC
    python backfill_version_pairs.py ADC --start 500        # resume from index 500
    python backfill_version_pairs.py ADC --only-missing     # skip NCTs already in DB
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import combined_pipeline as _cp
from NCT_Changes_Tracker import get_history_list


def backfill(dept: str, start: int = 0, batch_size: int = 50, only_missing: bool = False):
    _cp.init_dept(dept)
    dept_name = _cp.DEPT_NAME  # read after init_dept sets it
    nct_ids = db.load_tracking_list(dept_name)
    total   = len(nct_ids)
    subset  = nct_ids[start:]

    if only_missing:
        already_done = db.load_ncts_with_version_pairs(dept_name)
        subset = [n for n in subset if n not in already_done]
        print(f"\nSkipping {total - len(subset)} NCTs already in nct_version_pairs.")

    print(f"\nBackfilling nct_version_pairs for {len(subset)} NCTs (starting at index {start})...")
    print(f"Total tracked: {total}\n")

    batch = []
    saved = 0
    run_total = len(subset)

    for i, nct_id in enumerate(subset, start=1):
        print(f"[{i}/{run_total}] {nct_id}")
        try:
            pairs = _cp._get_all_version_pairs(nct_id)
            for p in pairs:
                p["nct_id"] = nct_id
            if not pairs:
                # Single-version NCT — store a placeholder with prev_version=0
                history = get_history_list(nct_id)
                if history:
                    last = history[-1]
                    pairs = [{
                        "nct_id":             nct_id,
                        "prev_version":       0,
                        "curr_version":       last.get("version", 1),
                        "prev_date":          "",
                        "curr_date":          last.get("date", ""),
                        "curr_status":        last.get("status", ""),
                        "modules_changed":    "",
                        "field_change_count": 0,
                        "field_changes":      "",
                        "note":               "Initial version",
                        "total_versions":     len(history),
                    }]
            batch.extend(pairs)
        except Exception as e:
            print(f"  ERROR: {e}")

        if len(batch) >= batch_size:
            db.upsert_version_pairs(dept_name, batch)
            saved += len(batch)
            print(f"  -- Saved batch ({saved} total) --")
            batch = []

        time.sleep(0.4)

    if batch:
        db.upsert_version_pairs(dept_name, batch)
        saved += len(batch)

    print(f"\nDone. {saved} version pair rows upserted for {dept}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dept", help='Department name e.g. "ADC"')
    parser.add_argument("--start", type=int, default=0, help="Resume from this index (0-based)")
    parser.add_argument("--only-missing", action="store_true", help="Skip NCTs that already have entries in nct_version_pairs")
    args = parser.parse_args()
    backfill(args.dept, args.start, only_missing=args.only_missing)
