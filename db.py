"""
db.py — PostgreSQL database interface for the NCT Combined Pipeline.
Connects to dricenta.com PostgreSQL, schema "CT" (case-sensitive).
All pipeline reads and writes go through this module.
Uses psycopg2 — install with: pip install psycopg2-binary
"""

import math
import os
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.extensions
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_DB_URL = os.environ["DATABASE_URL"]
_BATCH  = 500  # rows per INSERT/UPSERT batch

# Serialize Python dicts/lists as JSONB automatically
psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)
psycopg2.extensions.register_adapter(list, psycopg2.extras.Json)

_conn: Optional[psycopg2.extensions.connection] = None


def _new_conn() -> psycopg2.extensions.connection:
    """Open a fresh connection with keepalive and correct search_path."""
    conn = psycopg2.connect(
        _DB_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute('SET search_path TO "CT"')
    return conn


def _db() -> psycopg2.extensions.connection:
    """Return a verified live connection. Reconnects transparently on any drop."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = _new_conn()
        return _conn
    # Actively ping the server — catches silent server-side drops that
    # _conn.closed misses (it stays 0 until a query actually fails)
    try:
        with _conn.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg2.OperationalError:
        _conn = _new_conn()
    return _conn


def _cur():
    return _db().cursor()


def _to_int(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _clean(v) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    return str(v) if v != "" else ""


def _qi(name: str) -> str:
    """Quote a PostgreSQL identifier (handles spaces and uppercase)."""
    return '"' + name.replace('"', '""') + '"'


def _bulk_upsert(
    table: str,
    records: list[dict],
    conflict_cols: list[str],
    batch_size: int = _BATCH,
) -> None:
    """Generic INSERT … ON CONFLICT DO UPDATE for any table."""
    if not records:
        return
    global _conn
    cols        = list(records[0].keys())
    update_cols = [c for c in cols if c not in conflict_cols]
    col_sql      = ", ".join(_qi(c) for c in cols)
    conflict_sql = ", ".join(_qi(c) for c in conflict_cols)
    update_sql   = ", ".join(f"{_qi(c)} = EXCLUDED.{_qi(c)}" for c in update_cols)
    sql = (
        f"INSERT INTO {_qi(table)} ({col_sql}) VALUES %s "
        f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_sql}"
    )
    rows = [[r.get(c) for c in cols] for r in records]
    try:
        with _cur() as cur:
            for i in range(0, len(rows), batch_size):
                psycopg2.extras.execute_values(cur, sql, rows[i : i + batch_size])
    except psycopg2.OperationalError:
        _conn = None
        with _cur() as cur:
            for i in range(0, len(rows), batch_size):
                psycopg2.extras.execute_values(cur, sql, rows[i : i + batch_size])


def _bulk_insert(table: str, records: list[dict], batch_size: int = _BATCH) -> None:
    """Generic INSERT (no conflict handling) for append-only tables."""
    if not records:
        return
    global _conn
    cols    = list(records[0].keys())
    col_sql = ", ".join(_qi(c) for c in cols)
    sql     = f"INSERT INTO {_qi(table)} ({col_sql}) VALUES %s"
    rows    = [[r.get(c) for c in cols] for r in records]
    try:
        with _cur() as cur:
            for i in range(0, len(rows), batch_size):
                psycopg2.extras.execute_values(cur, sql, rows[i : i + batch_size])
    except psycopg2.OperationalError:
        _conn = None
        with _cur() as cur:
            for i in range(0, len(rows), batch_size):
                psycopg2.extras.execute_values(cur, sql, rows[i : i + batch_size])


# ── READ ──────────────────────────────────────────────────────────


def load_departments() -> list[str]:
    with _cur() as cur:
        cur.execute("SELECT name FROM departments ORDER BY name")
        return [r["name"] for r in cur.fetchall()]


def load_tracking_list(dept: str, indication: str = "") -> list[str]:
    with _cur() as cur:
        cur.execute(
            "SELECT nct_id FROM tracking_list WHERE dept = %s AND indication = %s",
            (dept, indication),
        )
        ids = [r["nct_id"] for r in cur.fetchall()]
    print(f"  [DB] Tracking list: {len(ids)} NCTs for {dept}/{indication or 'asset'}")
    return ids


def load_rejected_trials(dept: str, indication: str = "") -> set[str]:
    with _cur() as cur:
        cur.execute(
            "SELECT nct_id FROM rejected_trials WHERE dept = %s AND indication = %s",
            (dept, indication),
        )
        ids = {r["nct_id"] for r in cur.fetchall()}
    print(f"  [DB] Rejected list: {len(ids)} blocked NCTs for {dept}/{indication or 'asset'}")
    return ids


def load_pipeline_state(dept: str, indication: str = "") -> dict:
    with _cur() as cur:
        cur.execute(
            "SELECT etag, last_run_date FROM pipeline_state WHERE dept = %s AND indication = %s",
            (dept, indication),
        )
        row = cur.fetchone()
    if row:
        lrd = str(row["last_run_date"]) if row.get("last_run_date") else None
        return {"etag": row.get("etag"), "last_run_date": lrd}
    return {"etag": None, "last_run_date": None}


def load_version_cache(dept: str) -> tuple[dict, dict]:
    with _cur() as cur:
        cur.execute("SELECT * FROM version_cache WHERE dept = %s", (dept,))
        rows = cur.fetchall()
    versions: dict[str, int]  = {}
    result:   dict[str, dict] = {}
    for r in rows:
        nct = r["nct_id"]
        if r.get("curr_version") is not None:
            try:
                versions[nct] = int(r["curr_version"])
            except (ValueError, TypeError):
                pass
        result[nct] = {
            "nct_id":             nct,
            "note":               r.get("note", ""),
            "total_versions":     r.get("total_versions", ""),
            "prev_version":       r.get("prev_version", ""),
            "curr_version":       r.get("curr_version", ""),
            "prev_date":          r.get("prev_date", ""),
            "curr_date":          r.get("curr_date", ""),
            "curr_status":        r.get("curr_status", ""),
            "modules_changed":    r.get("modules_changed", ""),
            "field_change_count": r.get("field_change_count", 0),
            "field_changes":      r.get("field_changes", ""),
        }
    print(f"  [DB] Version cache: {len(versions)} entries for {dept}")
    return versions, result


def load_organized_base(dept: str) -> dict:
    with _cur() as cur:
        cur.execute("SELECT * FROM organized_trials WHERE dept = %s", (dept,))
        rows = cur.fetchall()
    result = {}
    for r in rows:
        nct = r["nct_id"]
        row_dict = {k: (v if v is not None else "") for k, v in r.items()}
        row_dict["NCT ID"] = nct
        result[nct] = row_dict
    print(f"  [DB] Organized base: {len(result)} rows for {dept}")
    return result


def load_all_interventions(dept: str) -> dict:
    with _cur() as cur:
        cur.execute(
            'SELECT nct_id, "Interventions" FROM organized_trials WHERE dept = %s',
            (dept,),
        )
        return {r["nct_id"]: r.get("Interventions") or "" for r in cur.fetchall()}


def load_ncts_with_version_pairs(dept: str) -> set:
    with _cur() as cur:
        cur.execute(
            "SELECT DISTINCT nct_id FROM nct_version_pairs WHERE dept = %s", (dept,)
        )
        return {r["nct_id"] for r in cur.fetchall()}


def load_indication_keywords(dept: str, indication: str) -> list[str]:
    """Return keywords for a dept+indication from dept_indications (pipe-separated → list)."""
    with _cur() as cur:
        cur.execute(
            "SELECT keywords FROM dept_indications WHERE dept = %s AND indication = %s",
            (dept, indication),
        )
        row = cur.fetchone()
    if not row or not row.get("keywords"):
        return []
    return [k.strip().lower() for k in row["keywords"].split("|") if k.strip()]


def load_all_indications(dept: str) -> list[str]:
    """Return all indication names configured for a dept (from dept_indications)."""
    with _cur() as cur:
        cur.execute(
            "SELECT indication FROM dept_indications WHERE dept = %s ORDER BY indication",
            (dept,),
        )
        return [r["indication"] for r in cur.fetchall()]


def load_dept_keywords(dept: str) -> list[dict]:
    """Return [{drug_name, alias_names}] for the dept (replaces direct _db() calls)."""
    with _cur() as cur:
        cur.execute(
            "SELECT drug_name, alias_names FROM dept_keywords WHERE dept = %s", (dept,)
        )
        return [dict(r) for r in cur.fetchall()]


def load_dept_general_terms(dept: str) -> list[str]:
    """Return list of general search terms for the dept."""
    with _cur() as cur:
        cur.execute(
            "SELECT term FROM dept_general_terms WHERE dept = %s", (dept,)
        )
        return [r["term"] for r in cur.fetchall()]


# ── WRITE ─────────────────────────────────────────────────────────


def save_pipeline_state(dept: str, etag: str, last_run_date: str, indication: str = ""):
    _bulk_upsert(
        "pipeline_state",
        [{"dept": dept, "indication": indication, "etag": etag, "last_run_date": last_run_date}],
        ["dept", "indication"],
    )
    print(f"  [DB] State saved: {dept}/{indication or 'asset'} last_run={last_run_date}")


def upsert_tracking_list(dept: str, nct_ids: list[str], added_by: str = "pipeline", indication: str = ""):
    if not nct_ids:
        return
    records = [{"nct_id": n, "dept": dept, "indication": indication, "added_by": added_by} for n in nct_ids]
    _bulk_upsert("tracking_list", records, ["nct_id", "dept", "indication"])
    print(f"  [DB] Tracking list upserted: {len(nct_ids)} NCTs for {dept}/{indication or 'asset'}")


def upsert_organized_trials(dept: str, rows: list[dict]):
    if not rows:
        return

    now_ts = datetime.now(timezone.utc).isoformat()

    def _record(row: dict) -> dict | None:
        nct = _clean(row.get("NCT ID"))
        if not nct:
            return None
        record: dict = {"nct_id": nct, "dept": dept}
        for key, val in row.items():
            if key in ("NCT ID", "last_upserted_at"):
                continue  # handled explicitly below
            record[key] = _clean(val)
        record["last_upserted_at"] = now_ts
        return record

    records = [r for row in rows if (r := _record(row))]
    _bulk_upsert("organized_trials", records, ["nct_id"])
    print(f"  [DB] Organized trials upserted: {len(records)} rows for {dept}")


def upsert_version_cache(dept: str, all_rows: list[dict]):
    if not all_rows:
        return

    def _record(r: dict) -> dict | None:
        nct = _clean(r.get("nct_id"))
        if not nct:
            return None
        return {
            "nct_id":             nct,
            "dept":               dept,
            "curr_version":       _to_int(r.get("curr_version")),
            "curr_date":          _clean(r.get("curr_date")),
            "curr_status":        _clean(r.get("curr_status")),
            "note":               _clean(r.get("note")),
            "total_versions":     _to_int(r.get("total_versions")),
            "prev_version":       _to_int(r.get("prev_version")),
            "prev_date":          _clean(r.get("prev_date")),
            "modules_changed":    _clean(r.get("modules_changed")),
            "field_change_count": _to_int(r.get("field_change_count")),
            "field_changes":      _clean(r.get("field_changes")),
        }

    records = [r for row in all_rows if (r := _record(row))]
    _bulk_upsert("version_cache", records, ["nct_id", "dept"])
    print(f"  [DB] Version cache upserted: {len(records)} rows for {dept}")


def insert_new_candidates(dept: str, run_date: str, trials: list[dict], indication: str = ""):
    if not trials:
        return

    # Skip NCTs already decided (Approved/Rejected) for this dept+indication.
    # Their run_date differs each pipeline run, so the conflict key won't catch them —
    # we filter explicitly to avoid re-surfacing them as Pending.
    nct_ids = [t["NCT ID"] for t in trials if t.get("NCT ID")]
    if nct_ids:
        with _cur() as cur:
            cur.execute(
                """
                SELECT DISTINCT nct_id FROM new_candidates_log
                WHERE dept = %s AND indication = %s
                  AND decision IN ('Approved', 'Rejected')
                  AND nct_id IN %s
                """,
                (dept, indication, tuple(nct_ids)),
            )
            decided = {r["nct_id"] for r in cur.fetchall()}
        if decided:
            trials = [t for t in trials if t.get("NCT ID") not in decided]
            print(f"  [DB] New candidates: skipped {len(decided)} already-decided NCTs for {dept}/{indication or 'asset'}")

    if not trials:
        return

    records = [
        {
            "nct_id":             _clean(t.get("NCT ID")),
            "dept":               dept,
            "indication":         indication,
            "run_date":           run_date,
            "decision":           "Pending",
            "title":              _clean(t.get("Title")),
            "conditions":         _clean(t.get("Conditions")),
            "mesh_conditions":    _clean(t.get("MeSH Conditions")),
            "interventions":      _clean(t.get("Interventions")),
            "sponsor":            _clean(t.get("Sponsor")),
            "collaborators":      _clean(t.get("Collaborators")),
            "phase":              _clean(t.get("Phase")),
            "study_type":         _clean(t.get("Study Type")),
            "enrollment":         _clean(t.get("Enrollment")),
            "recruitment_status": _clean(t.get("Recruitment Status")),
            "study_first_posted": _clean(t.get("Study First Posted")),
            "last_updated":       _clean(t.get("Last Updated")),
            "link":               _clean(t.get("Link")),
        }
        for t in trials
        if t.get("NCT ID")
    ]
    _bulk_upsert("new_candidates_log", records, ["nct_id", "dept", "indication", "run_date"])
    print(f"  [DB] New candidates inserted: {len(records)} for {dept}/{indication or 'asset'}")


def upsert_unmatched(dept: str, run_date: str, trials: list[dict], indication: str = ""):
    """
    Deduplicated upsert for unmatched trials.
    New NCTs → insert with Pending decision.
    Existing NCTs → update last_seen_date only (decision preserved).
    """
    if not trials:
        return
    records = [
        {
            "nct_id":             _clean(t.get("NCT ID")),
            "dept":               dept,
            "indication":         indication,
            "run_date":           run_date,
            "title":              _clean(t.get("Title")),
            "conditions":         _clean(t.get("Conditions")),
            "mesh_conditions":    _clean(t.get("MeSH Conditions")),
            "interventions":      _clean(t.get("Interventions")),
            "sponsor":            _clean(t.get("Sponsor")),
            "collaborators":      _clean(t.get("Collaborators")),
            "phase":              _clean(t.get("Phase")),
            "study_type":         _clean(t.get("Study Type")),
            "enrollment":         _clean(t.get("Enrollment")),
            "recruitment_status": _clean(t.get("Recruitment Status")),
            "link":               _clean(t.get("Link")),
        }
        for t in trials
        if t.get("NCT ID")
    ]
    if not records:
        return
    # Insert new rows as Pending; on conflict only update last_seen_date
    # RETURNING (xmax = 0) distinguishes new inserts (True) from updates (False)
    sql = """
        INSERT INTO unmatched_log
            (nct_id, dept, indication, first_seen_date, last_seen_date,
             decision, title, conditions, mesh_conditions, interventions,
             sponsor, collaborators, phase, study_type, enrollment,
             recruitment_status, link)
        VALUES %s
        ON CONFLICT (nct_id, dept, indication)
        DO UPDATE SET last_seen_date = EXCLUDED.last_seen_date
        RETURNING (xmax = 0) AS is_new
    """
    rows = [
        (
            r["nct_id"], r["dept"], r["indication"], r["run_date"], r["run_date"],
            "Pending", r["title"], r["conditions"], r["mesh_conditions"],
            r["interventions"], r["sponsor"], r["collaborators"],
            r["phase"], r["study_type"], r["enrollment"],
            r["recruitment_status"], r["link"],
        )
        for r in records
    ]
    new_count = 0
    def _run(cur):
        nonlocal new_count
        for i in range(0, len(rows), _BATCH):
            psycopg2.extras.execute_values(cur, sql, rows[i : i + _BATCH], fetch=True)
            new_count += sum(1 for r in cur.fetchall() if r[0])
    try:
        with _cur() as cur:
            _run(cur)
    except psycopg2.OperationalError:
        global _conn
        _conn = None
        new_count = 0
        with _cur() as cur:
            _run(cur)
    print(f"  [DB] Unmatched upserted: {len(records)} found, {new_count} new  for {dept}/{indication or 'asset'}")
    return new_count


def insert_modified_log(dept: str, run_date: str, trials: list[dict], indication: str = ""):
    if not trials:
        return
    records = [
        {
            "nct_id":             _clean(t.get("NCT ID")),
            "dept":               dept,
            "indication":         indication,
            "run_date":           run_date,
            "last_updated":       _clean(t.get("Last Updated")),
            "recruitment_status": _clean(t.get("Recruitment Status")),
            "title":              _clean(t.get("Title")),
            "link":               _clean(t.get("Link")),
        }
        for t in trials
        if t.get("NCT ID")
    ]
    _bulk_upsert("modified_log", records, ["nct_id", "dept", "indication", "run_date"])
    print(f"  [DB] Modified log inserted: {len(records)} for {dept}/{indication or 'asset'}")


def insert_field_changes(dept: str, run_date: str, rows: list[dict], indication: str = ""):
    if not rows:
        return
    records = [
        {
            "nct_id":             _clean(r.get("nct_id")),
            "dept":               dept,
            "indication":         indication,
            "run_date":           run_date,
            "note":               _clean(r.get("note")),
            "total_versions":     _to_int(r.get("total_versions")),
            "prev_version":       _to_int(r.get("prev_version")),
            "curr_version":       _to_int(r.get("curr_version")),
            "prev_date":          _clean(r.get("prev_date")),
            "curr_date":          _clean(r.get("curr_date")),
            "curr_status":        _clean(r.get("curr_status")),
            "modules_changed":    _clean(r.get("modules_changed")),
            "field_change_count": _to_int(r.get("field_change_count")),
            "field_changes":      _clean(r.get("field_changes")),
            "curr_full_data":     r.get("curr_full_data") or None,
        }
        for r in rows
        if r.get("nct_id")
    ]
    _bulk_insert("field_changes_log", records)
    print(f"  [DB] Field changes inserted: {len(records)} for {dept}/{indication or 'asset'}")


def insert_canonical_changes(dept: str, run_date: str, redirects: list[dict]):
    if not redirects:
        return
    records = [
        {
            "dept":             dept,
            "run_date":         run_date,
            "requested_nct":    _clean(r.get("requested")),
            "canonical_nct":    _clean(r.get("canonical")),
            "already_tracked":  bool(r.get("in_input", False)),
        }
        for r in redirects
    ]
    _bulk_insert("canonical_changes_log", records)
    print(f"  [DB] Canonical changes inserted: {len(records)} for {dept}")


def batch_update_primary_drug(dept: str, updates: dict) -> None:
    if not updates:
        return
    records = [
        {"nct_id": k, "dept": dept, "Primary Drug": v}
        for k, v in updates.items()
    ]
    _bulk_upsert("organized_trials", records, ["nct_id"])
    tagged = sum(1 for v in updates.values() if v)
    print(f"  [DB] Primary Drug tagged: {tagged}/{len(records)} rows for {dept}")


def upsert_version_pairs(dept: str, records: list[dict]) -> None:
    if not records:
        return
    rows = []
    for r in records:
        nct  = _clean(r.get("nct_id"))
        prev = r.get("prev_version") if r.get("prev_version") not in (None, "") else r.get("from_version")
        curr = r.get("curr_version") if r.get("curr_version") not in (None, "") else r.get("to_version")
        if not nct or curr in (None, ""):
            continue
        try:
            prev_int = int(prev) if prev not in (None, "") else 0
            curr_int = int(curr)
        except (ValueError, TypeError):
            continue
        rows.append({
            "nct_id":             nct,
            "dept":               dept,
            "note":               _clean(r.get("note", "")),
            "total_versions":     int(r.get("total_versions", 0) or 0),
            "prev_version":       prev_int,
            "curr_version":       curr_int,
            "prev_date":          _clean(r.get("prev_date") or r.get("from_date", "")),
            "curr_date":          _clean(r.get("curr_date") or r.get("to_date", "")),
            "curr_status":        _clean(r.get("curr_status") or r.get("status", "")),
            "modules_changed":    _clean(r.get("modules_changed", "")),
            "field_change_count": int(r.get("field_change_count", 0) or 0),
            "field_changes":      _clean(r.get("field_changes", "")),
            "curr_full_data":     r.get("curr_full_data") or None,
        })
    if not rows:
        return
    # Small batch — each row carries a full CT.gov JSON blob (~100-300 KB)
    try:
        _bulk_upsert(
            "nct_version_pairs", rows,
            ["nct_id", "dept", "prev_version", "curr_version"],
            batch_size=3,
        )
        print(f"  [DB] Version pairs upserted: {len(rows)} for {dept}")
    except psycopg2.errors.InsufficientPrivilege:
        # RLS is enabled on nct_version_pairs and pranay has no matching policy.
        # Skip silently — pipeline continues; fix by asking admin:
        #   GRANT BYPASS RLS TO pranay;
        print(f"  [DB] WARNING: nct_version_pairs RLS blocked {len(rows)} pairs — skipping (admin must run: GRANT BYPASS RLS TO pranay)")


def upsert_trial_history(dept: str, records: list[dict]) -> None:
    if not records:
        return
    rows = []
    for r in records:
        nct = _clean(r.get("nct_id"))
        if not nct:
            continue
        rows.append({
            "nct_id":          nct,
            "dept":            dept,
            "version_num":     _to_int(r.get("version_num")),
            "version_date":    _clean(r.get("version_date")),
            "overall_status":  _clean(r.get("overall_status")),
            "modules_changed": _clean(r.get("modules_changed")),
            "field_changes":   _clean(r.get("field_changes")),
            "full_data":       r.get("full_data") or {},
        })
    _bulk_upsert("trial_history", rows, ["nct_id", "dept", "version_num"])
    print(f"  [DB] Trial history upserted: {len(rows)} snapshots for {dept}")


def insert_run_history(
    dept:            str,
    run_date:        str,
    new_candidates:  int,
    unmatched:       int,
    modified_trials: int,
    field_diffs:     int,
    canonical_fixes: int,
    duration_s:      float,
    output_file:     str,
    indication:      str = "",
):
    _bulk_insert("run_history", [{
        "dept":            dept,
        "indication":      indication,
        "run_date":        run_date,
        "new_candidates":  new_candidates,
        "unmatched":       unmatched,
        "modified_trials": modified_trials,
        "field_diffs":     field_diffs,
        "canonical_fixes": canonical_fixes,
        "duration_s":      round(duration_s, 1),
        "output_file":     output_file,
    }])
    print(f"  [DB] Run history saved: {dept}/{indication or 'asset'} {run_date} ({duration_s:.0f}s)")


# ── Dashboard actions (called by web app via API routes) ──────────


def approve_trial(nct_id: str, dept: str, source: str, indication: str = ""):
    table = "new_candidates_log" if source == "new_candidates" else "unmatched_log"
    today = _date.today().isoformat()
    with _cur() as cur:
        cur.execute(
            f"UPDATE {_qi(table)} SET decision = %s, decided_at = %s "
            f"WHERE nct_id = %s AND dept = %s AND indication = %s",
            ("Approved", today, nct_id, dept, indication),
        )
    upsert_tracking_list(dept, [nct_id], added_by="approved", indication=indication)
    print(f"  [DB] Approved {nct_id} → tracking list for {dept}/{indication or 'asset'}")


def reject_trial(nct_id: str, dept: str, source: str, indication: str = ""):
    today = _date.today().isoformat()
    _bulk_upsert(
        "rejected_trials",
        [{"nct_id": nct_id, "dept": dept, "indication": indication, "source": source, "rejected_date": today}],
        ["nct_id", "dept", "indication"],
    )
    table = "new_candidates_log" if source == "new_candidates" else "unmatched_log"
    with _cur() as cur:
        cur.execute(
            f"DELETE FROM {_qi(table)} WHERE nct_id = %s AND dept = %s AND indication = %s",
            (nct_id, dept, indication),
        )
    print(f"  [DB] Rejected {nct_id} for {dept}/{indication or 'asset'} — added to blocklist")


def delete_from_tracking_list(nct_id: str, dept: str, indication: str = ""):
    with _cur() as cur:
        cur.execute(
            "DELETE FROM tracking_list WHERE nct_id = %s AND dept = %s AND indication = %s",
            (nct_id, dept, indication),
        )
    print(f"  [DB] Removed {nct_id} from tracking list for {dept}/{indication or 'asset'}")


def unreject_trial(nct_id: str, dept: str, indication: str = ""):
    with _cur() as cur:
        cur.execute(
            "DELETE FROM rejected_trials WHERE nct_id = %s AND dept = %s AND indication = %s",
            (nct_id, dept, indication),
        )
    print(f"  [DB] Un-rejected {nct_id} for {dept}/{indication or 'asset'}")


def sync_approved_to_tracking(dept: str, indication: str = "") -> list[str]:
    with _cur() as cur:
        cur.execute(
            "SELECT nct_id FROM new_candidates_log WHERE dept = %s AND indication = %s AND decision = 'Approved'",
            (dept, indication),
        )
        r1 = [r["nct_id"] for r in cur.fetchall()]
        cur.execute(
            "SELECT nct_id FROM unmatched_log WHERE dept = %s AND indication = %s AND decision = 'Approved'",
            (dept, indication),
        )
        r2 = [r["nct_id"] for r in cur.fetchall()]
    approved = list(set(r1 + r2))
    if not approved:
        print(f"  [DB] No pending approvals for {dept}/{indication or 'asset'}")
        return []
    upsert_tracking_list(dept, approved, added_by="approved", indication=indication)
    with _cur() as cur:
        for nct_id in approved:
            cur.execute(
                "UPDATE new_candidates_log SET decision = 'Tracked' "
                "WHERE nct_id = %s AND dept = %s AND indication = %s AND decision = 'Approved'",
                (nct_id, dept, indication),
            )
            cur.execute(
                "UPDATE unmatched_log SET decision = 'Tracked' "
                "WHERE nct_id = %s AND dept = %s AND indication = %s AND decision = 'Approved'",
                (nct_id, dept, indication),
            )
    print(f"  [DB] Synced {len(approved)} approved NCT(s) to tracking list for {dept}/{indication or 'asset'}")
    return approved
