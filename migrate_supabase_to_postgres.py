"""
migrate_supabase_to_postgres.py
---------------------------------
Migrate ALL data from old Supabase to new PostgreSQL (dricenta.com, schema "CT").

Run once. Safe to re-run — uses upsert everywhere.

Tables migrated (in order):
  1.  tracking_list
  2.  rejected_trials
  3.  pipeline_state
  4.  organized_trials
  5.  version_cache
  6.  new_candidates_log
  7.  unmatched_log
  8.  modified_log
  9.  field_changes_log     (skips prev_full_data — not in new schema)
  10. canonical_changes_log
  11. run_history
  12. nct_version_pairs      (skips prev_full_data — not in new schema)
"""

import sys
from pathlib import Path
from supabase import create_client

sys.path.insert(0, str(Path(__file__).parent))
import db

# ── Old Supabase (read-only due to storage limit, but reads work fine) ─────────
_SUPA_URL = "https://kgbpqpdflqiawidpkfeh.supabase.co"
_SUPA_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtnYnBxcGRmbHFpYXdpZHBrZmVoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MzY2MDA3NywiZXhwIjoyMDk5MjM2MDc3fQ.9qRdp_s79YSbNc6KYA2U2FL5Lr6b06HzKaJHZpZmtGI"
supa = create_client(_SUPA_URL, _SUPA_KEY)

_READ_BATCH  = 500   # rows per Supabase API page
_WRITE_BATCH = 500   # rows per PostgreSQL insert


# ── Helpers ────────────────────────────────────────────────────────────────────

