"""
fix_collaborators_separator.py
-------------------------------
One-time backfill: re-fetches Collaborators from CT.gov API for all ADC
organized_trials rows and rewrites the field using " | " as separator
(instead of the old ", " which incorrectly split company names like
"Sobi, Inc." or "Bristol-Myers Squibb Co., Ltd.").

Run once after the pipeline separator fix.

Usage:
    python fix_collaborators_separator.py
"""

import time
import requests
import db

DEPT       = "ADC"
API_BASE   = "https://clinicaltrials.gov/api/v2/studies"
API_HDR    = {"User-Agent": "TrialsTracker/1.0"}
RATE_DELAY = 0.25   # seconds between API calls
BATCH_SIZE = 50     # commit every N rows

def fetch_collaborators(nct_id: str) -> str | None:
    """Returns ' | ' separated collaborator names, or None on error."""
    try:
        resp = requests.get(f"{API_BASE}/{nct_id}", headers=API_HDR, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            sp   = data.get("protocolSection", {}).get("sponsorCollaboratorsModule", {})
            names = [c.get("name", "") for c in sp.get("collaborators", []) if c.get("name")]
            return " | ".join(names)
        if resp.status_code == 404:
            return ""   # trial no longer on CT.gov — blank is fine
        print(f"  HTTP {resp.status_code} for {nct_id}")
        return None
    except Exception as e:
        print(f"  Error {nct_id}: {e}")
        return None


def main():
    print("=" * 60)
    print("Fix Collaborators Separator — ADC")
    print("=" * 60)

    conn = db._db()
    with conn.cursor() as cur:
        cur.execute(
            'SELECT nct_id, "Collaborators" FROM organized_trials WHERE dept=%s ORDER BY nct_id',
            (DEPT,)
        )
        rows = cur.fetchall()

    total = len(rows)
    print(f"\nTotal rows to process: {total}")

    updated  = 0
    skipped  = 0
    failed   = 0
    errors   = []

    conn2 = db._db()
    with conn2.cursor() as cur:
        for i, row in enumerate(rows, 1):
            nct_id  = row["nct_id"]
            old_val = row["Collaborators"] or ""

            new_val = fetch_collaborators(nct_id)
            time.sleep(RATE_DELAY)

            if new_val is None:
                # API call failed — leave existing value, try again later
                failed += 1
                errors.append(nct_id)
                print(f"  [{i}/{total}] {nct_id}  FAILED (keeping old)")
                continue

            if new_val == old_val:
                skipped += 1
            else:
                cur.execute(
                    'UPDATE organized_trials SET "Collaborators"=%s WHERE nct_id=%s AND dept=%s',
                    (new_val, nct_id, DEPT)
                )
                updated += 1

            if i % BATCH_SIZE == 0:
                conn2.commit()
                print(f"  [{i}/{total}] committed batch — updated so far: {updated}")

        conn2.commit()

    print(f"\n{'='*60}")
    print(f"Done.")
    print(f"  Updated : {updated}")
    print(f"  Skipped (no change): {skipped}")
    print(f"  Failed  : {failed}")
    if errors:
        print(f"\nFailed NCT IDs (re-run to retry):")
        for n in errors:
            print(f"  {n}")
    print("=" * 60)


if __name__ == "__main__":
    main()
