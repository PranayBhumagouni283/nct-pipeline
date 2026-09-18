"""
conversational_agent.py -- Clinical Trials Conversational Agent (V1)

Full flow:
  User Query
    → Analyse + Intent Classification  (GPT-4o mini)
    → Route:
        EXACT_LOOKUP   → es_retriever.retrieve_by_nct_id()
        SEMANTIC_SEARCH → es_retriever.retrieve() [BGE-M3 + BM25 + RRF]
        DB_COUNT       → db_query_handler.handle_count()
        DB_LIST        → db_query_handler.handle_list()
        DB_STATS       → db_query_handler.handle_stats()
    → Result Validation                (GPT-4o mini)
    → Conversational Answer            (GPT-4o mini)

Usage:
    python conversational_agent.py                   # interactive REPL
    python conversational_agent.py --query "..."     # single query
    python conversational_agent.py --query "..." --verbose
"""
import os
import sys
import json
import argparse
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

from db_query_handler import execute_db_query
from es_retriever import retrieve, retrieve_by_nct_id, extract_nct_ids

# ── OpenAI client ─────────────────────────────────────────────────────────────

_openai_client = None

def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            sys.exit("OPENAI_API_KEY not set in .env")
        _openai_client = OpenAI(api_key=key)
    return _openai_client


# ── Intent Classification ─────────────────────────────────────────────────────

