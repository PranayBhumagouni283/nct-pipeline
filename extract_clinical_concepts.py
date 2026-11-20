"""
__extract_clinical_concepts.py â€” Phase 4: Trial-Level Clinical Concept Extraction

Hybrid approach per trial:
  Pass 1 â€” Regex/dictionary : scan source text using clinical_query_dictionary variants
  Pass 2 â€” LLM (GPT-4o-mini): extract remaining concepts not resolved by regex
  Pass 3 â€” Merge            : regex result wins; LLM fills gaps

Manages 21 Clinical Intelligence columns on CT.organized_trials (pipe-separated canonical values).

Concept Dictionary Migration (26 â†’ 21):
  Removed (7): therapy_modality, drug_class, mechanism_of_action, targets, protein,
               expression_status, ecog_status
  Merged  (1): gene_mutation â†’ genomic_alteration (gene_mutation dict entries still
               searched under genomic_alteration)
  Added   (3): biomarker_measurement, biomarker_value_or_cutoff, biomarker_test_method

Usage:
    python __extract_clinical_concepts.py --dry-run              # show samples, no write
    python __extract_clinical_concepts.py --apply                # write to PROD only
    python __extract_clinical_concepts.py --apply --both         # write to PROD + AWS RDS
    python __extract_clinical_concepts.py --apply --limit 50     # first 50 trials
    python __extract_clinical_concepts.py --apply --dept ADC     # one dept only
    python __extract_clinical_concepts.py --apply --no-llm       # regex only (no OpenAI)
    python __extract_clinical_concepts.py --apply --force        # re-extract already-done rows
"""
import os, sys, re, json, argparse, textwrap
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2, psycopg2.extras

# â”€â”€ Configuration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DATABASES = {
    "PROD":    os.getenv("DATABASE_URL"),
    "AWS_RDS": os.getenv("DATABASE_URL_AWS"),
}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LLM_MODEL      = "gpt-4o-mini"
BATCH_SIZE     = 20     # trials per DB write batch
LLM_MAX_CHARS  = 8000   # max combined source text sent to LLM per trial

# â”€â”€ 21 active concept columns â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Final system: 26 original - 7 removed - 1 merged + 3 added = 21

CONCEPTS_21 = [
    "line_of_therapy", "biomarkers", "biomarker_status", "prior_therapies",
    "treatment_status", "treatment_setting", "disease_state",
    "endpoints_normalized", "gene", "genomic_alteration",
    "age_range_normalized", "cns_status", "metastatic_site",
    "organ_function_requirement", "performance_status", "combination_therapy",
    "comparator", "regimen",
    "biomarker_measurement", "biomarker_value_or_cutoff", "biomarker_test_method",
]

# 3 new columns that do NOT yet exist in the DB â€” must be added via DDL
NEW_COLUMNS = ["biomarker_measurement", "biomarker_value_or_cutoff", "biomarker_test_method"]

# Surviving original concepts (used as "already extracted?" proxy â€” excludes new cols)
EXISTING_CONCEPTS = [c for c in CONCEPTS_21 if c not in NEW_COLUMNS]

# Concepts primarily handled by regex/dictionary (LLM fills gaps on anything missing)
REGEX_PRIMARY = {
    "line_of_therapy", "biomarkers", "biomarker_status", "prior_therapies",
    "treatment_status", "treatment_setting", "disease_state",
    "organ_function_requirement", "performance_status", "cns_status",
    "genomic_alteration", "endpoints_normalized",
}

# Concept â†’ concept dict key(s) in clinical_query_dictionary.concept column.
# genomic_alteration searches both GENOMIC_ALTERATION and the now-merged GENE_MUTATION entries.
CONCEPT_TO_DICT_KEYS: dict[str, list[str]] = {
    "line_of_therapy":            ["LINE_OF_THERAPY"],
    "biomarkers":                 ["BIOMARKER", "BIOMARKERS"],
    "biomarker_status":           ["BIOMARKER_STATUS"],
    "prior_therapies":            ["PRIOR_THERAPY"],
    "treatment_status":           ["TREATMENT_STATUS"],
    "treatment_setting":          ["TREATMENT_SETTING"],
    "disease_state":              ["DISEASE_STATE"],
    "organ_function_requirement": ["ORGAN_FUNCTION_REQUIREMENT"],
    "performance_status":         ["PERFORMANCE_STATUS"],
    "cns_status":                 ["CNS_STATUS"],
    "genomic_alteration":         ["GENOMIC_ALTERATION", "GENE_MUTATION"],  # merged
    "endpoints_normalized":       ["ENDPOINT", "ENDPOINTS_NORMALIZED"],
}

