"""
reparse_all.py — One-time re-parse of all tracked trials to 62-column schema

Fetches every NCT in the tracking list from CT.gov API, parses with
parse_study_to_organized(), tags Primary Drug inline, and upserts to
organized_trials in Supabase.

Usage:
    python reparse_all.py ADC
    python reparse_all.py ADC --resume    # skip NCTs already re-parsed this session

This does NOT touch run_history, field_changes, candidates, or any other table.
Safe to interrupt and re-run with --resume.
"""

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import db
from combined_pipeline import (
    parse_study_to_organized,
    load_drug_keywords,
    tag_primary_drug,
    tag_all_organized_drug,
    KEYWORDS_DIR,
)

import requests

API_URL      = "https://clinicaltrials.gov/api/v2/studies"
MAX_RETRIES  = 3
RETRY_WAIT   = 5.0
REQ_TIMEOUT  = 20
SLEEP        = 0.35   # polite rate limit between requests
UPSERT_EVERY = 50     # upsert to Supabase every N parsed rows


def fetch_raw(nct_id: str):
    """Fetch full raw JSON for one NCT from CT.gov. Returns (data, error)."""
    url = f"{API_URL}/{nct_id}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=REQ_TIMEOUT)
            if resp.status_code == 404:
                return None, "404 Not Found"
            resp.raise_for_status()
            return resp.json(), None
        except Exception as e:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT)
    return None, f"Failed after {MAX_RETRIES} attempts"


def main():
    parser = argparse.ArgumentParser(description="Re-parse all tracked NCTs to 62-column schema")
    parser.add_argument("dept", help='Department name e.g. "ADC"')
    parser.add_argument("--resume", action="store_true",
                        help="Skip NCTs already processed in a previous partial run")
    parser.add_argument("--skip-drug-tag", action="store_true",
                        help="Skip the final Primary Drug re-tag pass")
    args = parser.parse_args()

    dept = args.dept

    # Resolve KEYWORDS_DIR to dept-specific folder so load_drug_keywords() works
    import combined_pipeline as _cp
    dept_path = _HERE / dept
    if dept_path.exists():
        _cp.KEYWORDS_DIR = dept_path / "keywords"

    # Load drug keywords once
    print(f"\nLoading drug keywords...")
    drugs = load_drug_keywords()

    # Load all tracked NCT IDs
    print(f"\nLoading tracking list for {dept}...")
    nct_ids = db.load_tracking_list(dept)
    total   = len(nct_ids)
    print(f"  {total} NCTs to re-parse\n")

    # Optional resume: load already-done NCTs from checkpoint file
    checkpoint_file = _HERE / f"_reparse_done_{dept}.json"
    done_set: set[str] = set()
    if args.resume and checkpoint_file.exists():
        done_set = set(json.loads(checkpoint_file.read_text(encoding="utf-8")))
        print(f"  Resuming — {len(done_set)} already done, {total - len(done_set)} remaining\n")

    pending    = [n for n in nct_ids if n not in done_set]
    batch:     list[dict] = []
    done_list: list[str]  = list(done_set)
    errors:    list[str]  = []

    for i, nct_id in enumerate(pending, 1):
        print(f"  [{i}/{len(pending)}] {nct_id}", end="  ", flush=True)

        data, err = fetch_raw(nct_id)
        if err or not data:
            print(f"ERROR: {err}")
            errors.append(nct_id)
            time.sleep(SLEEP)
            continue

        parsed = parse_study_to_organized(nct_id, data)
        # Tag Primary Drug inline so it's included in the upsert
        parsed["Primary Drug"] = tag_primary_drug(parsed.get("Interventions", ""), drugs)
        batch.append(parsed)
        done_list.append(nct_id)
        print(f"OK  [{len(batch)} in batch]")

        # Upsert and checkpoint every UPSERT_EVERY rows
        if len(batch) >= UPSERT_EVERY:
            print(f"\n  --- Upserting {len(batch)} rows to Supabase ---")
            db.upsert_organized_trials(dept, batch)
            checkpoint_file.write_text(json.dumps(done_list), encoding="utf-8")
            batch.clear()
            print()

        time.sleep(SLEEP)

    # Final upsert for remaining batch
    if batch:
        print(f"\n  --- Upserting final {len(batch)} rows to Supabase ---")
        db.upsert_organized_trials(dept, batch)
        checkpoint_file.write_text(json.dumps(done_list), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"Re-parse complete for {dept}")
    print(f"  Processed : {len(done_list) - len(done_set)}")
    print(f"  Errors    : {len(errors)}")
    if errors:
        print(f"  Failed NCTs: {', '.join(errors)}")
    print(f"  Total in Supabase: {len(done_list)}")

    # Final Primary Drug pass (catches any keyword file updates)
    if not args.skip_drug_tag:
        print(f"\n[Final Step] Re-tagging Primary Drug for all {dept} trials...")
        tag_all_organized_drug(dept, drugs)

    # Clean up checkpoint on full success
    if not errors and checkpoint_file.exists():
        checkpoint_file.unlink()
        print("  Checkpoint cleared.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