INTENT_SYSTEM = """You are a clinical trials query classifier for a pharma company tracking system.

The system tracks trials across three departments:
  - ADC (Antibody-Drug Conjugates): breast cancer, NSCLC, HNSCC, ovarian, cervical, endometrial, pancreatic, prostate, SCLC
  - Infectious Diseases (ID): CMV, HSV, Influenza, RSV, VZV
  - Liver Diseases (LD): HBV, HDV, PBC, PSC

Classify the user query into ONE of these intents:

  EXACT_LOOKUP    — Query contains a specific NCT ID (e.g. NCT04697628)
  SEMANTIC_SEARCH — Find trials by clinical meaning: endpoints (PFS, OS, pCR), biomarkers (HER2, EGFR),
                    mechanisms (immunotherapy, ADC), eligibility criteria, line of therapy (LOT),
                    specific clinical concepts, trial design questions
  DB_COUNT        — Count queries: "how many", "total number", "count of"
  DB_LIST         — List/filter by structured fields: show recruiting trials, phase 3 trials, ADC trials
  DB_STATS        — Aggregated statistics: breakdown, distribution, enrollment stats, summary
  CHANGE_HISTORY  — Version history or historical changes from trial_history table:
                    "show version history", "how many times has X changed", "what changed in ADC trials",
                    "trials that changed their eligibility", "which module changes most often"
  CHANGE_LOG      — Recent pipeline run changes from field_changes_log:
                    "what changed in the last run", "recent updates", "trials updated on date X",
                    "show changes for NCT...", "which trials had outcome measure changes recently"
  KOL             — Key Opinion Leader / investigator queries (direct Postgres lookup, no semantic):
                    "who is the PI of NCT...", "show KOLs for this trial", "which trials involve Dr. Smith",
                    "investigators for ADC trials", "who are the key opinion leaders for NCT..."

Return JSON with these fields:
  intent            : one of [EXACT_LOOKUP, SEMANTIC_SEARCH, DB_COUNT, DB_LIST, DB_STATS, CHANGE_HISTORY, CHANGE_LOG, KOL]
  corrected_query   : cleaned/expanded query for semantic search (fix typos, expand abbreviations)
  nct_ids           : list of NCT IDs found (e.g. ["NCT04697628"]) or []
  filters           : {
    dept            : department name or null (ADC / Infectious Diseases / Liver Diseases)
    phase           : normalized phase or null (phase 1 / phase 2 / phase 3 / phase 1/2 / phase 2/3)
    status          : normalized status or null (recruiting / completed / active / terminated)
    indication      : specific indication or null (Breast Cancer / NSCLC / HBV / RSV / etc.)
    nct_id          : specific NCT ID for change queries, or null
    module          : changed module name or null (Outcome Measures / Eligibility / Study Status /
                      Arms and Interventions / Study Design / Contacts/Locations / Sponsor/Collaborators)
    kol_name        : investigator name to search (for KOL intent, query_type=search), or null
    query_type      : for CHANGE_HISTORY — latest | versions | recent | module_stats
                        latest   = single most recent version for one NCT ID (full change detail)
                        versions = full version list for one NCT ID (all versions)
                        recent   = most recently changed trials across dept/module (1 row per trial)
                        module_stats = top modules by change frequency
                      for CHANGE_LOG     — latest_run | trial | module_filter | stats (default: latest_run)
    run_date        : specific pipeline run date YYYY-MM-DD or null (for CHANGE_LOG)
    date_from       : start date YYYY-MM-DD or null.
                      AUTO-SET to (today - 12 months) when user says "recently", "in the last year",
                      "in the past year", "recent changes", "new changes" (for CHANGE_HISTORY queries).
                      For explicit periods: "last 6 months" → today-6mo, "this year" → Jan 1 this year.
    date_to         : end date YYYY-MM-DD or null
  }
  db_metric         : for DB_STATS — one of [phase_breakdown, status_breakdown, enrollment_stats, all] or null
  is_ambiguous      : true if query is unclear or missing critical context
  clarification     : if ambiguous, what to ask the user; otherwise null

Examples:
  "how many ADC trials do we have?" → DB_COUNT, dept=ADC
  "ADC trials" → DB_COUNT, dept=ADC  ← vague dept-only = summary count, NOT a list
  "Liver Disease Trials" → DB_COUNT, dept=Liver Diseases  ← vague = summary, NOT a list
  "Breast Cancer" → DB_COUNT, indication=Breast Cancer  ← vague = summary, NOT a list
  "show recruiting phase 3 trials" → DB_LIST, status=recruiting, phase=phase 3
  "list ADC trials" → DB_LIST, dept=ADC  ← explicit "show"/"list" = list
  "HER2 positive breast cancer immunotherapy trials" → SEMANTIC_SEARCH
  "trials with PFS as primary endpoint" → SEMANTIC_SEARCH
  "NCT04697628" → EXACT_LOOKUP
  "phase breakdown for liver diseases" → DB_STATS, dept=Liver Diseases, db_metric=phase_breakdown
  "what are the eligibility criteria for ADC trials in prostate cancer" → SEMANTIC_SEARCH
  "what changed in NCT04697628?" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=latest
  "what are the last modules changed for NCT04697628?" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=latest
  "what was the most recent update to NCT04697628?" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=latest
  "what changed in NCT04697628 last time?" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=latest
  "latest change for NCT04697628" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=latest
  "what modules changed in the last version of NCT04697628?" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=latest
  "show version history for NCT04697628" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=versions
  "show me all changes for NCT04697628" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=versions
  "how many times has NCT04697628 been updated?" → CHANGE_HISTORY, nct_id=NCT04697628, query_type=versions
  "which ADC trials changed recently?" → CHANGE_HISTORY, dept=ADC, query_type=recent, date_from=(today-12mo)
  "trials that changed their eligibility criteria recently" → CHANGE_HISTORY, module=Eligibility, query_type=recent, date_from=(today-12mo)
  "trials that changed their eligibility criteria" → CHANGE_HISTORY, module=Eligibility, query_type=recent (no date_from)
  "which modules change most often?" → CHANGE_HISTORY, query_type=module_stats
  "what changed in the last pipeline run?" → CHANGE_LOG, query_type=latest_run
  "which trials had outcome measure changes in last run?" → CHANGE_LOG, module=Outcome Measures, query_type=latest_run
  "change statistics for ADC" → CHANGE_LOG, dept=ADC, query_type=stats
  "who is the PI of NCT04697628?" → KOL, nct_id=NCT04697628, query_type=trial
  "show KOLs for NCT04332822" → KOL, nct_id=NCT04332822, query_type=trial
  "which trials involve Dr. Jerkeman?" → KOL, kol_name=Jerkeman, query_type=search
  "find trials where Smith is investigator" → KOL, kol_name=Smith, query_type=search
  "list KOLs for ADC trials" → KOL, dept=ADC, query_type=dept_list
  "who are the key opinion leaders in our ADC portfolio?" → KOL, dept=ADC, query_type=dept_list
  "investigators for NCT04697628" → KOL, nct_id=NCT04697628, query_type=trial

IMPORTANT for NCT-specific change queries:
- "what changed", "latest change", "last update", "most recent change", "last modules changed",
  "what modules changed last" for a specific NCT ID → CHANGE_HISTORY, query_type=latest
  (NOT CHANGE_LOG — field_changes_log covers only recent pipeline runs, not full history)
- "show all changes / full history / all versions" for a specific NCT ID → CHANGE_HISTORY, query_type=versions
- CHANGE_LOG is ONLY for pipeline-run monitoring ("last pipeline run", "last run", "pipeline changes")

RULES for DB_COUNT vs DB_LIST:
- DB_LIST when ANY of these: user says "show", "list", "find", "give me" + trials; OR query contains
  an explicit status filter (recruiting, terminated, completed, active, suspended); OR query combines
  status+phase as filters (implies browsing, not counting).
- DB_COUNT when query is vague — dept name only, indication name only, or "how many" phrasing.
- Examples that are DB_LIST: "recruiting trials", "terminated PSC trials", "list ADC trials",
  "show phase 3 trials", "find completed RSV trials", "active HBV phase 2 trials",
  "show me all trials", "show me trials" (explicit show = always DB_LIST even with no other filters).
- Examples that are DB_COUNT: "ADC trials", "Breast Cancer", "Liver Disease Trials", "RSV trials".

IMPORTANT: Distinguish CHANGE_HISTORY vs CHANGE_LOG carefully:
- CHANGE_HISTORY → trial_history table. Use when: "version history", "how many times changed",
  "what has changed over time", "changed their [module]", "historical changes", "updated X times",
  "show changes for trial X". Default for general change questions about trials.
- CHANGE_LOG → field_changes_log only. Use ONLY when: user explicitly mentions "last pipeline run",
  "most recent run", "last run", "pipeline run on [date]", "run date". This is our internal pipeline
  monitoring data, not general trial change history.
- "recently changed" WITHOUT mentioning "run" → CHANGE_HISTORY (query_type=recent), NOT CHANGE_LOG.
"""