def fetch_all(table: str, select: str = "*") -> list[dict]:
    """Read every row from old Supabase with pagination."""
    rows = []
    offset = 0
    while True:
        resp = (
            supa.table(table)
            .select(select)
            .range(offset, offset + _READ_BATCH - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        print(f"  [Supabase] {table}: fetched {len(rows)} rows...", end="\r", flush=True)
        if len(batch) < _READ_BATCH:
            break
        offset += _READ_BATCH
    print(f"  [Supabase] {table}: {len(rows)} rows total          ")
    return rows


def drop_cols(rows: list[dict], *cols: str) -> list[dict]:
    """Strip auto-generated or unwanted columns before insert."""
    return [{k: v for k, v in r.items() if k not in cols} for r in rows]


def migrate(label: str, rows: list[dict], conflict_cols: list[str], batch_size: int = _WRITE_BATCH):
    if not rows:
        print(f"  [PG] {label}: nothing to insert")
        return
    db._bulk_upsert(label, rows, conflict_cols, batch_size=batch_size)
    print(f"  [PG] {label}: {len(rows)} rows upserted")


def migrate_append(label: str, rows: list[dict], batch_size: int = _WRITE_BATCH):
    """For append-only tables — plain INSERT (new DB is empty so no duplicates)."""
    if not rows:
        print(f"  [PG] {label}: nothing to insert")
        return
    db._bulk_insert(label, rows, batch_size=batch_size)
    print(f"  [PG] {label}: {len(rows)} rows inserted")


# ── 1. tracking_list ──────────────────────────────────────────────────────────
print("\n[1/12] tracking_list")
rows = fetch_all("tracking_list")
migrate("tracking_list", rows, ["nct_id", "dept"])

# ── 2. rejected_trials ────────────────────────────────────────────────────────
print("\n[2/12] rejected_trials")
rows = fetch_all("rejected_trials")
migrate("rejected_trials", rows, ["nct_id", "dept"])

# ── 3. pipeline_state ─────────────────────────────────────────────────────────
print("\n[3/12] pipeline_state")
rows = fetch_all("pipeline_state")
rows = drop_cols(rows, "updated_at")   # let PG use DEFAULT NOW()
migrate("pipeline_state", rows, ["dept"])

# ── 4. organized_trials ───────────────────────────────────────────────────────
print("\n[4/12] organized_trials")
rows = fetch_all("organized_trials")
rows = drop_cols(rows, "last_upserted_at")  # let PG use DEFAULT NOW()
migrate("organized_trials", rows, ["nct_id"])

# ── 5. version_cache ──────────────────────────────────────────────────────────
print("\n[5/12] version_cache")
rows = fetch_all("version_cache")
rows = drop_cols(rows, "updated_at")
migrate("version_cache", rows, ["nct_id", "dept"])

# ── 6. new_candidates_log ─────────────────────────────────────────────────────
print("\n[6/12] new_candidates_log")
rows = fetch_all("new_candidates_log")
rows = drop_cols(rows, "id")   # BIGSERIAL — auto-generated in PG
migrate("new_candidates_log", rows, ["nct_id", "dept", "run_date"])

# ── 7. unmatched_log ──────────────────────────────────────────────────────────
print("\n[7/12] unmatched_log")
rows = fetch_all("unmatched_log")
migrate("unmatched_log", rows, ["nct_id", "dept"])

# ── 8. modified_log ───────────────────────────────────────────────────────────
print("\n[8/12] modified_log")
rows = fetch_all("modified_log")
rows = drop_cols(rows, "id")
migrate_append("modified_log", rows)

# ── 9. field_changes_log ─────────────────────────────────────────────────────
print("\n[9/12] field_changes_log")
# Select without prev_full_data (not in new schema)
rows = fetch_all(
    "field_changes_log",
    "id,nct_id,dept,run_date,note,total_versions,prev_version,curr_version,"
    "prev_date,curr_date,curr_status,modules_changed,field_change_count,"
    "field_changes,curr_full_data",
)
rows = drop_cols(rows, "id")
migrate_append("field_changes_log", rows)

# ── 10. canonical_changes_log ─────────────────────────────────────────────────
print("\n[10/12] canonical_changes_log")
rows = fetch_all("canonical_changes_log")
rows = drop_cols(rows, "id")
migrate_append("canonical_changes_log", rows)

# ── 11. run_history ───────────────────────────────────────────────────────────
print("\n[11/12] run_history")
rows = fetch_all("run_history")
rows = drop_cols(rows, "id", "created_at")
migrate_append("run_history", rows)

# ── 12. nct_version_pairs ────────────────────────────────────────────────────
print("\n[12/12] nct_version_pairs (largest table — skips prev_full_data)")
print("  Fetching metadata first (no JSON)...")
meta_rows = fetch_all(
    "nct_version_pairs",
    "id,nct_id,dept,note,total_versions,prev_version,curr_version,"
    "prev_date,curr_date,curr_status,modules_changed,field_change_count,"
    "field_changes,fetched_at",
)
meta_rows = drop_cols(meta_rows, "id", "fetched_at")
print(f"  {len(meta_rows)} version pair rows found")

print("  Now fetching curr_full_data in small batches (this may take a while)...")
# Fetch curr_full_data separately in small pages to avoid large payloads
pair_ids_data: dict[tuple, dict] = {}  # (nct_id, dept, prev_v, curr_v) -> row
offset = 0
SMALL_BATCH = 50  # small batch for large JSON
total_fetched = 0
while True:
    resp = (
        supa.table("nct_version_pairs")
        .select("nct_id,dept,prev_version,curr_version,curr_full_data")
        .range(offset, offset + SMALL_BATCH - 1)
        .execute()
    )
    batch = resp.data or []
    for r in batch:
        key = (r["nct_id"], r["dept"], r["prev_version"], r["curr_version"])
        pair_ids_data[key] = r.get("curr_full_data")
    total_fetched += len(batch)
    print(f"  curr_full_data: fetched {total_fetched} rows...", end="\r", flush=True)
    if len(batch) < SMALL_BATCH:
        break
    offset += SMALL_BATCH
print(f"  curr_full_data: {total_fetched} rows fetched          ")

# Merge metadata + curr_full_data
final_rows = []
for r in meta_rows:
    key = (r["nct_id"], r["dept"], r["prev_version"], r["curr_version"])
    r["curr_full_data"] = pair_ids_data.get(key)
    final_rows.append(r)

# Upsert in very small batches (each row may contain 100-300KB JSON)
print(f"  Writing {len(final_rows)} rows to PostgreSQL in batches of 3...")
db._bulk_upsert(
    "nct_version_pairs",
    final_rows,
    ["nct_id", "dept", "prev_version", "curr_version"],
    batch_size=3,
)
print(f"  [PG] nct_version_pairs: {len(final_rows)} rows upserted")

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*55)
print("Migration complete! Verifying counts in PostgreSQL...")
print("="*55)

checks = [
    ("tracking_list",       "SELECT COUNT(*) FROM tracking_list"),
    ("rejected_trials",     "SELECT COUNT(*) FROM rejected_trials"),
    ("organized_trials",    "SELECT COUNT(*) FROM organized_trials"),
    ("version_cache",       "SELECT COUNT(*) FROM version_cache"),
    ("new_candidates_log",  "SELECT COUNT(*) FROM new_candidates_log"),
    ("unmatched_log",       "SELECT COUNT(*) FROM unmatched_log"),
    ("modified_log",        "SELECT COUNT(*) FROM modified_log"),
    ("field_changes_log",   "SELECT COUNT(*) FROM field_changes_log"),
    ("nct_version_pairs",   "SELECT COUNT(*) FROM nct_version_pairs"),
    ("run_history",         "SELECT COUNT(*) FROM run_history"),
]
for label, sql in checks:
    with db._cur() as cur:
        cur.execute(sql)
        count = cur.fetchone()["count"]
    print(f"  {label:<25} {count:>8} rows")

print("\nDone! PostgreSQL is now ready to use.")
