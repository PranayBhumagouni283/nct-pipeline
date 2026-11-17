"""
db_query_handler.py -- Structured DB queries for the conversational agent.

Handles:
  DB_COUNT / DB_LIST / DB_STATS  → organized_trials + tracking_list
  CHANGE_HISTORY                 → trial_history (version snapshots, historical changes)
  CHANGE_LOG                     → field_changes_log (recent pipeline run changes)

All queries are parameterized — no string interpolation of user input.
"""
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

# ── Phase / Status canonical maps ─────────────────────────────────────────────

# DB stores comma-space format for multi-phase: "PHASE1, PHASE2" not "PHASE1_PHASE2"
PHASE_MAP = {
    "phase 1":       ["PHASE1", "EARLY_PHASE1"],
    "phase 2":       ["PHASE2"],
    "phase 3":       ["PHASE3"],
    "phase 1/2":     ["PHASE1, PHASE2"],
    "phase 2/3":     ["PHASE2, PHASE3"],
    "phase 4":       ["PHASE4"],
    "early phase 1": ["EARLY_PHASE1"],
}

STATUS_MAP = {
    "recruiting":               "RECRUITING",
    "completed":                "COMPLETED",
    "active":                   "ACTIVE_NOT_RECRUITING",
    "active not recruiting":    "ACTIVE_NOT_RECRUITING",
    "not yet recruiting":       "NOT_YET_RECRUITING",
    "terminated":               "TERMINATED",
    "withdrawn":                "WITHDRAWN",
    "suspended":                "SUSPENDED",
    "enrolling by invitation":  "ENROLLING_BY_INVITATION",
}

DEPT_ALIASES = {
    "adc":                  "ADC",
    "antibody drug":        "ADC",
    "infectious":           "Infectious Diseases",
    "infectious diseases":  "Infectious Diseases",
    "id":                   "Infectious Diseases",
    "liver":                "Liver Diseases",
    "liver diseases":       "Liver Diseases",
    "ld":                   "Liver Diseases",
}

PHASE_DISPLAY = {
    "PHASE1":          "Phase 1",
    "EARLY_PHASE1":    "Early Phase 1",
    "PHASE2":          "Phase 2",
    "PHASE3":          "Phase 3",
    "PHASE4":          "Phase 4",
    "PHASE1_PHASE2":   "Phase 1/2",
    "PHASE1, PHASE2":  "Phase 1/2",
    "PHASE2_PHASE3":   "Phase 2/3",
    "PHASE2, PHASE3":  "Phase 2/3",
    "NA":              "N/A",
    "":                "Not specified",
}

STATUS_DISPLAY = {
    "RECRUITING":               "Recruiting",
    "COMPLETED":                "Completed",
    "ACTIVE_NOT_RECRUITING":    "Active (not recruiting)",
    "NOT_YET_RECRUITING":       "Not yet recruiting",
    "TERMINATED":               "Terminated",
    "WITHDRAWN":                "Withdrawn",
    "SUSPENDED":                "Suspended",
    "UNKNOWN":                  "Unknown",
    "ENROLLING_BY_INVITATION":  "Enrolling by invitation",
    "APPROVED_FOR_MARKETING":   "Approved for marketing",
    "AVAILABLE":                "Available",
    "NO_LONGER_AVAILABLE":      "No longer available",
}


# ── DB connection ─────────────────────────────────────────────────────────────

_conn = None

def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        dsn = os.getenv("DATABASE_URL") or os.getenv("DB_URL")
        _conn = psycopg2.connect(
            dsn,
            cursor_factory=psycopg2.extras.RealDictCursor,
            keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=5,
        )
        _conn.autocommit = True
    return _conn


def _cur():
    return _get_conn().cursor()


# ── Filter builders ───────────────────────────────────────────────────────────