def classify_intent(query: str, verbose: bool = False) -> dict:
    """Classify user query into structured intent using GPT-4o mini."""
    from datetime import date, timedelta
    client = _get_openai()

    today     = date.today()
    twelve_mo = (today - timedelta(days=365)).isoformat()
    six_mo    = (today - timedelta(days=182)).isoformat()
    jan_1     = date(today.year, 1, 1).isoformat()

    system_with_dates = (
        INTENT_SYSTEM
        + f"\n\nToday's date: {today.isoformat()}."
        f"\ndate_from rules — ONLY set date_from when the user includes an explicit time word:"
        f"\n  'recently' or 'in the last year' → date_from={twelve_mo}"
        f"\n  'last 6 months' or 'past 6 months' → date_from={six_mo}"
        f"\n  'this year' or 'in 2026' → date_from={jan_1}"
        f"\n  No time word in query → date_from=null. Do NOT infer recency."
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_with_dates},
            {"role": "user",   "content": query},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=400,
    )

    raw = resp.choices[0].message.content
    intent = json.loads(raw)

    # Ensure required keys exist with defaults
    intent.setdefault("nct_ids",        [])
    intent.setdefault("filters",        {})
    intent.setdefault("db_metric",      None)
    intent.setdefault("is_ambiguous",   False)
    intent.setdefault("clarification",  None)
    intent.setdefault("corrected_query", query)

    for key in ["dept", "phase", "status", "indication"]:
        intent["filters"].setdefault(key, None)

    if verbose:
        print(f"\n[Intent] {json.dumps(intent, indent=2)}")

    return intent


# ── Retrieval routing ─────────────────────────────────────────────────────────

def route_query(intent: dict, original_query: str, top_k: int = 10, verbose: bool = False) -> dict:
    """Route to the correct handler and return raw results."""
    kind    = intent.get("intent", "SEMANTIC_SEARCH")
    filters = intent.get("filters", {})

    if kind == "EXACT_LOOKUP":
        nct_ids = intent.get("nct_ids") or extract_nct_ids(original_query)
        results = []
        for nct in nct_ids:
            results.extend(retrieve_by_nct_id(nct, top_k=top_k))
        return {"type": "semantic", "intent": kind, "results": results}

    if kind == "SEMANTIC_SEARCH":
        corrected = intent.get("corrected_query") or original_query
        results = retrieve(
            query      = corrected,
            top_k      = top_k,
            dept       = filters.get("dept"),
            phase      = filters.get("phase"),
            status     = filters.get("status"),
        )
        if verbose:
            print(f"[Retrieval] {len(results)} trials returned for: '{corrected}'")
        return {"type": "semantic", "intent": kind, "results": results,
                "corrected_query": corrected}

    if kind in ("DB_COUNT", "DB_LIST", "DB_STATS", "CHANGE_HISTORY", "CHANGE_LOG", "KOL"):
        db_result = execute_db_query(kind, filters, intent.get("db_metric"))
        return {"type": "db", "intent": kind, "results": db_result}

    # Fallback
    return {"type": "semantic", "intent": "SEMANTIC_SEARCH",
            "results": retrieve(original_query, top_k=top_k)}


