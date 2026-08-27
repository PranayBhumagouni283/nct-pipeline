"""
backfill_scan.py
-----------------
Re-scans CT.gov from the first pipeline run (2026-07-12) to today and reports
any keyword-matching trials that are NOT already in:
  - tracking_list      (existing tracked trials)
  - new_candidates_log (already in the new bucket)

By default: read-only, saves results to CSV for review.
Use --save to insert missed trials into new_candidates_log (run_date = today).

Usage:
    python backfill_scan.py ADC                      # dry run, CSV only
    python backfill_scan.py ADC --from 2026-06-01   # override start date
    python backfill_scan.py ADC --save              # insert missed into new_candidates_log
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

import db
import combined_pipeline as _cp

FIRST_RUN_DATE = "2026-07-02"  # first run was 2026-07-12, which scanned 10 days back to Jul 2


def _load_already_known(dept: str) -> set[str]:
    """NCTs already in tracking list or new candidates bucket."""
    known: set[str] = set()
    with db._cur() as cur:
        cur.execute("SELECT nct_id FROM tracking_list      WHERE dept = %s", (dept,))
        known.update(r["nct_id"] for r in cur.fetchall())
        cur.execute("SELECT nct_id FROM new_candidates_log WHERE dept = %s", (dept,))
        known.update(r["nct_id"] for r in cur.fetchall())
    return known


def main(dept_arg: str, from_override: str | None, save: bool = False):
    _cp.init_dept(dept_arg)
    dept  = _cp.DEPT_NAME
    today = datetime.today().strftime("%Y-%m-%d")

    from_date = from_override or FIRST_RUN_DATE
    print(f"\n[backfill_scan]  dept={dept}  range={from_date} → {today}")

    # ── Load keywords + known NCTs ────────────────────────────────────────────
    print("\nLoading keywords and known NCT lists...")
    _cp.INDICATION = ""
    asset_keywords = _cp.load_keywords()
    rejected_ids   = db.load_rejected_trials(dept, "")
    already_known  = _load_already_known(dept)

    print(f"  Already known (tracking + new bucket): {len(already_known):,}")
    print(f"  Rejected                              : {len(rejected_ids):,}")
    print(f"  Keywords                              : {len(asset_keywords):,}")

    # ── Scan CT.gov ───────────────────────────────────────────────────────────
    print(f"\nScanning CT.gov  {from_date} → {today}...")
    all_trials = _cp.scan_global_delta(from_date, today)
    print(f"  Total trials in range: {len(all_trials):,}")

    # ── Find missed candidates ────────────────────────────────────────────────
    missed = []
    for trial in all_trials:
        nct_id = trial["NCT ID"]
        if nct_id in rejected_ids:
            continue
        if nct_id in already_known:
            continue
        if _cp.matches_keywords(trial, asset_keywords):
            missed.append(trial)

    print(f"\n  Missed candidates: {len(missed):,}")

    if not missed:
        print("\nNothing missed — all keyword-matching trials are already captured.")
        return

    # ── Save CSV report ───────────────────────────────────────────────────────
    import pandas as pd
    out_path = Path(__file__).parent / f"backfill_missed_{dept}_{today}.csv"
    pd.DataFrame(missed).to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n  Report saved → {out_path.name}")

    # ── Insert into new_candidates_log ────────────────────────────────────────
    if save:
        print(f"\n[--save] Inserting {len(missed)} missed trials into new_candidates_log...")

        # Group by Last Updated date — each trial goes into the run bucket
        # corresponding to when CT.gov actually posted it
        from collections import defaultdict
        by_date: dict[str, list[dict]] = defaultdict(list)
        for t in missed:
            run_date = t.get("Last Updated") or today
            by_date[run_date].append(t)

        for run_date, trials in sorted(by_date.items()):
            db.insert_new_candidates(dept, run_date, trials, indication="")
            print(f"  {run_date}: inserted {len(trials)} trial(s)")

            # Update the matching run_history row so counts stay consistent.
            # Find the first pipeline run ON or AFTER the trial's Last Updated date.
            with db._cur() as cur:
                cur.execute(
                    """
                    UPDATE run_history
                    SET    new_candidates = new_candidates + %s
                    WHERE  dept = %s AND indication = %s
                      AND  run_date = (
                          SELECT MIN(run_date) FROM run_history
                          WHERE  dept = %s AND indication = %s
                            AND  run_date >= %s
                      )
                    """,
                    (len(trials), dept, "", dept, "", run_date),
                )
                updated = cur.rowcount
                if updated:
                    print(f"    → run_history updated for nearest run on/after {run_date}")
                else:
                    print(f"    → no run_history row found on/after {run_date} — counts unchanged")

        print(f"\n  Done. {len(missed)} trials are now Pending in the New Candidates page.")
    else:
        print("  Run with --save to insert these into the new candidates bucket.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find missed new candidates")
    parser.add_argument("dept",        help='Department e.g. "ADC"')
    parser.add_argument("--from",      dest="from_date", default=None,
                        help=f"Override start date (YYYY-MM-DD). Default: {FIRST_RUN_DATE}")
    parser.add_argument("--save",      action="store_true",
                        help="Insert missed trials into new_candidates_log (run_date = today)")
    args = parser.parse_args()
    main(args.dept, args.from_date, args.save)