def _build_ot_filters(filters: dict, prefix: str = "") -> tuple[str, list]:
    """
    Build WHERE clause for organized_trials queries.
    prefix: table alias prefix e.g. 'ot.' for joined queries.
    """
    clauses = []
    params  = []
    p = prefix

    dept = (filters.get("dept") or "").strip().lower()
    if dept:
        canonical = DEPT_ALIASES.get(dept, filters["dept"])
        clauses.append(f'{p}dept = %s')
        params.append(canonical)

    phase = (filters.get("phase") or "").strip().lower()
    if phase:
        db_phases = PHASE_MAP.get(phase)
        if db_phases:
            if len(db_phases) == 1:
                clauses.append(f'{p}phases = %s')
                params.append(db_phases[0])
            else:
                clauses.append(f'{p}phases = ANY(%s)')
                params.append(db_phases)

    status = (filters.get("status") or "").strip().lower()
    if status:
        db_status = STATUS_MAP.get(status)
        if db_status:
            clauses.append(f'{p}"Overall Status" = %s')
            params.append(db_status)

    indication = (filters.get("indication") or "").strip()
    if indication:
        # Use tracking_list subquery for curated indication match
        clauses.append(f'''
            {p}nct_id IN (
                SELECT nct_id FROM "CT".tracking_list
                WHERE indication ILIKE %s
            )
        ''')
        params.append(f"%{indication}%")
    else:
        # No indication: restrict to trials that exist in tracking_list (indication='')
        # so handle_list total matches handle_count (both sourced from tracking_list)
        clauses.append(f'''
            {p}nct_id IN (
                SELECT nct_id FROM "CT".tracking_list
                WHERE indication = ''
            )
        ''')

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _build_tl_filters(filters: dict) -> tuple[str, list, bool]:
    """
    Build WHERE clause for tracking_list queries.
    Returns (where_clause, params, needs_ot_join).
    needs_ot_join=True when phase/status filters require joining organized_trials.
    """
    clauses    = []
    params     = []
    needs_join = False

    dept = (filters.get("dept") or "").strip().lower()
    if dept:
        canonical = DEPT_ALIASES.get(dept, filters["dept"])
        clauses.append("tl.dept = %s")
        params.append(canonical)

    indication = (filters.get("indication") or "").strip()
    if indication:
        clauses.append("tl.indication ILIKE %s")
        params.append(f"%{indication}%")
    else:
        clauses.append("tl.indication = ''")

    phase = (filters.get("phase") or "").strip().lower()
    if phase:
        db_phases = PHASE_MAP.get(phase)
        if db_phases:
            needs_join = True
            if len(db_phases) == 1:
                clauses.append("ot.phases = %s")
                params.append(db_phases[0])
            else:
                clauses.append("ot.phases = ANY(%s)")
                params.append(db_phases)

    status = (filters.get("status") or "").strip().lower()
    if status:
        db_status = STATUS_MAP.get(status)
        if db_status:
            needs_join = True
            clauses.append('ot."Overall Status" = %s')
            params.append(db_status)

    where = "WHERE " + " AND ".join(clauses)
    return where, params, needs_join


# ── Query handlers ────────────────────────────────────────────────────────────

