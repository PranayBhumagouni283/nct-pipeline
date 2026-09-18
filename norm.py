"""
norm.py — Normalization module for the NCT Combined Pipeline.

Single source of truth for each alias type:
  - org_aliases      (DB table)  : company/sponsor name variants → canonical
  - dept_indications (DB table)  : indication keywords → canonical indication name
                                   (same table used by discovery pipeline)
  - dept_keywords    (DB table)  : drug name + aliases → canonical drug name
                                   (handled by tag_primary_drug, not this module)

Auto-clean also strips trailing legal suffixes (Inc., LLC, Ltd., etc.) from
org names even without an explicit alias entry.

Usage in pipeline:
    import norm
    sponsor    = norm.normalize_org(raw_sponsor_name)
    collabs    = norm.normalize_orgs_field(pipe_separated_collaborators)
    conditions = norm.normalize_conditions_field(comma_separated_conditions)
    phase_str  = norm.normalize_phase(design.get("phases", []))

To force reload after updating alias tables:
    norm.reload()
"""

import logging
import re
import db

# ── Internal state ─────────────────────────────────────────────────
_org_aliases:       dict[str, str] = {}
_condition_aliases: dict[str, str] = {}   # keyword.lower() → canonical indication
_loaded      = False
_load_failed = False

# Sponsors seen this session that are NOT in org_aliases (for B-02 backfill tracking).
# Access via norm.get_unresolved_sponsors() after a pipeline run.
_unresolved_sponsors: set[str] = set()

# Trailing legal entity suffixes to strip automatically.
# Applied up to 3 times so "Co., Ltd." resolves cleanly.
_LEGAL_RE = re.compile(
    r',?\s*('
    r'Inc\.|Incorporated|LLC|L\.L\.C\.|Ltd\.|Limited|'
    r'Corp\.|Corporation|Co\.,?\s*Ltd\.?|GmbH|S\.A\.|AG|PLC|'
    r'B\.V\.|N\.V\.|SE|S\.p\.A\.|K\.K\.|AB|AS|OY|SAS|SRL'
    r')\s*$',
    flags=re.IGNORECASE,
)


def _load() -> None:
    global _org_aliases, _condition_aliases, _loaded, _load_failed
    if _loaded:
        return
    try:
        with db._cur() as cur:
            # Org aliases
            cur.execute('SELECT alias, canonical FROM org_aliases')
            _org_aliases = {r['alias'].lower(): r['canonical'] for r in cur.fetchall()}

            # Condition aliases — derived from dept_indications keywords.
            # Each keyword in dept_indications maps to its indication (canonical name).
            # Keywords are pipe-separated. Load across all depts; first definition wins
            # on conflict (indications are universal medical terms).
            cur.execute('SELECT indication, keywords FROM dept_indications WHERE keywords IS NOT NULL')
            for row in cur.fetchall():
                canonical = row['indication'].strip()
                for kw in (row['keywords'] or '').split('|'):
                    kw = kw.strip().lower()
                    if kw and kw not in _condition_aliases:
                        _condition_aliases[kw] = canonical

        _loaded = True
        print(
            f"  [Norm] Aliases loaded — "
            f"org:{len(_org_aliases)}  cond:{len(_condition_aliases)} (from dept_indications)"
        )
    except Exception as e:
        print(f"  [Norm] Warning: alias tables not available ({e}) — auto-clean only")
        _loaded      = True   # prevent repeated load attempts
        _load_failed = True   # flag so callers can detect degraded mode


def reload() -> None:
    """Force reload from DB (call after updating org_aliases or dept_indications)."""
    global _loaded
    _loaded = False
    _load()


# ── Organization normalization ─────────────────────────────────────────────────

def _auto_clean_org(name: str) -> str:
    """Strip trailing legal suffixes and normalize internal whitespace."""
    name = re.sub(r'\s+', ' ', name.strip())
    for _ in range(3):
        cleaned = _LEGAL_RE.sub('', name).strip().rstrip(',').strip()
        if cleaned == name:
            break
        name = cleaned
    return name


def normalize_org(name: str) -> str:
    """
    Return canonical org name.
    Priority: org_aliases table → auto-clean legal suffix → raw.
    Sponsors not in org_aliases are tracked in _unresolved_sponsors for backfill.
    """
    _load()
    if _load_failed:
        logging.warning("norm: alias table failed to load — using degraded normalization")
    name = (name or '').strip()
    if not name:
        return name
    canonical = _org_aliases.get(name.lower())
    if canonical:
        return canonical
    _unresolved_sponsors.add(name)
    return _auto_clean_org(name)


