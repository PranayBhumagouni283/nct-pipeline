"""
fill_version_gaps.py
---------------------
Fills missing intermediate version pairs in nct_version_pairs.

For each NCT in the tracking list:
  1. Fetches ALL CT.gov versions via get_history_list()          -- 1 API call
  2. Checks which consecutive pairs are already in DB
  3. Fetches ONLY the unique version data needed for missing pairs (cached)
  4. Computes diffs and inserts missing pairs into nct_version_pairs

Only nct_version_pairs is updated — field_changes_log and version_cache
are NOT touched (no run_date to associate the backfilled data with).

Usage:
    python fill_version_gaps.py ADC
    python fill_version_gaps.py ADC --start 200     # resume from index 200
    python fill_version_gaps.py ADC --dry-run       # report gaps without writing
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import combined_pipeline as _cp
from NCT_Changes_Tracker import get_history_list, get_version_data, compare_versions, format_field_changes


def _load_existing_pairs(dept: str, nct_id: str) -> set[tuple[int, int]]:
    with db._cur() as cur:
        cur.execute(
            "SELECT prev_version, curr_version FROM nct_version_pairs "
            "WHERE dept = %s AND nct_id = %s",
            (dept, nct_id),
        )
        return {(r["prev_version"], r["curr_version"]) for r in cur.fetchall()}


def _all_tracking_ncts(dept: str) -> list[str]:
    """Distinct NCT IDs across all indications for this dept."""
    with db._cur() as cur:
        cur.execute(
            "SELECT DISTINCT nct_id FROM tracking_list WHERE dept = %s ORDER BY nct_id",
            (dept,),
        )
        return [r["nct_id"] for r in cur.fetchall()]


def fill_gaps(dept: str, start: int = 0, dry_run: bool = False):
    _cp.init_dept(dept)
    dept_name = _cp.DEPT_NAME

    nct_ids = _all_tracking_ncts(dept_name)
    subset  = nct_ids[start:]
    total   = len(subset)

    print(f"\n[fill_version_gaps] {dept_name} — {total} NCTs to check (start={start})")
    if dry_run:
        print("  DRY RUN — no writes\n")
    else:
        print()

    grand_ncts_with_gaps = 0
    grand_gaps_found     = 0
    grand_pairs_written  = 0
    batch                = []
    BATCH_SIZE           = 50

    for i, nct_id in enumerate(subset, 1):
        try:
            # ── 1. All CT.gov versions (1 API call) ──────────────────────────
            history = get_history_list(nct_id)
            time.sleep(0.2)

            if len(history) < 2:
                print(f"  [{i}/{total}] {nct_id} — <2 versions, skip")
                continue

            # ── 2. Expected consecutive pairs from CT.gov ─────────────────────
            versions       = [h["version"] for h in history]
            expected_pairs = set(zip(versions, versions[1:]))

            # ── 3. Existing pairs in DB ───────────────────────────────────────
            existing_pairs = _load_existing_pairs(dept_name, nct_id)

            # ── 4. Missing pairs ──────────────────────────────────────────────
            missing = sorted(expected_pairs - existing_pairs)
            if not missing:
                print(f"  [{i}/{total}] {nct_id} — {len(history)} versions, no gaps")
                continue

            grand_ncts_with_gaps += 1
            grand_gaps_found     += len(missing)
            print(f"  [{i}/{total}] {nct_id} — {len(missing)} gap(s): "
                  f"{missing[:3]}{'...' if len(missing) > 3 else ''}")

            if dry_run:
                continue

            # ── 5. Collect unique version numbers needed ──────────────────────
            versions_needed = set()
            for pv, cv in missing:
                versions_needed.add(pv)
                versions_needed.add(cv)

            # ── 6. Fetch each unique version once (cached) ───────────────────
            meta_by_ver   = {h["version"]: h for h in history}
            version_cache = {}
            for v in sorted(versions_needed):
                try:
                    version_cache[v] = get_version_data(nct_id, v)
                    time.sleep(0.25)
                except Exception as e:
                    print(f"    ERROR fetching v{v}: {e}")

            # ── 7. Compute diff for each missing pair ─────────────────────────
            total_versions = len(history)
            pairs_written  = 0
            for pv, cv in missing:
                if pv not in version_cache or cv not in version_cache:
                    print(f"    SKIP ({pv}→{cv}) — version data unavailable")
                    continue

                curr_meta     = meta_by_ver.get(cv, {})
                module_labels = curr_meta.get("moduleLabels", [])
                field_chg     = compare_versions(
                    version_cache[pv], version_cache[cv], module_labels
                )

                batch.append({
                    "nct_id":             nct_id,
                    "note":               "",
                    "total_versions":     total_versions,
                    "prev_version":       pv,
                    "curr_version":       cv,
                    "prev_date":          meta_by_ver.get(pv, {}).get("date", ""),
                    "curr_date":          curr_meta.get("date", ""),
                    "curr_status":        curr_meta.get("status", ""),
                    "modules_changed":    "; ".join(module_labels),
                    "field_change_count": len(field_chg),
                    "field_changes":      format_field_changes(field_chg),
                    "curr_full_data":     version_cache[cv],
                })
                pairs_written        += 1
                grand_pairs_written  += 1

            if pairs_written:
                print(f"    → {pairs_written} pair(s) queued for write")

            # ── 8. Flush every BATCH_SIZE rows ────────────────────────────────
            if len(batch) >= BATCH_SIZE:
                db.upsert_version_pairs(dept_name, batch)
                print(f"  -- Flushed {len(batch)} pairs ({grand_pairs_written} total) --")
                batch = []

        except Exception as e:
            print(f"  [{i}/{total}] {nct_id} — ERROR: {e}")

        time.sleep(0.2)

    # ── Final flush ───────────────────────────────────────────────────────────
    if batch and not dry_run:
        db.upsert_version_pairs(dept_name, batch)
        print(f"  -- Final flush: {len(batch)} pairs --")

    print(f"""
Done.
  NCTs checked      : {total}
  NCTs with gaps    : {grand_ncts_with_gaps}
  Missing pairs found: {grand_gaps_found}
  Pairs written     : {grand_pairs_written if not dry_run else '(dry run)'}
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fill missing intermediate version pairs")
    parser.add_argument("dept",        help='Department e.g. "ADC"')
    parser.add_argument("--start",     type=int, default=0, help="Resume from this NCT index")
    parser.add_argument("--dry-run",   action="store_true",  help="Report gaps without writing")
    args = parser.parse_args()

    fill_gaps(args.dept, args.start, args.dry_run)