def handle_count(filters: dict) -> dict:
    """
    COUNT with optional filters. Total from tracking_list (matches dashboard).
    Phase/status breakdowns join organized_trials.
    Phase and status filters are correctly applied to the total count via OT join.
    """
    tl_where, tl_params, needs_join = _build_tl_filters(filters)
    ot_join = (
        'JOIN "CT".organized_trials ot ON ot.nct_id = tl.nct_id AND ot.dept = tl.dept'
        if needs_join else ""
    )

    # Breakdown always joins OT (for phase/status display)
    breakdown_where = tl_where.replace("tl.indication", "tl.indication")

    with _cur() as cur:
        # Total — uses OT join when phase/status filters present
        cur.execute(f'''
            SELECT COUNT(DISTINCT tl.nct_id) AS total
            FROM "CT".tracking_list tl
            {ot_join}
            {tl_where}
        ''', tl_params)
        total = cur.fetchone()["total"]

        # Dept breakdown
        cur.execute(f'''
            SELECT tl.dept, COUNT(DISTINCT tl.nct_id) AS cnt
            FROM "CT".tracking_list tl
            {ot_join}
            {tl_where}
            GROUP BY tl.dept ORDER BY cnt DESC
        ''', tl_params)
        by_dept = [dict(r) for r in cur.fetchall()]

        # Phase breakdown — always join OT
        phase_join = 'JOIN "CT".organized_trials ot ON ot.nct_id = tl.nct_id AND ot.dept = tl.dept'
        # Rebuild tl_where without phase/status since they're already in OT join above
        base_tl_clauses = []
        base_tl_params  = []
        dept_raw = (filters.get("dept") or "").strip().lower()
        if dept_raw:
            canonical = DEPT_ALIASES.get(dept_raw, filters["dept"])
            base_tl_clauses.append("tl.dept = %s")
            base_tl_params.append(canonical)
        indication = (filters.get("indication") or "").strip()
        if indication:
            base_tl_clauses.append("tl.indication ILIKE %s")
            base_tl_params.append(f"%{indication}%")
        else:
            base_tl_clauses.append("tl.indication = ''")
        base_where = "WHERE " + " AND ".join(base_tl_clauses)

        cur.execute(f'''
            SELECT ot.phases, COUNT(DISTINCT tl.nct_id) AS cnt
            FROM "CT".tracking_list tl
            {phase_join}
            {base_where}
            GROUP BY ot.phases ORDER BY cnt DESC
        ''', base_tl_params)
        by_phase = [
            {"phase": PHASE_DISPLAY.get(r["phases"] or "", r["phases"] or "Not specified"),
             "count": r["cnt"]}
            for r in cur.fetchall()
        ]

        # Status breakdown
        cur.execute(f'''
            SELECT ot."Overall Status", COUNT(DISTINCT tl.nct_id) AS cnt
            FROM "CT".tracking_list tl
            {phase_join}
            {base_where}
            GROUP BY ot."Overall Status" ORDER BY cnt DESC
        ''', base_tl_params)
        by_status = [
            {"status": STATUS_DISPLAY.get(r["Overall Status"], r["Overall Status"]),
             "count": r["cnt"]}
            for r in cur.fetchall()
        ]

    return {
        "type":      "count",
        "total":     total,
        "by_dept":   by_dept,
        "by_phase":  by_phase,
        "by_status": by_status,
        "filters":   filters,
    }


def handle_list(filters: dict, limit: int = 15) -> dict:
    """
    List trials with key fields, filtered.
    Sort: recruiting first, then not-yet-recruiting, then active, then rest.
    Within each group: most recent Start Date first.
    Indication filter uses tracking_list subquery for curated match (consistent with handle_count).
    """
    where, params = _build_ot_filters(filters)

    with _cur() as cur:
        cur.execute(f'''
            SELECT
                nct_id, dept, "Brief Title", "Overall Status", phases,
                conditions, "Primary Drug", "Enrollment",
                "Primary Completion Date", "Sponsors", "Start Date"
            FROM "CT".organized_trials
            {where}
            ORDER BY
                CASE "Overall Status"
                    WHEN 'RECRUITING'              THEN 1
                    WHEN 'NOT_YET_RECRUITING'      THEN 2
                    WHEN 'ACTIVE_NOT_RECRUITING'   THEN 3
                    ELSE 4
                END,
                TO_DATE(
                    CASE WHEN LENGTH(COALESCE("Start Date", '')) = 7
                         THEN "Start Date" || '-01'
                         WHEN LENGTH(COALESCE("Start Date", '')) >= 10
                         THEN SUBSTRING("Start Date", 1, 10)
                         ELSE NULL
                    END,
                    'YYYY-MM-DD'
                ) DESC NULLS LAST
            LIMIT %s
        ''', params + [limit])
        rows = [dict(r) for r in cur.fetchall()]

        cur.execute(f'SELECT COUNT(DISTINCT nct_id) AS total FROM "CT".organized_trials {where}', params)
        total = cur.fetchone()["total"]

    for row in rows:
        row["phases"]         = PHASE_DISPLAY.get(row["phases"] or "", "Not specified")
        row["Overall Status"] = STATUS_DISPLAY.get(row["Overall Status"], row["Overall Status"])

    return {
        "type":    "list",
        "total":   total,
        "shown":   len(rows),
        "trials":  rows,
        "filters": filters,
    }