def get_unresolved_sponsors() -> frozenset[str]:
    """Sponsors seen this session that were NOT found in org_aliases.
    Call after pipeline run to identify candidates for B-02 backfill."""
    return frozenset(_unresolved_sponsors)


def flush_unresolved_to_db(run_date=None) -> int:
    """
    Upsert all in-memory unresolved sponsors into CT.unresolved_sponsors.

    On first encounter: inserts with first_seen_run = last_seen_run = run_date.
    On repeat:          updates last_seen_run + increments occurrence_count.
    Status (PENDING/IGNORED/RESOLVED) is never overwritten by this function —
    only human edits change status.

    Returns number of rows upserted. Clears the in-memory set.
    """
    if not _unresolved_sponsors:
        return 0

    from datetime import date as _date
    import psycopg2.extras as _extras

    run_dt = run_date or _date.today()
    sponsors = list(_unresolved_sponsors)

    try:
        with db._cur() as cur:
            _extras.execute_values(
                cur,
                """
                INSERT INTO "CT".unresolved_sponsors
                    (sponsor, first_seen_run, last_seen_run)
                VALUES %s
                ON CONFLICT (sponsor) DO UPDATE SET
                    last_seen_run    = EXCLUDED.last_seen_run,
                    occurrence_count = unresolved_sponsors.occurrence_count + 1
                """,
                [(s, run_dt, run_dt) for s in sponsors],
            )
        _unresolved_sponsors.clear()
        print(f"  [Norm] {len(sponsors)} unresolved sponsor(s) flushed to CT.unresolved_sponsors")
        return len(sponsors)
    except Exception as exc:
        logging.warning(f"norm.flush_unresolved_to_db failed: {exc}")
        return 0


def normalize_orgs_field(pipe_separated: str) -> str:
    """Normalize and deduplicate a ' | ' separated org field."""
    if not pipe_separated:
        return pipe_separated
    seen:   set[str]  = set()
    result: list[str] = []
    for part in pipe_separated.split(' | '):
        part = part.strip()
        if not part:
            continue
        normed = normalize_org(part)
        if normed and normed.lower() not in seen:
            seen.add(normed.lower())
            result.append(normed)
    return ' | '.join(result)


# ── Condition normalization ────────────────────────────────────────────────────

def normalize_condition(name: str) -> str:
    """
    Return canonical condition name.
    Looks up the exact condition string against dept_indications keywords.
    Falls back to raw name if no match found.
    """
    _load()
    name = (name or '').strip()
    if not name:
        return name
    canonical = _condition_aliases.get(name.lower())
    return canonical if canonical else name


def normalize_conditions_field(comma_separated: str) -> str:
    """Normalize and deduplicate a ', ' separated conditions field."""
    if not comma_separated:
        return comma_separated
    seen:   set[str]  = set()
    result: list[str] = []
    for part in comma_separated.split(', '):
        part = part.strip()
        if not part:
            continue
        normed = normalize_condition(part)
        if normed and normed.lower() not in seen:
            seen.add(normed.lower())
            result.append(normed)
    return ', '.join(result)


# ── Phase normalization ────────────────────────────────────────────────────────

def normalize_phase(phases) -> str:
    """
    Convert CT.gov phases to canonical underscore-joined string.
    Accepts list (from raw API) or existing comma-separated string (re-parse).

    Examples:
        ["PHASE1", "PHASE2"]  → "PHASE1_PHASE2"
        ["PHASE2"]            → "PHASE2"
        "PHASE1, PHASE2"      → "PHASE1_PHASE2"   (re-parse stored value)
        []                    → ""
        ["NA"]                → "NA"
        ["EARLY_PHASE1"]      → "EARLY_PHASE1"
    """
    if isinstance(phases, str):
        parts = [p.strip() for p in phases.split(',') if p.strip()]
    elif isinstance(phases, (list, tuple)):
        parts = [str(p).strip() for p in phases if p and str(p).strip()]
    else:
        parts = []
    # Compact any whitespace within each part ("PHASE 1" → "PHASE1")
    normalized = [re.sub(r'\s+', '', p.upper()) for p in parts]
    return "_".join(normalized)