# Source columns per concept â€” concept-specific field routing (not global search)
CONCEPT_SOURCES: dict[str, list[str]] = {
    "line_of_therapy":            ["eligibilityCriteria", "briefSummary", "detailedDescription", "Interventions"],
    "biomarkers":                 ["eligibilityCriteria", "briefSummary", "detailedDescription"],
    "biomarker_status":           ["eligibilityCriteria", "briefSummary", "detailedDescription"],
    "biomarker_measurement":      ["eligibilityCriteria", "detailedDescription", "briefSummary"],
    "biomarker_value_or_cutoff":  ["eligibilityCriteria", "detailedDescription", "briefSummary"],
    "biomarker_test_method":      ["eligibilityCriteria", "detailedDescription", "briefSummary"],
    "prior_therapies":            ["eligibilityCriteria", "briefSummary", "detailedDescription"],
    "treatment_status":           ["eligibilityCriteria", "briefSummary", "detailedDescription"],
    "treatment_setting":          ["eligibilityCriteria", "briefSummary", "detailedDescription"],
    "disease_state":              ["conditions", "eligibilityCriteria", "briefSummary", "detailedDescription"],
    "endpoints_normalized":       ["Primary Outcomes", "Secondary Outcomes"],
    "gene":                       ["eligibilityCriteria", "briefSummary", "detailedDescription"],
    "genomic_alteration":         ["eligibilityCriteria", "detailedDescription", "briefSummary"],
    "age_range_normalized":       [],   # handled from structured columns
    "cns_status":                 ["eligibilityCriteria", "detailedDescription", "briefSummary"],
    "metastatic_site":            ["eligibilityCriteria", "conditions", "briefSummary", "detailedDescription"],
    "organ_function_requirement": ["eligibilityCriteria", "detailedDescription"],
    "performance_status":         ["eligibilityCriteria", "detailedDescription", "briefSummary"],
    "combination_therapy":        ["Interventions", "briefSummary", "detailedDescription"],
    "comparator":                 ["Interventions", "briefSummary", "detailedDescription"],
    "regimen":                    ["Interventions", "briefSummary", "detailedDescription", "eligibilityCriteria"],
}

# â”€â”€ Column DDL â€” only add the 3 new columns â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

ADD_COLUMNS_SQL = [
    f'ALTER TABLE "CT".organized_trials ADD COLUMN IF NOT EXISTS {col} TEXT'
    for col in NEW_COLUMNS
]

# â”€â”€ DB helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def connect(url: str):
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute('SET search_path TO "CT"')
    return conn


