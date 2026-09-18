"""
resend_alert.py
---------------
Re-send the last run alert email for one or all departments.

Usage:
    python resend_alert.py ADC          # re-send ADC alert
    python resend_alert.py ASMB         # re-send ASMB alert
    python resend_alert.py ADC ASMB     # re-send both
    python resend_alert.py --all        # re-send all departments
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import db
import combined_pipeline as cp


def resend(dept: str):
    cp.init_dept(dept)

    # Load last run from run_history
    with db._cur() as cur:
        cur.execute(
            'SELECT * FROM "CT".run_history WHERE dept = %s ORDER BY run_date DESC LIMIT 1',
            (dept,)
        )
        rows = cur.fetchall()

    if not rows:
        print(f"  [{dept}] No run history found — cannot resend.")
        return

    last = rows[0]
    run_date = last["run_date"]
    print(f"  [{dept}] Resending alert for run_date={run_date} ...")

    # send_alert only uses len() of each list — pass placeholder lists with correct counts
    cp.send_alert(
        run_date        = run_date,
        new_candidates  = [None] * (last["new_candidates"]  or 0),
        unmatched       = [None] * (last["unmatched"]        or 0),
        modified_trials = [None] * (last["modified_trials"]  or 0),
        newly_compared  = [None] * (last["field_diffs"]      or 0),
        redirects       = [None] * (last["canonical_fixes"]  or 0),
    )
    print(f"  [{dept}] Alert sent.")


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage: python resend_alert.py ADC [ASMB ...] | --all")
        sys.exit(1)

    if "--all" in args:
        depts = db.load_departments()
    else:
        depts = args

    print(f"\nResending alerts for: {', '.join(depts)}\n")
    for dept in depts:
        resend(dept)


if __name__ == "__main__":
    main()