def handle_stats(filters: dict, metric: str | None) -> dict:
    """Aggregated statistics — phase, status, enrollment, dept breakdown."""
    where, params = _build_ot_filters(filters)

    results = {}

    with _cur() as cur:
        # Phase distribution
        cur.execute(f'''
            SELECT phases, COUNT(DISTINCT nct_id) AS cnt
            FROM "CT".organized_trials {where}
            GROUP BY phases ORDER BY cnt DESC
        ''', params)
        results["phase_distribution"] = [
            {"phase": PHASE_DISPLAY.get(r["phases"] or "", "Not specified"), "count": r["cnt"]}
            for r in cur.fetchall()
        ]

        # Status distribution
        cur.execute(f'''
            SELECT "Overall Status", COUNT(DISTINCT nct_id) AS cnt
            FROM "CT".organized_trials {where}
            GROUP BY "Overall Status" ORDER BY cnt DESC
        ''', params)
        results["status_distribution"] = [
            {"status": STATUS_DISPLAY.get(r["Overall Status"], r["Overall Status"]),
             "count": r["cnt"]}
            for r in cur.fetchall()
        ]

        # Enrollment stats (numeric only)
        cur.execute(f'''
            SELECT
                COUNT(DISTINCT nct_id)                                                        AS total_trials,
                COUNT(DISTINCT CASE WHEN "Enrollment" ~ '^[0-9]+$' THEN nct_id END)          AS trials_with_enrollment,
                AVG(CASE WHEN "Enrollment" ~ '^[0-9]+$' THEN "Enrollment"::int END)::int      AS avg_enrollment,
                PERCENTILE_CONT(0.5) WITHIN GROUP (
                    ORDER BY CASE WHEN "Enrollment" ~ '^[0-9]+$' THEN "Enrollment"::int END
                )::int                                                                         AS median_enrollment,
                MAX(CASE WHEN "Enrollment" ~ '^[0-9]+$' THEN "Enrollment"::int END)           AS max_enrollment,
                SUM(CASE WHEN "Enrollment" ~ '^[0-9]+$' THEN "Enrollment"::int END)           AS total_enrollment
            FROM "CT".organized_trials {where}
        ''', params)
        results["enrollment_stats"] = dict(cur.fetchone())

        # Dept breakdown
        cur.execute(f'''
            SELECT dept, COUNT(DISTINCT nct_id) AS cnt
            FROM "CT".organized_trials {where}
            GROUP BY dept ORDER BY cnt DESC
        ''', params)
        results["dept_breakdown"] = [dict(r) for r in cur.fetchall()]

        # Study type breakdown
        cur.execute(f'''
            SELECT "studyType", COUNT(DISTINCT nct_id) AS cnt
            FROM "CT".organized_trials {where}
            GROUP BY "studyType" ORDER BY cnt DESC
        ''', params)
        results["study_type_breakdown"] = [dict(r) for r in cur.fetchall()]

    return {
        "type":    "stats",
        "filters": filters,
        **results,
    }


# ── Change / History handlers ─────────────────────────────────────────────────

# Canonical module names as they appear in modules_changed column
MODULE_ALIASES = {
    "outcome":          "Outcome Measures",
    "outcomes":         "Outcome Measures",
    "primary endpoint": "Outcome Measures",
    "endpoints":        "Outcome Measures",
    "eligibility":      "Eligibility",
    "inclusion":        "Eligibility",
    "exclusion":        "Eligibility",
    "status":           "Study Status",
    "study status":     "Study Status",
    "arms":             "Arms and Interventions",
    "interventions":    "Arms and Interventions",
    "design":           "Study Design",
    "protocol":         "Study Design",
    "contacts":         "Contacts/Locations",
    "locations":        "Contacts/Locations",
    "sites":            "Contacts/Locations",
    "description":      "Study Description",
    "conditions":       "Conditions",
    "sponsor":          "Sponsor/Collaborators",
    "sponsors":         "Sponsor/Collaborators",
    "identification":   "Study Identification",
    "results":          "Outcome Measures (Results)",
    "adverse events":   "Adverse Events",
}


def _resolve_module(module_raw: str) -> str:
    """Resolve user module input to canonical DB module name (or pass through)."""
    return MODULE_ALIASES.get(module_raw.strip().lower(), module_raw.strip())