def load_dict_rows(conn) -> dict[str, list[dict]]:
    """Load clinical_query_dictionary grouped by concept key."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM clinical_query_dictionary ORDER BY concept, confidence DESC, id")
        rows = [dict(r) for r in cur.fetchall()]
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["concept"], []).append(r)
    return grouped


def fetch_trials(conn, dept: str | None, limit: int | None, force: bool,
                 dry_run: bool = False) -> list[dict]:
    """Fetch trials needing extraction."""
    where_parts = []
    params: list[Any] = []

    if dept:
        where_parts.append("dept = %s")
        params.append(dept)

    if not force and not dry_run:
        # Skip rows where any surviving concept column is already populated.
        # Uses EXISTING_CONCEPTS (not new cols) so trials extracted with old system are skipped.
        where_parts.append(
            "(" + " AND ".join(f"{c} IS NULL" for c in EXISTING_CONCEPTS) + ")"
        )

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    order_sql  = "ORDER BY _parsed_date DESC NULLS LAST"
    limit_sql  = f"LIMIT {limit}" if limit else ""

    cols = [
        "nct_id", "dept",
        '"eligibilityCriteria"', '"briefSummary"', '"detailedDescription"',
        '"Interventions"', '"conditions"',
        '"Primary Outcomes"', '"Secondary Outcomes"',
        '"MeSH Conditions"', '"MeSH Interventions"',
        '"Minimum Age"', '"Standard Ages"',
        "min_age_value", "min_age_unit",
    ]
    sql = f'SELECT {", ".join(cols)} FROM organized_trials {where_sql} {order_sql} {limit_sql}'
    with conn.cursor() as cur:
        cur.execute(sql, params or None)
        return [dict(r) for r in cur.fetchall()]


# â”€â”€ Pass 1: Regex / dictionary extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# Negation patterns â€” checked in window BEFORE the match.
# "naive/naÃ¯ve" intentionally excluded: "treatment-naive patients with BRCA mutation"
# would falsely negate BRCA. Treatment-naive status is captured by LINE_OF_THERAPY dict.
_NEG_BEFORE = re.compile(
    r"\b(no|not|without|non[-\s]|never|absence\s+of|lack\s+of|excluded?|must\s+not|"
    r"ineligible|negative|rule(?:d)?\s+out|free\s+of)\b",
    re.IGNORECASE,
)

# Biomarker/gene concepts use strict negation WITHOUT "negative":
# "KRAS negative patients with EGFR mutation" â€” "negative" is a biomarker STATUS for KRAS,
# not exclusion of EGFR.  "HER2-negative breast cancer" still selects on HER2 status.
# For these concepts, only hard exclusion words (no/not/without/etc.) are real negations.
_NEG_BEFORE_BIOMARKER = re.compile(
    r"\b(no|not|without|non[-\s]|never|absence\s+of|lack\s+of|excluded?|must\s+not|"
    r"ineligible|rule(?:d)?\s+out|free\s+of)\b",
    re.IGNORECASE,
)

# Negation patterns â€” checked in window AFTER the match (for "X excluded", "X not required")
_NEG_AFTER = re.compile(
    r"\b(excluded?|not\s+required|not\s+allowed|not\s+permitted|free)\b",
    re.IGNORECASE,
)

# Short uppercase abbreviations in biomarker/gene context require case-sensitive matching
# to prevent "met" (verb) matching "MET" (gene), "ALK" vs "alkaline", etc.
_UPPERCASE_ABBREV_RE = re.compile(r'^[A-Z][A-Z0-9\-]{1,7}$')
_CASE_SENSITIVE_CONCEPTS = {"BIOMARKER", "GENE", "GENOMIC_ALTERATION", "GENE_MUTATION"}

# Clinical context words that must appear near a biomarker/gene match
# to confirm clinical relevance (reduces false positives from background text)
_BIOMARKER_CONTEXT_RE = re.compile(
    r"\b(positive|negative|overexpression|overexpressed|amplification|amplified|"
    r"mutation|mutant|mutated|fusion|fused|rearrangement|rearranged|deletion|"
    r"exon|status|inhibitor|targeting|directed|expression|expressed|test|assay|"
    r"score|selected|enriched|required|eligible|marker|target|driven|dependent|"
    r"pathway|signaling|altered|alteration|high|low|elevated|CPS|TPS|IHC|ISH|"
    r"FISH|NGS|PCR|H-score|skipping|amplify|copy\s+number|translocation|"
    r"documented|testing|checkpoint|immune|therapy|treatment|biomarker|profiling|"
    r"evaluated|confirmed|detected|measured|expressing|sensitizing|activating|"
    r"wild.?type|resistance|resistant|sensitive|sensitivity|testing|identified|"
    r"harboring|harbor|carrying|carry|known|prior|previous|current|eligible)\b",
    re.IGNORECASE,
)


# LINE_OF_THERAPY prior-treatment context â€” when these words precede a LoT term,
# the term describes a PAST treatment the patient received (eligibility criterion),
# not the trial's own line-of-therapy setting.  Prevents "prior adjuvant therapy",
# "failed first-line treatment", "refractory to first-line" from tagging the trial.
_LOT_PRIOR_CONTEXT_RE = re.compile(
    r"\b(prior|previous|previously|history\s+of|failed|failure|refractory)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _needs_case_sensitive(variant_original: str, concept_key: str) -> bool:
    """True for single-token uppercase abbreviations in biomarker/gene concepts."""
    if concept_key not in _CASE_SENSITIVE_CONCEPTS:
        return False
    return bool(_UPPERCASE_ABBREV_RE.match(variant_original.strip()))


def _match_variants(
    text_norm: str,
    original_text: str,
    dict_rows: list[dict],
    concept_key: str,
    require_context: bool = False,
) -> list[str]:
    """
    Return canonical values whose variants are found in the text.

    For biomarker/gene concepts with short uppercase abbreviation variants (MET, ALK, HER2),
    uses case-sensitive matching against the original (non-normalized) text to prevent
    false positives from common words (metastasis, method, etc.).

    Negation check: scans 100-char window before AND 60-char window after the match.
    Context check (optional): ensures at least one clinical keyword is nearby.
    """
    found: list[str] = []

    # Biomarker/gene concepts use strict negation (no "negative" word) because
    # "negative" there is a biomarker status, not an exclusion marker.
    neg_before_pat = (
        _NEG_BEFORE_BIOMARKER
        if concept_key in _CASE_SENSITIVE_CONCEPTS
        else _NEG_BEFORE
    )

    for row in dict_rows:
        raw = row.get("variants") or ""
        canonical = row["canonical_value"]
        neg_support = row.get("negation_support", False)

        for variant_raw in raw.split(";"):
            variant_original = variant_raw.strip()
            if not variant_original:
                continue

            use_case_sensitive = _needs_case_sensitive(variant_original, concept_key)

            use_anti_guard = (
                concept_key in _CASE_SENSITIVE_CONCEPTS
                and re.match(r'^[A-Z0-9]', variant_original.strip())
            )

            if use_case_sensitive:
                # Negative lookbehind: "anti-X" is a drug, not a biomarker status.
                prefix = r'(?<!anti-)(?<!\w)' if use_anti_guard else r'(?<!\w)'
                # Negative lookahead: "X inhibitor/antibody/blocker" is drug-context
                # (e.g. "PD-L1 inhibitor" describes the drug, not patient selection).
                drug_suffix = r'(?![\s\-]{0,2}(?:inhibitor|antibody|blocker|antagonist)\b)'
                suffix = (drug_suffix + r'(?!\w)') if use_anti_guard else r'(?!\w)'
                pat = re.compile(prefix + re.escape(variant_original) + suffix)
                search_text = original_text
            else:
                v_norm = _normalize(variant_original)
                pat = re.compile(
                    r"(?<![a-z0-9])" + re.escape(v_norm) + r"(?![a-z0-9])",
                    re.IGNORECASE,
                )
                search_text = text_norm

            # Iterate ALL occurrences â€” accept canonical if ANY is not negated/filtered.
            # Using search() (first match only) was causing false negatives when
            # the first occurrence was negated but a later one was valid
            # (e.g., "absence of EGFR mutations ... EGFR exon 20 insertion").
            accepted = False
            for m in pat.finditer(search_text):
                match_start = m.start()
                match_end   = m.end()

                # Negation check â€” before the match (sentence-boundary + comma-aware)
                if neg_support:
                    before_window = search_text[max(0, match_start - 100):match_start]
                    # Take only the last sentence fragment (don't cross sentence boundaries).
                    # Semicolons are also clause separators in eligibility criteria lists.
                    before_sentence = re.split(r'[.!?;\n]', before_window)[-1]
                    neg_m = neg_before_pat.search(before_sentence)
                    if neg_m:
                        # Comma guard: only bypass negation for attributive "negative"
                        # (e.g. "HR negative, HER2 positive" â€” comma separates clauses).
                        # Predicative negators (no/not/without/never/etc.) maintain scope
                        # across commas because they negate entire lists:
                        # "No prior EGFR, platinum, or immunotherapy" â€” all items negated.
                        is_attributive = bool(re.match(r'negative$', neg_m.group(0), re.I))
                        between = before_sentence[neg_m.end():]
                        if not (is_attributive and ',' in between):
                            continue  # negated â€” skip this occurrence

                    # Negation check â€” after the match
                    after_window = search_text[match_end:match_end + 60]
                    if _NEG_AFTER.search(after_window):
                        continue

                # Context check for sensitive abbreviations
                if require_context and use_case_sensitive:
                    ctx_window = original_text[max(0, match_start - 200):match_end + 200]
                    if not _BIOMARKER_CONTEXT_RE.search(ctx_window):
                        continue  # no clinical context â€” skip this occurrence

                # LINE_OF_THERAPY: skip matches that appear in prior-treatment context.
                # Eligibility criteria often say "patients who received prior adjuvant therapy"
                # or "failed first-line treatment" â€” these describe REQUIRED past treatments,
                # not the trial's own LoT setting.
                if concept_key == "LINE_OF_THERAPY":
                    before_window = search_text[max(0, match_start - 100):match_start]
                    before_clause = re.split(r'[.!?;\n]', before_window)[-1]
                    if _LOT_PRIOR_CONTEXT_RE.search(before_clause):
                        continue  # prior-treatment context â€” skip this occurrence

                accepted = True
                break  # one valid occurrence is enough

            if accepted and canonical not in found:
                found.append(canonical)

    return found


def extract_age_range(row: dict) -> str | None:
    """Build age_range_normalized from structured min_age + Standard Ages columns."""
    val  = row.get("min_age_value")
    unit = row.get("min_age_unit")
    std  = row.get("Standard Ages") or ""
    parts = []
    if val is not None and unit:
        parts.append(f">={val} {unit.capitalize()}")
    if std:
        for s in std.split("|"):
            s = s.strip()
            if s:
                parts.append(s)
    return " | ".join(dict.fromkeys(parts)) or None


def _gather_source_text(row: dict, src_cols: list[str]) -> tuple[str, str]:
    """Return (original_text, normalized_text) for the given source columns."""
    parts = []
    for c in src_cols:
        val = row.get(c) or row.get(f'"{c}"') or ""
        if val:
            parts.append(str(val))
    original = " ".join(parts)
    return original, _normalize(original)


def regex_pass(row: dict, dict_index: dict[str, list[dict]]) -> dict[str, str | None]:
    results: dict[str, str | None] = {c: None for c in CONCEPTS_21}

    # Age range â€” structured columns only
    results["age_range_normalized"] = extract_age_range(row)

    for concept in REGEX_PRIMARY:
        dict_keys = CONCEPT_TO_DICT_KEYS.get(concept, [])
        if not dict_keys:
            continue

        src_cols = CONCEPT_SOURCES.get(concept, [])
        original_text, text_norm = _gather_source_text(row, src_cols)
        if not text_norm:
            continue

        # Biomarker/gene concepts use context validation to prevent background-text FPs
        require_ctx = concept in ("biomarkers", "gene", "genomic_alteration")

        all_matches: list[str] = []
        for dk in dict_keys:
            if dk not in dict_index:
                continue
            matches = _match_variants(
                text_norm, original_text, dict_index[dk], dk,
                require_context=require_ctx,
            )
            for m in matches:
                if m not in all_matches:
                    all_matches.append(m)

        if all_matches:
            results[concept] = " | ".join(all_matches)

    # â”€â”€ Performance status deduplication â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # "ECOG 0" variant matches inside "ECOG 0 or 1" text, producing both
    # ECOG_0 and ECOG_0_1.  When a composite range is present, drop the
    # individual components it subsumes.
    ps = results.get("performance_status")
    if ps:
        ps_vals = [v.strip() for v in ps.split("|")]
        if "ECOG_0_2" in ps_vals:
            ps_vals = [v for v in ps_vals if v not in ("ECOG_0", "ECOG_0_1", "ECOG_1", "ECOG_2")]
        elif "ECOG_0_1" in ps_vals:
            ps_vals = [v for v in ps_vals if v not in ("ECOG_0", "ECOG_1")]
        if "ECOG_1_2" in ps_vals:
            ps_vals = [v for v in ps_vals if v not in ("ECOG_1", "ECOG_2")]
        results["performance_status"] = " | ".join(ps_vals) if ps_vals else None

    return results


# â”€â”€ Pass 2: LLM extraction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

LLM_CONCEPTS = [
    c for c in CONCEPTS_21
    if c not in REGEX_PRIMARY and c != "age_range_normalized"
]

_LLM_SYSTEM = textwrap.dedent("""
    You are a clinical trial data extraction specialist.

    Extract structured clinical concepts from the provided trial text.

    CRITICAL RULES:
    - Only extract concepts clearly supported by the trial text.
    - Do NOT infer biomarkers from intervention names alone (e.g., "study administers
      MET inhibitor" does not automatically mean BIOMARKER = MET unless the trial
      explicitly selects patients based on MET status).
    - Do NOT confuse gene names (EGFR) with genomic alterations (EGFR exon 19 deletion).
    - Do NOT extract prior_therapies from current interventions.
    - Do NOT infer combination_therapy just because multiple drugs appear in the trial.
    - Preserve negated context â€” "without brain metastases" is not a CNS finding.
    - Return ONLY a JSON object â€” no explanation, no markdown, no extra keys.
    - Use null for any concept not clearly present in the text.
    - Use " | " (space-pipe-space) to separate multiple values within a field.
    - Use canonical UPPERCASE labels where specified.
    - EXCLUSION CRITERIA RULE: Do NOT assign any concept value based on text found in
      exclusion criteria sections. If a condition is mentioned as a reason to EXCLUDE
      patients (e.g. "patients with hepatitis B excluded", "no prior systemic therapy"),
      do NOT capture it as a positive clinical characteristic of the enrolled population.
      Exclusion criteria negate a condition â€” they describe who is NOT enrolled.
    - LINE OF THERAPY: Do NOT assign a line of therapy if the eligibility text describes
      previously untreated, treatment-naive, first-line, neoadjuvant, or adjuvant patients
      in the INCLUSION criteria. Only assign if inclusion criteria explicitly require prior
      treatment history.