# ── Result Validation ─────────────────────────────────────────────────────────

VALIDATION_SYSTEM = """You are a result validator for a clinical trials search system.

Given the user's original query and the retrieved results, decide:
1. Do the results actually answer what the user asked?
2. If semantic search: are the top results relevant to the query?
3. If DB query: does the data make sense for the question?

Return JSON:
  is_relevant     : true/false — do results answer the query?
  confidence      : high / medium / low
  issues          : list of specific issues (empty if none)
  suggestion      : if not relevant, suggest a better query or approach; null if relevant
"""


def validate_results(query: str, intent: dict, raw_results: dict, verbose: bool = False) -> dict:
    """Quick LLM check: do results actually answer the user's question?"""
    client = _get_openai()

    # Build a compact summary of results for validation
    if raw_results["type"] == "semantic":
        results = raw_results.get("results", [])
        summary = f"Found {len(results)} trials.\n"
        for r in results[:5]:
            summary += f"  - {r['nct_id']}: {r.get('best_chunk_text', '')[:150]}\n"
    else:
        db_res  = raw_results.get("results", {})
        summary = json.dumps(db_res, default=str)[:800]

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system",  "content": VALIDATION_SYSTEM},
            {"role": "user",    "content": (
                f"User query: {query}\n"
                f"Intent: {intent.get('intent')}\n"
                f"Results summary:\n{summary}"
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=200,
    )

    validation = json.loads(resp.choices[0].message.content)
    validation.setdefault("is_relevant",  True)
    validation.setdefault("confidence",   "medium")
    validation.setdefault("issues",       [])
    validation.setdefault("suggestion",   None)

    if verbose:
        print(f"\n[Validation] {json.dumps(validation, indent=2)}")

    return validation


# ── Response Generation ───────────────────────────────────────────────────────

RESPONSE_SYSTEM = """You are a clinical trials intelligence assistant for a pharma company.

Generate a clear, concise, conversational answer based on the retrieved data.

Guidelines:
- Be specific and data-driven — cite NCT IDs, trial counts, phases, statuses
- For semantic results: highlight the most relevant trials with key details
- For DB results: present counts/stats clearly, use bullet points or short tables in markdown
- For change/history results: summarise what changed, which modules, how many times
  - trial_history_versions: list versions chronologically with date, status, and modules changed
  - trial_history_latest: single most recent version for one NCT ID. Show: trial title, version number,
    version date, status, modules changed, then the full field_changes text formatted as a readable
    change log (each [Module] section as a subheading, each field change as a bullet point).
    Also mention total_versions so the user knows the full history depth.
  - trial_history_recent: list trials with their most recent change date and changed modules.
    If total=0 and no_recent_context is present: tell the user NO trials changed in that period,
    then cite the last_changes entries (nct_id, version_date, modules_changed) so they know
    when the most recent change actually occurred. Example: "No ADC trials changed their
    eligibility criteria in the last 12 months. The most recent eligibility change was
    NCT00003131 on 2007-02-08 (modules: Eligibility, Study Status)."
  - trial_history_module_stats: present as a ranked list of modules with change counts
  - change_log_entries: list trials with run_date, modules changed, and field change count
  - change_log_stats: show changes per run date and top modules as a summary table
- Keep the answer focused — don't pad with generic statements
- If top results are shown, mention how many total were found
- Use plain language — avoid jargon unless the user used it
- Maximum 350 words
"""


def _trim_db_result(db_res: dict) -> dict:
    """
    Remove heavy text blobs before passing DB results to the LLM.
    field_changes can be thousands of characters per entry.
    Exception: trial_history_latest keeps full field_changes (user wants to read it).
    """
    import copy
    res = copy.deepcopy(db_res)
    HEAVY_FIELDS = ("full_data", "curr_full_data")
    TRUNCATE_FIELDS = ("field_changes",)

    # For latest-version queries, show field_changes in full (up to 3000 chars)
    if res.get("type") == "trial_history_latest" and res.get("latest"):
        for f in ("full_data", "curr_full_data"):
            if res["latest"].get(f):
                res["latest"][f] = str(res["latest"][f])[:200] + "…"
        if res["latest"].get("field_changes"):
            res["latest"]["field_changes"] = str(res["latest"]["field_changes"])[:3000]
        return res

    # For list results: truncate field_changes to 200 chars, strip full_data entirely
    for list_key in ("versions", "trials", "entries"):
        if list_key in res:
            for row in res[list_key]:
                for f in HEAVY_FIELDS:
                    if f in row and row[f]:
                        row[f] = str(row[f])[:200] + "…"
                for f in TRUNCATE_FIELDS:
                    if f in row and row[f]:
                        row[f] = str(row[f])[:200] + "…"
    return res


def generate_response(
    query:      str,
    intent:     dict,
    raw_results: dict,
    validation: dict,
    verbose:    bool = False,
) -> str:
    """Generate a conversational answer from results using GPT-4o mini."""
    client = _get_openai()

    # Build context for the LLM
    if raw_results["type"] == "semantic":
        results = raw_results.get("results", [])
        corrected = raw_results.get("corrected_query", query)
        context_parts = [f"Total results: {len(results)}"]
        if corrected != query:
            context_parts.append(f"(Searched for: '{corrected}')")
        for i, r in enumerate(results[:10], 1):
            chunk = r.get("best_chunk_text", "")[:300]
            context_parts.append(
                f"\n{i}. {r['nct_id']} | {r.get('phase','')} | {r.get('study_status','')} | "
                f"{r.get('conditions','')[:80]}\n   {chunk}"
            )
        context = "\n".join(context_parts)

    else:
        db_res = raw_results.get("results", {})
        # Strip heavy text fields before serialising to avoid context overflow
        db_res_clean = _trim_db_result(db_res)
        context = json.dumps(db_res_clean, default=str, indent=2)[:6000]

    # Include validation issues if any
    issues_note = ""
    if not validation.get("is_relevant") and validation.get("suggestion"):
        issues_note = f"\nNote: {validation['suggestion']}"

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": RESPONSE_SYSTEM},
            {"role": "user",   "content": (
                f"User question: {query}\n\n"
                f"Retrieved data:\n{context}"
                f"{issues_note}"
            )},
        ],
        temperature=0.3,
        max_tokens=500,
    )

    return resp.choices[0].message.content.strip()