def handle_trial_history(filters: dict) -> dict:
    """
    Query trial_history table.
    query_type:
      latest        — the single most recent version for a specific nct_id (full field_changes)
      versions      — all versions for a specific nct_id (ordered by version_num DESC)
      recent        — most recently changed trials (dept/module/date filters), 1 row per trial
      module_stats  — which modules change most often across the dataset
    Filters: nct_id, dept, module, date_from, date_to, query_type
    """
    query_type = (filters.get("query_type") or "recent").lower()
    nct_id     = (filters.get("nct_id") or "").strip().upper()
    dept_raw   = (filters.get("dept") or "").strip().lower()
    dept       = DEPT_ALIASES.get(dept_raw, filters.get("dept") or "") if dept_raw else ""
    module_raw = (filters.get("module") or "").strip()
    module     = _resolve_module(module_raw) if module_raw else ""
    date_from  = (filters.get("date_from") or "").strip()
    date_to    = (filters.get("date_to") or "").strip()

    with _cur() as cur:

        # ── latest: single most recent version for one trial (full field_changes) ──
        if query_type == "latest" and nct_id:
            cur.execute('''
                SELECT th.version_num, th.version_date, th.overall_status,
                       th.modules_changed, th.field_changes,
                       ot."Brief Title", ot.dept,
                       (SELECT COUNT(*) FROM "CT".trial_history WHERE nct_id = th.nct_id) AS total_versions
                FROM "CT".trial_history th
                LEFT JOIN "CT".organized_trials ot
                       ON ot.nct_id = th.nct_id AND ot.dept = th.dept
                WHERE th.nct_id = %s
                ORDER BY th.version_num DESC
                LIMIT 1
            ''', (nct_id,))
            row = cur.fetchone()
            if not row:
                return {"type": "trial_history_latest", "nct_id": nct_id,
                        "found": False, "filters": filters}
            row = dict(row)
            row["overall_status"] = STATUS_DISPLAY.get(row["overall_status"] or "", row["overall_status"] or "")
            return {
                "type":           "trial_history_latest",
                "nct_id":         nct_id,
                "found":          True,
                "total_versions": row.pop("total_versions"),
                "latest":         row,
                "filters":        filters,
            }

        # ── versions: full version list for one trial ──────────────────────────
        if query_type == "versions" and nct_id:
            cur.execute('''
                SELECT th.version_num, th.version_date, th.overall_status,
                       th.modules_changed, th.field_changes,
                       ot."Brief Title", ot.dept
                FROM "CT".trial_history th
                LEFT JOIN "CT".organized_trials ot
                       ON ot.nct_id = th.nct_id AND ot.dept = th.dept
                WHERE th.nct_id = %s
                ORDER BY th.version_num DESC
            ''', (nct_id,))
            rows = [dict(r) for r in cur.fetchall()]

            cur.execute('SELECT COUNT(*) AS cnt FROM "CT".trial_history WHERE nct_id = %s', (nct_id,))
            total = cur.fetchone()["cnt"]

            for r in rows:
                r["overall_status"] = STATUS_DISPLAY.get(r["overall_status"] or "", r["overall_status"] or "")

            return {
                "type":       "trial_history_versions",
                "nct_id":     nct_id,
                "total":      total,
                "shown":      len(rows),
                "versions":   rows,
                "filters":    filters,
            }

        # ── module_stats: top modules by change frequency ──────────────────────
        if query_type == "module_stats":
            clauses, params = [], []
            if dept:
                clauses.append("dept = %s"); params.append(dept)
            if date_from:
                clauses.append("version_date >= %s"); params.append(date_from)
            if date_to:
                clauses.append("version_date <= %s"); params.append(date_to)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

            # unnest semicolon-separated modules_changed into individual module counts
            cur.execute(f'''
                SELECT TRIM(m) AS module, COUNT(*) AS change_count
                FROM "CT".trial_history th,
                     UNNEST(STRING_TO_ARRAY(modules_changed, ';')) AS m
                {where}
                GROUP BY TRIM(m)
                ORDER BY change_count DESC
                LIMIT 20
            ''', params)
            module_counts = [dict(r) for r in cur.fetchall()]

            cur.execute(f'SELECT COUNT(*) AS cnt FROM "CT".trial_history {where}', params)
            total_versions = cur.fetchone()["cnt"]
            cur.execute(f'SELECT COUNT(DISTINCT nct_id) AS cnt FROM "CT".trial_history {where}', params)
            unique_trials = cur.fetchone()["cnt"]

            return {
                "type":            "trial_history_module_stats",
                "total_versions":  total_versions,
                "unique_trials":   unique_trials,
                "module_counts":   module_counts,
                "filters":         filters,
            }

        # ── recent: latest change per trial, filtered ──────────────────────────
        clauses, params = [], []
        if nct_id:
            clauses.append("th.nct_id = %s"); params.append(nct_id)
        if dept:
            clauses.append("th.dept = %s"); params.append(dept)
        if module:
            clauses.append("th.modules_changed ILIKE %s"); params.append(f"%{module}%")
        if date_from:
            clauses.append("th.version_date >= %s"); params.append(date_from)
        if date_to:
            clauses.append("th.version_date <= %s"); params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        # Use ROW_NUMBER so we can pick the latest version per trial AND sort by date DESC
        cur.execute(f'''
            SELECT nct_id, dept, version_num, version_date, overall_status,
                   modules_changed, field_changes, "Brief Title"
            FROM (
                SELECT th.nct_id, th.dept, th.version_num, th.version_date,
                       th.overall_status, th.modules_changed, th.field_changes,
                       ot."Brief Title",
                       ROW_NUMBER() OVER (
                           PARTITION BY th.nct_id, th.dept
                           ORDER BY th.version_num DESC
                       ) AS rn
                FROM "CT".trial_history th
                LEFT JOIN "CT".organized_trials ot
                       ON ot.nct_id = th.nct_id AND ot.dept = th.dept
                {where}
            ) sub
            WHERE rn = 1
            ORDER BY version_date DESC
            LIMIT 20
        ''', params)
        rows = [dict(r) for r in cur.fetchall()]

        cur.execute(f'''
            SELECT COUNT(DISTINCT (th.nct_id, th.dept)) AS cnt
            FROM "CT".trial_history th {where}
        ''', params)
        total = cur.fetchone()["cnt"]

        for r in rows:
            r["overall_status"] = STATUS_DISPLAY.get(r["overall_status"] or "", r["overall_status"] or "")

        # ── Fallback when date_from filter returns no results ──────────────────
        # Tell the user WHEN the last change actually occurred instead of "no results"
        no_recent_context = None
        if total == 0 and date_from:
            fallback_clauses = [c for c in clauses if "version_date >=" not in c]
            fallback_where = ("WHERE " + " AND ".join(fallback_clauses)) if fallback_clauses else ""
            fallback_params = [p for p, c in zip(params, clauses) if "version_date >=" not in c]

            cur.execute(f'''
                SELECT th.nct_id, th.dept, th.version_date, th.modules_changed,
                       ot."Brief Title"
                FROM "CT".trial_history th
                LEFT JOIN "CT".organized_trials ot
                       ON ot.nct_id = th.nct_id AND ot.dept = th.dept
                {fallback_where}
                ORDER BY th.version_date DESC
                LIMIT 3
            ''', fallback_params)
            last_changes = [dict(r) for r in cur.fetchall()]

            if last_changes:
                no_recent_context = {
                    "date_from":    date_from,
                    "module":       module or None,
                    "last_changes": last_changes,
                }

        return {
            "type":               "trial_history_recent",
            "total":              total,
            "shown":              len(rows),
            "trials":             rows,
            "no_recent_context":  no_recent_context,
            "filters":            filters,
        }


