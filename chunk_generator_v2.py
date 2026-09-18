"""
chunk_generator_v2.py -- V2 semantic chunking for clinical trial retrieval.

Changes from V1:
  TRIAL_SUMMARY    -- Added: nct_id prefix, Primary Drug, Sponsors
  PROTOCOL         -- Replaced briefSummary with study_type + allocation;
                      now covers full protocol design, not repeated summary text
  PI_INFO          -- Renamed from KOL_INSIGHTS (same content: Overall Officials + Central Contacts)
  KOL_OPINIONS     -- Reserved for future expert opinion data (no documents yet)

Chunk types (10):
  TRIAL_SUMMARY
  DETAILED_DESCRIPTION
  ELIGIBILITY
  INTERVENTION
  PRIMARY_ENDPOINT
  SECONDARY_ENDPOINT
  MESH_TERMS
  PROTOCOL
  PI_INFO
  KOL_OPINIONS     (reserved)
"""
import json
import hashlib

CHUNK_TYPES = [
    "TRIAL_SUMMARY",
    "DETAILED_DESCRIPTION",
    "ELIGIBILITY",
    "INTERVENTION",
    "PRIMARY_ENDPOINT",
    "SECONDARY_ENDPOINT",
    "MESH_TERMS",
    "PROTOCOL",
    "PI_INFO",
    "KOL_OPINIONS",
]

_MIN_TEXT_LEN = 20
_EMPTY_VALUES = {"", "none", "n/a", "null", "null.", "unknown", "not provided", "-", "[]", "{}"}


def _g(row: dict, *keys: str) -> str:
    for key in keys:
        val = row.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s and s.lower() not in _EMPTY_VALUES:
            return s
    return ""


def _parse_outcomes(text: str) -> list[dict]:
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _outcomes_numbered(outcomes: list[dict]) -> str:
    parts = []
    for i, o in enumerate(outcomes, 1):
        measure = (o.get("measure") or "").strip()
        tf      = (o.get("timeFrame") or "").strip()
        desc    = (o.get("description") or "").strip()
        if not measure:
            continue
        line = f"{i}. {measure}"
        if tf:
            line += f" (time frame: {tf})"
        if desc:
            line += f" -- {desc}"
        parts.append(line)
    return "\n".join(parts)


