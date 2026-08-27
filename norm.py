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

To force reload after updating alias tables:
    norm.reload()
"""

import re
import db

# ── Internal state ─────────────────────────────────────────────────
_org_aliases:       dict[str, str] = {}
_condition_aliases: dict[str, str] = {}   # keyword.lower() → canonical indication
_loaded = False

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
    global _org_aliases, _condition_aliases, _loaded
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
        _loaded = True  # prevent repeated failures


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
    """
    _load()
    name = (name or '').strip()
    if not name:
        return name
    canonical = _org_aliases.get(name.lower())
    if canonical:
        return canonical
    return _auto_clean_org(name)


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