""").strip()

_LLM_SCHEMA: dict[str, str] = {
    "gene":                      (
        "Gene names explicitly mentioned in the trial context (eligibility, disease, interventions). "
        "Examples: ERBB2, EGFR, KRAS, BRAF, PIK3CA, ALK, ROS1, BRCA1, BRCA2, MET. "
        "ONLY if the gene is clearly referenced (not just mentioned in background text)."
    ),
    "metastatic_site":           (
        "Specific metastatic sites. Examples: LIVER, LUNG, BONE, BRAIN, LYMPH_NODE, PERITONEUM. "
        "Only when explicit â€” 'without brain metastases' means CNS is EXCLUDED, not present."
    ),
    "organ_function_requirement": (
        "Organ-function eligibility requirements. "
        "Examples: ADEQUATE_RENAL, ADEQUATE_HEPATIC, ADEQUATE_CARDIAC, ADEQUATE_HEMATOLOGIC, "
        "ADEQUATE_BONE_MARROW, TOTAL_BILIRUBIN_THRESHOLD, CREATININE_CLEARANCE, PROTHROMBIN_TIME_INR."
    ),
    "combination_therapy":       (
        "Drug combinations used together IN THE SAME ARM of this trial. "
        "Accept when 2+ drugs are administered together: look for 'and', 'plus', 'with', '+', "
        "'combination', 'co-administered', 'together', 'along with', 'in addition to', "
        "or a regimen listing multiple drugs for the same treatment arm. "
        "Do NOT infer from separate arms each receiving a different single drug. "
        "Format: DRUG_A + DRUG_B. Examples: T-DXd + PEMBROLIZUMAB, CARBOPLATIN + PACLITAXEL."
    ),
    "comparator":                (
        "Control/comparator arm explicitly described. "
        "Examples: PLACEBO, STANDARD_OF_CARE, PHYSICIAN_CHOICE, ACTIVE_COMPARATOR, USUAL_CARE."
    ),
    "regimen":                   (
        "Dosing regimen details where explicitly stated. "
        "Examples: 5.4 mg/kg IV Q3W, 200 mg Q3W, WEEKLY IV, 21-day cycle."
    ),
    "biomarker_measurement":     (
        "Measurement method or assay score used to quantify a biomarker. "
        "Examples: IHC 3+, CPS, TPS, H-SCORE, FISH, COPY_NUMBER, ALLELE_FREQUENCY. "
        "Only if explicitly stated alongside a biomarker."
    ),
    "biomarker_value_or_cutoff": (
        "Numeric threshold or value defining biomarker eligibility. "
        "Examples: >=1, >=10, 3+, >=50%, <1%. "
        "Only if an explicit cutoff is stated."
    ),
    "biomarker_test_method":     (
        "Laboratory method used to test the biomarker. "
        "Examples: IHC, FISH, ISH, NGS, PCR, ELISA, FLOW_CYTOMETRY, IMMUNOHISTOCHEMISTRY. "
        "Only if explicitly mentioned."
    ),
}


def _build_source_text(row: dict) -> str:
    """Assemble compact source text for LLM (capped at LLM_MAX_CHARS)."""
    sections = {
        "Eligibility": row.get("eligibilityCriteria") or "",
        "Brief Summary": row.get("briefSummary") or "",
        "Detailed Description": row.get("detailedDescription") or "",
        "Interventions": row.get("Interventions") or "",
        "Conditions": row.get("conditions") or "",
        "Primary Outcomes": row.get("Primary Outcomes") or "",
        "Secondary Outcomes": row.get("Secondary Outcomes") or "",
    }
    parts = []
    total = 0
    for label, text in sections.items():
        if not text.strip():
            continue
        snippet   = text[:2000]
        remaining = LLM_MAX_CHARS - total
        if remaining <= 0:
            break
        snippet = snippet[:remaining]
        parts.append(f"[{label}]\n{snippet}")
        total += len(snippet)
    return "\n\n".join(parts)


def _build_llm_prompt(source_text: str, concepts_needed: list[str]) -> str:
    schema_lines = "\n".join(
        f'  "{c}": <{_LLM_SCHEMA[c]}>'
        for c in concepts_needed
        if c in _LLM_SCHEMA
    )
    return (
        f"Extract the following clinical concepts from this clinical trial text.\n"
        f"Return ONLY valid JSON with exactly these keys (null if not present):\n"
        f"{{\n{schema_lines}\n}}\n\n"
        f"TRIAL TEXT:\n{source_text}"
    )


def llm_pass(row: dict, missing_concepts: list[str], client) -> dict[str, str | None]:
    """Call OpenAI to extract concepts not resolved by regex."""
    results: dict[str, str | None] = {}
    if not missing_concepts or client is None:
        return results

    needed = [c for c in missing_concepts if c in _LLM_SCHEMA]
    if not needed:
        return results

    source_text = _build_source_text(row)
    if not source_text.strip():
        return results

    prompt = _build_llm_prompt(source_text, needed)

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            temperature=0,
            max_tokens=512,
        )
        raw    = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        for c in needed:
            val = parsed.get(c)
            if val and str(val).strip().lower() not in ("null", "none", "n/a", ""):
                results[c] = str(val).strip()
    except Exception as e:
        print(f"    [LLM] {row.get('nct_id')} error: {e}")

    return results


# â”€â”€ Pass 3: Merge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def merge_results(regex_result: dict, llm_result: dict) -> dict[str, str | None]:
    """Regex wins; LLM fills None slots."""
    merged = dict(regex_result)
    for concept, llm_val in llm_result.items():
        if merged.get(concept) is None and llm_val:
            merged[concept] = llm_val
    return merged


# â”€â”€ Main extraction loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def process_db(db_name: str, url: str, args) -> None:
    print(f"\n{'='*60}")
    print(f"  {db_name}  ({'DRY RUN' if args.dry_run else 'APPLY'})")
    print('='*60)

    conn = connect(url)

    # Step 1: Add the 3 new columns (existing 18 already in DB â€” IF NOT EXISTS is safe)
    with conn.cursor() as cur:
        for sql in ADD_COLUMNS_SQL:
            cur.execute(sql)
    if not args.dry_run:
        conn.commit()
        print(f"  [OK] {len(NEW_COLUMNS)} new columns added (biomarker_measurement, biomarker_value_or_cutoff, biomarker_test_method)")
    else:
        conn.rollback()
        conn = connect(url)
        print(f"  [DRY RUN] Would add {len(NEW_COLUMNS)} new columns")

    # Step 2: Load dictionary
    try:
        dict_index = load_dict_rows(conn)
        print(f"  [OK] Loaded {sum(len(v) for v in dict_index.values())} concept dictionary rows "
              f"({len(dict_index)} concept keys)")
    except Exception as e:
        print(f"  [ERR] clinical_query_dictionary not found: {e}")
        print("    Run __migrate_clinical_query_dict.py --apply first.")
        conn.close()
        return

    # Step 3: Load trials
    trials = fetch_trials(conn, args.dept or None, args.limit or None, args.force,
                          dry_run=args.dry_run)
    print(f"  [OK] {len(trials)} trials to process")
    if not trials:
        conn.close()
        return

    # Step 4: OpenAI client
    llm_client = None
    if not args.no_llm:
        if not OPENAI_API_KEY:
            print("  [WARN] OPENAI_API_KEY not set â€” running regex-only mode")
        else:
            try:
                import openai
                llm_client = openai.OpenAI(api_key=OPENAI_API_KEY)
                print(f"  [OK] OpenAI client ready ({LLM_MODEL})")
            except ImportError:
                print("  [WARN] openai package not installed â€” running regex-only mode")

    # Step 5: Extract + batch update
    updates: list[dict] = []
    llm_calls  = 0
    regex_hits = 0

    for i, row in enumerate(trials, 1):
        nct_id = row["nct_id"]

        # Pass 1 â€” regex/dictionary
        regex_result = regex_pass(row, dict_index)
        regex_found  = [c for c, v in regex_result.items() if v is not None]
        regex_hits  += len(regex_found)

        # Pass 2 â€” LLM for concepts still missing
        missing = [c for c in CONCEPTS_21 if regex_result.get(c) is None]
        llm_result: dict = {}
        if llm_client and missing:
            llm_result = llm_pass(row, missing, llm_client)
            if llm_result:
                llm_calls += 1

        # Pass 3 â€” merge (regex wins)
        final = merge_results(regex_result, llm_result)
        updates.append({"nct_id": nct_id, "dept": row["dept"], **final})

        if i % 50 == 0 or i == len(trials):
            print(f"  [{i}/{len(trials)}] regex_hits={regex_hits}  llm_calls={llm_calls}")

        if args.dry_run and i <= 3:
            print(f"\n  Sample â€” {nct_id}:")
            for c in CONCEPTS_21:
                v = final.get(c)
                if v:
                    print(f"    {c:<35} {v}")

        if not args.dry_run and (len(updates) >= BATCH_SIZE or i == len(trials)):
            _bulk_update(conn, updates)
            updates = []

    if not args.dry_run:
        if updates:
            _bulk_update(conn, updates)
        print(f"\n  [DONE] regex_hits={regex_hits}  llm_calls={llm_calls}")
    else:
        print(f"\n  [DRY RUN] Would update {len(trials)} trials")
        print(f"  regex_hits={regex_hits}  llm_calls_would_be={llm_calls}")

    conn.close()


def _bulk_update(conn, updates: list[dict]) -> None:
    if not updates:
        return
    # Only SET the 21 active concepts â€” the 7 removed columns are left untouched in DB
    set_clause       = ", ".join(f"{c} = v.{c}" for c in CONCEPTS_21)
    values_template  = "(" + ", ".join(["%s"] * (2 + len(CONCEPTS_21))) + ")"

    rows = [
        tuple([u["nct_id"], u["dept"]] + [u.get(c) for c in CONCEPTS_21])
        for u in updates
    ]

    sql = f"""
        UPDATE "CT".organized_trials AS o
        SET {set_clause}
        FROM (VALUES %s) AS v(nct_id, dept, {", ".join(CONCEPTS_21)})
        WHERE o.nct_id = v.nct_id AND o.dept = v.dept
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, template=values_template)
    conn.commit()


# â”€â”€ Entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true", default=True,
                        help="Show extraction samples without writing (default)")
    parser.add_argument("--apply",    action="store_true",
                        help="Write extracted values to DB")
    parser.add_argument("--both",     action="store_true",
                        help="Apply to both PROD and AWS RDS (default: PROD only)")
    parser.add_argument("--dept",     type=str, default=None,
                        help="Filter by department (e.g. ADC)")
    parser.add_argument("--limit",    type=int, default=None,
                        help="Max trials to process")
    parser.add_argument("--no-llm",   action="store_true",
                        help="Regex-only mode, skip OpenAI calls")
    parser.add_argument("--force",    action="store_true",
                        help="Re-extract all trials regardless of existing values")
    args = parser.parse_args()

    if args.apply:
        args.dry_run = False

    targets = list(DATABASES.items())
    if not args.both:
        targets = [("PROD", DATABASES["PROD"])]

    for db_name, url in targets:
        if not url:
            print(f"  [SKIP] {db_name}: DATABASE_URL not set")
            continue
        process_db(db_name, url, args)

    print("\n\nDone.")