def _is_meaningful(text: str) -> bool:
    s = text.strip()
    return len(s) >= _MIN_TEXT_LEN and s.lower() not in _EMPTY_VALUES


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_chunks(row: dict) -> list[dict[str, str]]:
    """
    V2 labeled chunk format for BGE-M3 / Elasticsearch architecture.
    Returns list of {chunk_type, content_text} for non-empty chunks only.
    """
    nct_id         = row.get("nct_id", "")
    brief_title    = _g(row, "Brief Title")
    official_title = _g(row, "Official Title")
    brief_summary  = _g(row, "briefSummary")
    detailed_desc  = _g(row, "detailedDescription")
    conditions     = _g(row, "conditions", "Conditions")
    prim_purpose   = _g(row, "Primary Purpose")
    eligibility    = _g(row, "eligibilityCriteria")
    interventions  = _g(row, "Interventions")
    collaborators  = _g(row, "Collaborators")
    primary_drug   = _g(row, "Primary Drug")
    sponsors       = _g(row, "Sponsors")
    prim_raw       = _g(row, "Primary Outcomes")
    sec_raw        = _g(row, "Secondary Outcomes")
    mesh_cond      = _g(row, "MeSH Conditions")
    mesh_interv    = _g(row, "MeSH Interventions")
    interv_model   = _g(row, "Intervention Model")
    study_type     = _g(row, "studyType")
    allocation     = _g(row, "Allocation")

    prim_outcomes = _parse_outcomes(prim_raw)
    sec_outcomes  = _parse_outcomes(sec_raw)

    chunks: list[dict[str, str]] = []

    def _add(chunk_type: str, text: str) -> None:
        text = text.strip()
        if _is_meaningful(text):
            chunks.append({"chunk_type": chunk_type, "content_text": text})

    # ── TRIAL_SUMMARY ──────────────────────────────────────────────────────────
    # V2: added nct_id, Primary Drug, Sponsors
    ts_parts = ["[TRIAL_SUMMARY]"]
    if nct_id:
        ts_parts += ["", f"NCT ID: {nct_id}"]
    if brief_title:
        ts_parts += ["", "Brief Title:", brief_title]
    if primary_drug:
        ts_parts += ["", f"Primary Drug: {primary_drug}"]
    if sponsors:
        ts_parts += ["", f"Sponsor: {sponsors}"]
    if brief_summary:
        ts_parts += ["", "Brief Summary:", brief_summary]
    if conditions:
        ts_parts += ["", "Conditions:", conditions]
    if prim_purpose:
        ts_parts += ["", "Primary Purpose:", prim_purpose]
    if len(ts_parts) > 1:
        _add("TRIAL_SUMMARY", "\n".join(ts_parts))

    # ── DETAILED_DESCRIPTION ───────────────────────────────────────────────────
    if detailed_desc:
        _add("DETAILED_DESCRIPTION", "\n".join([
            "[DETAILED_DESCRIPTION]", "", "Detailed Description:", detailed_desc,
        ]))

    # ── ELIGIBILITY ────────────────────────────────────────────────────────────
    if eligibility:
        _add("ELIGIBILITY", "\n".join([
            "[ELIGIBILITY]", "", "Eligibility Criteria:", eligibility,
        ]))

    # ── INTERVENTION ───────────────────────────────────────────────────────────
    iv_parts = ["[INTERVENTION]"]
    if primary_drug:
        iv_parts += ["", f"Primary Drug: {primary_drug}"]
    if interventions:
        iv_parts += ["", "Interventions:", interventions]
    if collaborators:
        iv_parts += ["", "Collaborators:", collaborators]
    if len(iv_parts) > 1:
        _add("INTERVENTION", "\n".join(iv_parts))

    # ── PRIMARY_ENDPOINT ───────────────────────────────────────────────────────
    if prim_outcomes:
        prim_text = _outcomes_numbered(prim_outcomes)
        if prim_text:
            _add("PRIMARY_ENDPOINT", "\n".join([
                "[PRIMARY_ENDPOINT]", "", "Primary Outcome Measures:", prim_text,
            ]))
    elif prim_raw and _is_meaningful(prim_raw):
        _add("PRIMARY_ENDPOINT", "\n".join([
            "[PRIMARY_ENDPOINT]", "", "Primary Outcome Measures:", prim_raw,
        ]))

    # ── SECONDARY_ENDPOINT ─────────────────────────────────────────────────────
    if sec_outcomes:
        sec_text = _outcomes_numbered(sec_outcomes)
        if sec_text:
            _add("SECONDARY_ENDPOINT", "\n".join([
                "[SECONDARY_ENDPOINT]", "", "Secondary Outcome Measures:", sec_text,
            ]))
    elif sec_raw and _is_meaningful(sec_raw):
        _add("SECONDARY_ENDPOINT", "\n".join([
            "[SECONDARY_ENDPOINT]", "", "Secondary Outcome Measures:", sec_raw,
        ]))

    # ── MESH_TERMS ─────────────────────────────────────────────────────────────
    mt_parts = ["[MESH_TERMS]"]
    if official_title:
        mt_parts += ["", "Official Title:", official_title]
    if mesh_cond:
        mt_parts += ["", "MeSH Conditions:", mesh_cond]
    if mesh_interv:
        mt_parts += ["", "MeSH Interventions:", mesh_interv]
    if len(mt_parts) > 1:
        _add("MESH_TERMS", "\n".join(mt_parts))

    # ── PROTOCOL ───────────────────────────────────────────────────────────────
    # V2: removed briefSummary (already in TRIAL_SUMMARY)
    #     now covers full study design: type, model, allocation, purpose, eligibility excerpt
    pr_parts = ["[PROTOCOL]"]
    if study_type:
        pr_parts += ["", f"Study Type: {study_type}"]
    if interv_model:
        pr_parts += ["", f"Intervention Model: {interv_model}"]
    if allocation:
        pr_parts += ["", f"Allocation: {allocation}"]
    if prim_purpose:
        pr_parts += ["", f"Primary Purpose: {prim_purpose}"]
    if eligibility:
        elig_excerpt = eligibility if len(eligibility) <= 600 else eligibility[:600] + "..."
        pr_parts += ["", "Eligibility (excerpt):", elig_excerpt]
    if len(pr_parts) > 1:
        _add("PROTOCOL", "\n".join(pr_parts))

    # ── PI_INFO (renamed from KOL_INSIGHTS) ────────────────────────────────────
    officials_raw = _g(row, "Overall Officials")
    contacts_raw  = _g(row, "Central Contacts")
    pi_parts = ["[PI_INFO]"]
    if brief_title:
        pi_parts += ["", f"Trial: {brief_title}"]
    if officials_raw:
        pi_parts += ["", "Key Opinion Leaders / Overall Officials:"]
        pi_parts += [f"  - {e.strip()}" for e in officials_raw.split(" | ") if e.strip()]
    if contacts_raw:
        pi_parts += ["", "Trial Contacts:"]
        pi_parts += [f"  - {e.strip()}" for e in contacts_raw.split(" | ") if e.strip()]
    if officials_raw or contacts_raw:
        _add("PI_INFO", "\n".join(pi_parts))

    # KOL_OPINIONS -- reserved for future expert opinion data, no documents yet

    return chunks