def handle_change_log(filters: dict) -> dict:
    """
    Query field_changes_log (recent pipeline run changes).
    query_type:
      latest_run    — changes from the most recent run (or a specific run_date)
      trial         — all log entries for a specific nct_id
      module_filter — entries where a specific module changed
      stats         — aggregated: changes per run_date, top modules
    Filters: nct_id, dept, indication, module, run_date, date_from, date_to, query_type
    """
    query_type  = (filters.get("query_type") or "latest_run").lower()
    nct_id      = (filters.get("nct_id") or "").strip().upper()
    dept_raw    = (filters.get("dept") or "").strip().lower()
    dept        = DEPT_ALIASES.get(dept_raw, filters.get("dept") or "") if dept_raw else ""
    indication  = (filters.get("indication") or "").strip()
    module_raw  = (filters.get("module") or "").strip()
    module      = _resolve_module(module_raw) if module_raw else ""
    run_date    = (filters.get("run_date") or "").strip()
    date_from   = (filters.get("date_from") or "").strip()
    date_to     = (filters.get("date_to") or "").strip()

    with _cur() as cur:

        # ── stats: aggregated change statistics ────────────────────────────────
        if query_type == "stats":
            clauses, params = [], []
            if dept:
                clauses.append("dept = %s"); params.append(dept)
            if indication:
                clauses.append("indication ILIKE %s"); params.append(f"%{indication}%")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

            # Changes per run date
            cur.execute(f'''
                SELECT run_date, COUNT(*) AS trials_changed,
                       SUM(field_change_count) AS total_field_changes
                FROM "CT".field_changes_log {where}
                GROUP BY run_date ORDER BY run_date DESC
                LIMIT 10
            ''', params)
            by_run_date = [dict(r) for r in cur.fetchall()]

            # Top modules
            cur.execute(f'''
                SELECT TRIM(m) AS module, COUNT(*) AS change_count
                FROM "CT".field_changes_log,
                     UNNEST(STRING_TO_ARRAY(modules_changed, ';')) AS m
                {where}
                GROUP BY TRIM(m)
                ORDER BY change_count DESC
                LIMIT 15
            ''', params)
            top_modules = [dict(r) for r in cur.fetchall()]

            cur.execute(f'SELECT COUNT(DISTINCT nct_id) AS cnt FROM "CT".field_changes_log {where}', params)
            unique_trials = cur.fetchone()["cnt"]
            cur.execute(f'SELECT COUNT(*) AS cnt FROM "CT".field_changes_log {where}', params)
            total_entries = cur.fetchone()["cnt"]

            return {
                "type":           "change_log_stats",
                "unique_trials":  unique_trials,
                "total_entries":  total_entries,
                "by_run_date":    by_run_date,
                "top_modules":    top_modules,
                "filters":        filters,
            }

        # ── Build base WHERE for list-style queries ────────────────────────────
        clauses, params = [], []
        if nct_id:
            clauses.append("fcl.nct_id = %s"); params.append(nct_id)
        if dept:
            clauses.append("fcl.dept = %s"); params.append(dept)
        if indication:
            clauses.append("fcl.indication ILIKE %s"); params.append(f"%{indication}%")
        if module:
            clauses.append("fcl.modules_changed ILIKE %s"); params.append(f"%{module}%")

        if query_type == "latest_run":
            if run_date:
                clauses.append("fcl.run_date = %s"); params.append(run_date)
            else:
                # Scope MAX(run_date) to the dept when one is provided,
                # so "latest ADC run" uses ADC's last run, not the global max
                if dept:
                    clauses.append('''fcl.run_date = (
                        SELECT MAX(run_date) FROM "CT".field_changes_log WHERE dept = %s
                    )''')
                    params.append(dept)
                else:
                    clauses.append('''fcl.run_date = (
                        SELECT MAX(run_date) FROM "CT".field_changes_log
                    )''')
        elif query_type in ("trial", "module_filter"):
            if date_from:
                clauses.append("fcl.run_date >= %s"); params.append(date_from)
            if date_to:
                clauses.append("fcl.run_date <= %s"); params.append(date_to)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        cur.execute(f'''
            SELECT fcl.nct_id, fcl.dept, fcl.indication, fcl.run_date,
                   fcl.prev_version, fcl.curr_version,
                   fcl.prev_date, fcl.curr_date, fcl.curr_status,
                   fcl.modules_changed,
                   COALESCE(fcl.field_change_count, 0) AS field_change_count,
                   fcl.field_changes,
                   ot."Brief Title",
                   CASE WHEN fcl.prev_version IS NULL THEN true ELSE false END AS is_new_trial
            FROM "CT".field_changes_log fcl
            LEFT JOIN "CT".organized_trials ot
                   ON ot.nct_id = fcl.nct_id AND ot.dept = fcl.dept
            {where}
            ORDER BY fcl.run_date DESC,
                     COALESCE(fcl.field_change_count, 0) DESC
            LIMIT 25
        ''', params)
        rows = [dict(r) for r in cur.fetchall()]

        count_where = where.replace("fcl.", "")
        cur.execute(f'SELECT COUNT(*) AS cnt FROM "CT".field_changes_log {count_where}', params)
        total = cur.fetchone()["cnt"]

        for r in rows:
            r["curr_status"] = STATUS_DISPLAY.get(r["curr_status"] or "", r["curr_status"] or "")
            r["modules_changed"] = r["modules_changed"] or ""

        return {
            "type":    "change_log_entries",
            "total":   total,
            "shown":   len(rows),
            "entries": rows,
            "filters": filters,
        }


