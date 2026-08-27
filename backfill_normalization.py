"""
backfill_normalization.py
--------------------------
Applies normalization to all existing organized_trials rows for a dept.

Normalizes:
  - Sponsors                    (lead sponsor display name)
  - responsiblePartyleadSponsor (same field, different column)
  - Collaborators               (' | ' separated orgs)
  - Primary Drug                (' | ' separated drugs)
  - conditions                  (', ' separated conditions)

Run AFTER create_alias_tables.py has been executed and alias tables are seeded.

Usage:
    python backfill_normalization.py           # defaults to ADC
    python backfill_normalization.py ASMB
    python backfill_normalization.py ADC --dry-run   # preview only, no DB writes
"""

import sys
import db
import norm

DEPT       = "ADC"
BATCH_SIZE = 100
DRY_RUN    = False

for arg in sys.argv[1:]:
    if arg == "--dry-run":
        DRY_RUN = True
    elif not arg.startswith("-"):
        DEPT = arg


def _diff(label: str, old: str, new: str, changes: list) -> None:
    if old != new:
        changes.append(f"  {label}:\n    OLD: {old!r}\n    NEW: {new!r}")


def main():
    print("=" * 60)
    print(f"Backfill Normalization — {DEPT}{' [DRY RUN]' if DRY_RUN else ''}")
    print("=" * 60)

    norm.reload()

    conn = db._db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nct_id,
                   "Sponsors",
                   "Collaborators",
                   conditions,
                   "responsiblePartyleadSponsor"
            FROM organized_trials
            WHERE dept = %s
            ORDER BY nct_id
            """,
            (DEPT,),
        )
        rows = cur.fetchall()

    total = len(rows)
    print(f"Total rows: {total}\n")

    updated = 0
    skipped = 0

    conn2 = db._db()
    with conn2.cursor() as cur:
        for i, row in enumerate(rows, 1):
            nct_id = row["nct_id"]

            old_sponsor      = row["Sponsors"] or ""
            old_collabs      = row["Collaborators"] or ""
            old_conditions   = row["conditions"] or ""
            old_resp_sponsor = row["responsiblePartyleadSponsor"] or ""

            new_sponsor      = norm.normalize_org(old_sponsor)
            new_collabs      = norm.normalize_orgs_field(old_collabs)
            new_conditions   = norm.normalize_conditions_field(old_conditions)
            new_resp_sponsor = norm.normalize_org(old_resp_sponsor)

            changes: list[str] = []
            _diff("Sponsors",                    old_sponsor,      new_sponsor,      changes)
            _diff("Collaborators",               old_collabs,      new_collabs,      changes)
            _diff("conditions",                  old_conditions,   new_conditions,   changes)
            _diff("responsiblePartyleadSponsor", old_resp_sponsor, new_resp_sponsor, changes)

            if changes:
                if DRY_RUN:
                    print(f"[{i}/{total}] {nct_id}  — would change:")
                    for c in changes:
                        print(c)
                else:
                    cur.execute(
                        """
                        UPDATE organized_trials
                        SET "Sponsors"                    = %s,
                            "Collaborators"               = %s,
                            conditions                    = %s,
                            "responsiblePartyleadSponsor" = %s
                        WHERE nct_id = %s AND dept = %s
                        """,
                        (new_sponsor, new_collabs, new_conditions,
                         new_resp_sponsor, nct_id, DEPT),
                    )
                updated += 1
            else:
                skipped += 1

            if not DRY_RUN and i % BATCH_SIZE == 0:
                conn2.commit()
                print(f"  [{i}/{total}] committed — updated so far: {updated}")

        if not DRY_RUN:
            conn2.commit()

    print(f"\n{'='*60}")
    print("Done." + (" [DRY RUN — no changes written]" if DRY_RUN else ""))
    print(f"  Would update : {updated}" if DRY_RUN else f"  Updated : {updated}")
    print(f"  Skipped (no change): {skipped}")
    print("=" * 60)


if __name__ == "__main__":
    main()