# ── Main agent function ───────────────────────────────────────────────────────

def ask(query: str, top_k: int = 10, verbose: bool = False) -> str:
    """
    Full pipeline: query → intent → retrieval → validation → answer.
    Returns the conversational answer string.
    """
    query = query.strip()
    if not query:
        return "Please enter a query."

    # Step 1: Intent classification
    intent = classify_intent(query, verbose=verbose)

    # Step 2: Handle ambiguous queries
    if intent.get("is_ambiguous") and intent.get("clarification"):
        return f"Could you clarify: {intent['clarification']}"

    # Step 3: Route to correct handler
    raw_results = route_query(intent, query, top_k=top_k, verbose=verbose)

    # Step 4: Validate results
    validation = validate_results(query, intent, raw_results, verbose=verbose)

    # Step 5: If validation fails and it was a DB query, try semantic search as fallback
    if (not validation.get("is_relevant") and
            raw_results["type"] == "db" and
            validation.get("confidence") == "low"):
        if verbose:
            print("[Fallback] DB results not relevant, trying semantic search...")
        raw_results = route_query(
            {"intent": "SEMANTIC_SEARCH", "corrected_query": query,
             "filters": intent.get("filters", {})},
            query, top_k=top_k, verbose=verbose
        )
        validation = validate_results(query, intent, raw_results, verbose=verbose)

    # Step 6: Generate conversational response
    answer = generate_response(query, intent, raw_results, validation, verbose=verbose)
    return answer


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Clinical Trials Conversational Agent")
    p.add_argument("--query",   help="Single query to run (non-interactive)")
    p.add_argument("--top-k",  type=int, default=10, help="Max trials to retrieve (default: 10)")
    p.add_argument("--verbose", action="store_true", help="Show intent + validation details")
    args = p.parse_args()

    if args.query:
        answer = ask(args.query, top_k=args.top_k, verbose=args.verbose)
        print(f"\n{answer}\n")
        return

    # Interactive REPL
    print("Clinical Trials Conversational Agent")
    print("Type your question or 'quit' to exit.\n")
    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break

        answer = ask(query, top_k=args.top_k, verbose=args.verbose)
        print(f"\nAgent: {answer}\n")


if __name__ == "__main__":
    main()