# ── KOL handler ───────────────────────────────────────────────────────────────

def handle_kol(filters: dict) -> dict:
    """
    Query KOL / investigator data from "Overall Officials" and "Central Contacts" columns.

    query_type:
      "trial"     -- KOLs for a specific NCT ID (nct_id required)
      "search"    -- trials involving a named investigator (kol_name required)
      "dept_list" -- all unique KOLs across a dept (dept or no filter)

    filters keys: nct_id, kol_name, dept
    """
    query_type = filters.get("query_type", "trial")
    nct_id     = (filters.get("nct_id") or "").strip().upper()
    kol_name   = (filters.get("kol_name") or "").strip()
    dept_raw   = (filters.get("dept") or "").strip().lower()
    dept       = DEPT_ALIASES.get(dept_raw, filters.get("dept") or "") if dept_raw else ""

    cur = _cur()

    # ── trial: KOLs for one specific trial ─────────────────────────────────
    if query_type == "trial" and nct_id:
        cur.execute('''
            SELECT nct_id, dept, "Brief Title", "Overall Officials", "Central Contacts"
            FROM "CT".organized_trials
            WHERE nct_id = %s
            LIMIT 1
        ''', (nct_id,))
        row = cur.fetchone()
        if not row:
            return {"type": "kol_trial", "nct_id": nct_id, "found": False}
        return {
            "type":          "kol_trial",
            "nct_id":        nct_id,
            "found":         True,
            "brief_title":   row["Brief Title"],
            "dept":          row["dept"],
            "kols":          row["Overall Officials"] or "",
            "kol_contacts":  row["Central Contacts"] or "",
        }

    # ── search: find trials by investigator name ────────────────────────────
    if query_type == "search" and kol_name:
        clauses = ['("Overall Officials" ILIKE %s OR "Central Contacts" ILIKE %s)']
        params  = [f"%{kol_name}%", f"%{kol_name}%"]
        if dept:
            clauses.append("dept = %s")
            params.append(dept)
        where = "WHERE " + " AND ".join(clauses)
        cur.execute(f'''
            SELECT nct_id, dept, "Brief Title", "Overall Status", "Overall Officials" AS kols
            FROM "CT".organized_trials
            {where}
            ORDER BY "Last Update Post Date" DESC NULLS LAST
            LIMIT 20
        ''', params)
        rows = [dict(r) for r in cur.fetchall()]
        return {
            "type":     "kol_search",
            "kol_name": kol_name,
            "dept":     dept,
            "total":    len(rows),
            "trials":   rows,
        }

    # ── dept_list: all KOLs across a dept ──────────────────────────────────
    clauses = ['"Overall Officials" IS NOT NULL', '"Overall Officials" != \'\'']
    params: list = []
    if dept:
        clauses.append("dept = %s")
        params.append(dept)
    where = "WHERE " + " AND ".join(clauses)
    cur.execute(f'''
        SELECT nct_id, "Brief Title", "Overall Status", "Overall Officials" AS kols, "Central Contacts" AS kol_contacts
        FROM "CT".organized_trials
        {where}
        ORDER BY "Last Update Post Date" DESC NULLS LAST
        LIMIT 30
    ''', params or None)
    rows = [dict(r) for r in cur.fetchall()]
    return {
        "type":   "kol_dept_list",
        "dept":   dept,
        "total":  len(rows),
        "trials": rows,
    }


# ── Public dispatch ───────────────────────────────────────────────────────────

def execute_db_query(intent: str, filters: dict, db_metric: str | None = None) -> dict:
    """
    Dispatch to the right handler based on intent.
    intent: DB_COUNT | DB_LIST | DB_STATS | CHANGE_HISTORY | CHANGE_LOG | KOL
    """
    if intent == "DB_COUNT":
        return handle_count(filters)
    elif intent == "DB_LIST":
        return handle_list(filters)
    elif intent == "DB_STATS":
        return handle_stats(filters, db_metric)
    elif intent == "CHANGE_HISTORY":
        return handle_trial_history(filters)
    elif intent == "CHANGE_LOG":
        return handle_change_log(filters)
    elif intent == "KOL":
        return handle_kol(filters)
    else:
        raise ValueError(f"Unknown DB intent: {intent}")
