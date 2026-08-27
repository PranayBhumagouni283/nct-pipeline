"""
add_trials_from_excel.py
------------------------
Reads NCT IDs from the "Trials_To_Add" sheet of an Excel file and adds them
to the ADC asset tracking list.

For each new trial (not already in tracking_list):
  1. Fetch full data from CT.gov API v2
  2. Parse into the 62-column organized schema
  3. Tag Primary Drug using current ADC drug keywords
  4. Upsert into organized_trials
  5. Insert into tracking_list (dept=ADC, indication='')
  6. Remove from new_candidates_log / unmatched_log if present

Usage:
    python add_trials_from_excel.py
"""

import re
import sys
import time
import requests
import openpyxl
from datetime import datetime, date, timezone

import db
import norm

# ── Config ────────────────────────────────────────────────────────────────────

EXCEL_PATH = r"C:\Users\LAPTOP\Downloads\CT_Update_by_Assest_Search-25Aug2026(Trials to add).xlsx"
SHEET_NAME = "Trials_To_Add"
DEPT       = "ADC"
INDICATION = ""           # asset pipeline — empty string, not 'asset'
TODAY      = str(date.today())
API_BASE   = "https://clinicaltrials.gov/api/v2/studies"
API_HDR    = {"User-Agent": "TrialsTracker/1.0"}
RATE_DELAY = 0.3          # seconds between API calls

# ── Helpers (inlined from combined_pipeline.py) ───────────────────────────────

def _nest(obj, *keys, default=""):
    curr = obj
    for k in keys:
        if isinstance(curr, dict) and k in curr:
            curr = curr[k]
        else:
            return default
    return curr if curr is not None else default


def _fmt_outcomes(lst):
    return "\n".join(
        f"{i}. {o.get('measure','N/A')} [Timeframe: {o.get('timeFrame','No timeframe')}]"
        for i, o in enumerate(lst, 1)
    ) if lst else ""


def _fmt_contacts(lst):
    parts = []
    for c in lst:
        s = f"{c.get('name','')} ({c.get('role','')})"
        if c.get("phone") or c.get("email"):
            s += f" - Phone: {c.get('phone','')}, Email: {c.get('email','')}"
        parts.append(s)
    return "\n".join(parts)


def _fmt_officials(lst):
    return "\n".join(
        f"{o.get('name','')} ({o.get('role','')}) - {o.get('affiliation','')}"
        for o in lst
    )


def _fmt_locations(locs):
    if not locs:
        return ""
    grouped = {}
    for loc in locs:
        country = loc.get("country", "Unknown")
        state   = loc.get("state", "Other")
        grouped.setdefault(country, {}).setdefault(state, [])
        parts = [loc.get("city"), loc.get("state"), loc.get("country"), loc.get("zip")]
        addr  = ", ".join(str(p) for p in parts if p)
        grouped[country][state].append((addr, loc.get("facility", "")))
    lines = [f"Total Locations: {len(locs)}", "", "--- Locations ---"]
    for i, country in enumerate(sorted(grouped), 1):
        lines.append(f"{i}) {country}:")
        for state in sorted(grouped[country]):
            lines.append(f"   - {state}:")
            for addr, fac in grouped[country][state]:
                lines.append(f"       {addr}")
                if fac:
                    lines.append(f"       {fac}")
            lines.append("")
    return "\n".join(lines).strip()


def _extract_countries(locs):
    seen = []
    for loc in locs:
        c = (loc.get("country") or "").strip()
        if c and c not in seen:
            seen.append(c)
    return " | ".join(sorted(seen))


def _extract_sites(locs):
    seen = []
    for loc in locs:
        fac = (loc.get("facility") or "").strip()
        fac = re.sub(r'\s*/ID#\s*\S+', '', fac).strip()
        fac = re.sub(r'^\d+\.\s+', '', fac).strip()
        if not fac or not re.search(r'[A-Za-z]', fac):
            continue
        if fac not in seen:
            seen.append(fac)
    return " | ".join(seen)


def parse_study_to_organized(nct_id, data):
    p  = data.get("protocolSection", {})
    d  = data.get("derivedSection", {})

    ident  = p.get("identificationModule", {})
    st     = p.get("statusModule", {})
    sp     = p.get("sponsorCollaboratorsModule", {})
    ov     = p.get("oversightModule", {})
    desc   = p.get("descriptionModule", {})
    cond_m = p.get("conditionsModule", {})
    design = p.get("designModule", {})
    arms   = p.get("armsInterventionsModule", {})
    outc   = p.get("outcomesModule", {})
    elig   = p.get("eligibilityModule", {})
    cl     = p.get("contactsLocationsModule", {})
    refs_m = p.get("referencesModule", {})
    ipd    = p.get("ipdSharingStatementModule", {})
    cb     = d.get("conditionBrowseModule", {})
    ib     = d.get("interventionBrowseModule", {})

    nct       = _nest(ident, "nctId") or nct_id
    org_id    = _nest(ident, "orgStudyIdInfo", "id")
    sec_ids   = [str(s.get("id", "")) for s in ident.get("secondaryIdInfos", []) if s.get("id")]
    other_ids = ", ".join(([str(org_id)] if org_id else []) + sec_ids)

    interventions_list = arms.get("interventions", [])
    interventions_str  = ", ".join(i.get("name", "") for i in interventions_list)

    refs      = refs_m.get("references", [])
    pmids     = [str(r.get("pmid", "")) for r in refs if r.get("pmid")]
    citations = [f"[{r.get('type','UNKNOWN')}] {r.get('citation','')}" for r in refs if r.get("citation")]

    docs = []
    for doc in d.get("largeDocModule", {}).get("largeDocs", []):
        fn = doc.get("filename", "")
        if fn and nct:
            url = f"https://cdn.clinicaltrials.gov/large-docs/{nct[-2:]}/{nct}/{fn}"
            docs.append(f"{doc.get('label','')}, {url}")

    return {
        "NCT ID":         nct,
        "Study URL":      f"https://clinicaltrials.gov/study/{nct}" if nct else "",
        "Other IDs":      other_ids,
        "Brief Title":    _nest(ident, "briefTitle"),
        "Official Title": _nest(ident, "officialTitle"),
        "Acronym":        _nest(ident, "acronym"),
        "Org Full Name":  _nest(ident, "organization", "fullName"),
        "Overall Status":              _nest(st, "overallStatus"),
        "Status Verified Date":        _nest(st, "statusVerifiedDate"),
        "Exclusion Rationale":         _nest(st, "whyStopped"),
        "Expanded Access Info":        str(_nest(st, "expandedAccessInfo", "hasExpandedAccess")),
        "Start Date":                  _nest(st, "startDateStruct", "date"),
        "Start DateType":              _nest(st, "startDateStruct", "type"),
        "Primary Completion Date":     _nest(st, "primaryCompletionDateStruct", "date"),
        "Primary Completion DateType": _nest(st, "primaryCompletionDateStruct", "type"),
        "completionDateStructDate":    _nest(st, "completionDateStruct", "date"),
        "completionDateStructType":    _nest(st, "completionDateStruct", "type"),
        "studyFirstSubmitDate":        _nest(st, "studyFirstSubmitDate"),
        "studyFirstSubmitQcDate":      _nest(st, "studyFirstSubmitQcDate"),
        "Study First Post Date":       _nest(st, "studyFirstPostDateStruct", "date"),
        "studyFirstPostDateType":      _nest(st, "studyFirstPostDateStruct", "type"),
        "Last Update Submit Date":     _nest(st, "lastUpdateSubmitDate"),
        "Last Update Post Date":       _nest(st, "lastUpdatePostDateStruct", "date"),
        "lastUpdatePostDateType":      _nest(st, "lastUpdatePostDateStruct", "type"),
        "Sponsors":                         norm.normalize_org(_nest(sp, "leadSponsor", "name") or ""),
        "Collaborators":                    norm.normalize_orgs_field(" | ".join(c.get("name", "") for c in sp.get("collaborators", []))),
        "Funder Type":                      _nest(sp, "leadSponsor", "class"),
        "responsiblePartyType":             _nest(sp, "responsibleParty", "type"),
        "responsiblePartyleadSponsor":      norm.normalize_org(_nest(sp, "leadSponsor", "name") or ""),
        "responsiblePartyleadSponsorclass": _nest(sp, "leadSponsor", "class"),
        "FDA Regulated Drug":   str(_nest(ov, "isFdaRegulatedDrug")),
        "FDA Regulated Device": str(_nest(ov, "isFdaRegulatedDevice")),
        "Has DMC":              str(_nest(ov, "hasDmc")),
        "briefSummary":        _nest(desc, "briefSummary"),
        "detailedDescription": _nest(desc, "detailedDescription"),
        "conditions":         norm.normalize_conditions_field(", ".join(cond_m.get("conditions", []))),
        "studyType":          _nest(design, "studyType"),
        "phases":             ", ".join(design.get("phases", [])),
        "Allocation":         _nest(design, "designInfo", "allocation"),
        "Intervention Model": _nest(design, "designInfo", "interventionModel"),
        "Masking":            _nest(design, "designInfo", "maskingInfo", "masking"),
        "Primary Purpose":    _nest(design, "designInfo", "primaryPurpose"),
        "Enrollment":         str(_nest(design, "enrollmentInfo", "count")),
        "enrollmentInfoType": _nest(design, "enrollmentInfo", "type"),
        "Interventions":      interventions_str,
        "Primary Outcomes":   _fmt_outcomes(outc.get("primaryOutcomes", [])),
        "Secondary Outcomes": _fmt_outcomes(outc.get("secondaryOutcomes", [])),
        "eligibilityCriteria": _nest(elig, "eligibilityCriteria"),
        "Sex":                 _nest(elig, "sex"),
        "Minimum Age":         _nest(elig, "minimumAge"),
        "Standard Ages":       ", ".join(elig.get("stdAges", [])),
        "healthyVolunteers":   str(_nest(elig, "healthyVolunteers")),
        "Central Contacts":    _fmt_contacts(cl.get("centralContacts", [])),
        "Overall Officials":   _fmt_officials(cl.get("overallOfficials", [])),
        "Locations":           _fmt_locations(cl.get("locations", [])),
        "countries":           _extract_countries(cl.get("locations", [])),
        "sites":               _extract_sites(cl.get("locations", [])),
        "IPD Sharing":         _nest(ipd, "ipdSharing"),
        "MeSH Conditions":     " | ".join(
            f"{m.get('id','')}: {m.get('term','')}"
            for m in cb.get("meshes", []) if m.get("term")
        ),
        "MeSH Interventions":  " | ".join(
            f"{m.get('id','')}: {m.get('term','')}"
            for m in ib.get("meshes", []) if m.get("term")
        ),
        "Study Documents":  " | ".join(docs),
        "Reference Count":  len(refs),
        "PMIDs":            ", ".join(pmids),
        "Citations":        " | ".join(citations),
        "Primary Drug":     "",
        "_parsed_date":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _compile_drug_patterns(drugs):
    compiled = []
    for drug_name, aliases in drugs:
        terms = [t for t in ([drug_name] + aliases) if t and t != "-"]
        if not terms:
            continue
        pattern = "|".join(
            r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])"
            for t in terms
        )
        compiled.append((drug_name, re.compile(pattern, re.IGNORECASE)))
    return compiled


def tag_primary_drug(interventions, compiled_drugs):
    if not interventions or not compiled_drugs:
        return ""
    matches = []
    for drug_name, pattern in compiled_drugs:
        if pattern.search(interventions):
            matches.append(drug_name)
    return " | ".join(matches)


# ── Core logic ────────────────────────────────────────────────────────────────

def read_excel_nct_ids():
    wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True)
    ws = wb[SHEET_NAME]
    seen, result = set(), []
    for row in ws.iter_rows(min_row=2, values_only=True):
        val = row[0]
        if val and str(val).strip().startswith("NCT"):
            nct = str(val).strip()
            if nct not in seen:
                seen.add(nct)
                result.append(nct)
    return result


def fetch_trial(nct_id):
    try:
        resp = requests.get(f"{API_BASE}/{nct_id}", headers=API_HDR, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        print(f"  [API] {nct_id}: HTTP {resp.status_code}")
        return None
    except Exception as e:
        print(f"  [API] {nct_id}: Error {e}")
        return None


def ph(n):
    return ",".join(["%s"] * n)


def main():
    print("=" * 60)
    print("ADC Add Trials from Excel")
    print("=" * 60)

    # 1. Read NCT IDs from Excel
    nct_ids = read_excel_nct_ids()
    print(f"\nExcel NCT IDs (unique): {len(nct_ids)}")

    conn = db._db()
    with conn.cursor() as cur:

        # 2. Find already-tracked
        cur.execute(
            f"SELECT nct_id FROM tracking_list WHERE dept = %s AND nct_id IN ({ph(len(nct_ids))})",
            [DEPT] + nct_ids,
        )
        already_tracked = {r["nct_id"] for r in cur.fetchall()}
        print(f"Already in tracking_list: {len(already_tracked)}")

        # 3. Find already in organized_trials
        cur.execute(
            f"SELECT nct_id FROM organized_trials WHERE dept = %s AND nct_id IN ({ph(len(nct_ids))})",
            [DEPT] + nct_ids,
        )
        already_organized = {r["nct_id"] for r in cur.fetchall()}
        print(f"Already in organized_trials: {len(already_organized)}")

        # 4. Check presence in candidate/unmatched logs
        cur.execute(
            f"SELECT DISTINCT nct_id, decision FROM new_candidates_log WHERE dept=%s AND nct_id IN ({ph(len(nct_ids))})",
            [DEPT] + nct_ids,
        )
        in_nc = cur.fetchall()
        cur.execute(
            f"SELECT DISTINCT nct_id, decision FROM unmatched_log WHERE dept=%s AND nct_id IN ({ph(len(nct_ids))})",
            [DEPT] + nct_ids,
        )
        in_um = cur.fetchall()
        print(f"In new_candidates_log: {len(in_nc)}")
        print(f"In unmatched_log:      {len(in_um)}")

    # 5. Load drug keywords for Primary Drug tagging
    print("\nLoading drug keywords...")
    raw_drugs = []
    for row in db.load_dept_keywords(DEPT):
        name = str(row.get("drug_name", "") or "").strip()
        if not name or name in ("nan", "-"):
            continue
        alias_raw = str(row.get("alias_names", "") or "").strip()
        aliases = []
        if alias_raw and alias_raw not in ("-", "nan", ""):
            for a in re.split(r"[|,]", alias_raw):
                a = a.strip()
                if a and a not in ("-", ""):
                    aliases.append(a)
        raw_drugs.append((name, aliases))
    compiled_drugs = _compile_drug_patterns(raw_drugs)
    print(f"Loaded {len(raw_drugs)} drug keywords")

    # 6. Process trials that need fetching
    to_add     = [n for n in nct_ids if n not in already_tracked]
    need_fetch = [n for n in to_add if n not in already_organized]

    print(f"\nTrials to add to tracking_list: {len(to_add)}")
    print(f"Trials needing CT.gov fetch:    {len(need_fetch)}")

    parsed_rows = {}
    if need_fetch:
        print(f"\nFetching {len(need_fetch)} trials from CT.gov...")
        for i, nct_id in enumerate(need_fetch, 1):
            print(f"  [{i}/{len(need_fetch)}] {nct_id}", end=" ... ", flush=True)
            data = fetch_trial(nct_id)
            if data:
                row = parse_study_to_organized(nct_id, data)
                row["Primary Drug"] = tag_primary_drug(row.get("Interventions", ""), compiled_drugs)
                parsed_rows[nct_id] = row
                print(f"OK  (status={row.get('Overall Status','?')}, drug={row.get('Primary Drug') or '-'})")
            else:
                print("FAILED — skipped")
            time.sleep(RATE_DELAY)

    # 7. Upsert into organized_trials
    if parsed_rows:
        print(f"\nUpserting {len(parsed_rows)} rows into organized_trials...")
        db.upsert_organized_trials(DEPT, list(parsed_rows.values()))

    # 8. Remove from new_candidates_log
    nc_ids = list({r["nct_id"] for r in in_nc})
    if nc_ids:
        conn = db._db()
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM new_candidates_log WHERE dept=%s AND nct_id IN ({ph(len(nc_ids))})",
                [DEPT] + nc_ids,
            )
            print(f"\nDeleted {cur.rowcount} rows from new_candidates_log")
            conn.commit()

    # 9. Remove from unmatched_log
    um_ids = list({r["nct_id"] for r in in_um})
    if um_ids:
        conn = db._db()
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM unmatched_log WHERE dept=%s AND nct_id IN ({ph(len(um_ids))})",
                [DEPT] + um_ids,
            )
            print(f"Deleted {cur.rowcount} rows from unmatched_log")
            conn.commit()

    # 10. Insert into tracking_list
    print(f"\nInserting {len(to_add)} trials into tracking_list...")
    inserted = 0
    skipped  = 0
    conn = db._db()
    with conn.cursor() as cur:
        for nct in to_add:
            cur.execute("""
                INSERT INTO tracking_list (nct_id, dept, indication, added_date, added_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (nct_id, dept, indication) DO NOTHING
            """, (nct, DEPT, INDICATION, TODAY, "manual_excel"))
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
    print(f"  Inserted: {inserted}  |  Skipped (conflict): {skipped}")

    # 11. Summary
    conn = db._db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(DISTINCT nct_id) FROM tracking_list WHERE dept=%s",
            (DEPT,),
        )
        total = cur.fetchone()["count"]
    print(f"\n{'='*60}")
    print(f"ADC tracking_list total now: {total} distinct NCT IDs")
    print(f"{'='*60}")

    # List any NCTs that failed API fetch (not in organized_trials and not fetched)
    failed = [n for n in need_fetch if n not in parsed_rows]
    if failed:
        print(f"\nWARNING — {len(failed)} NCTs could not be fetched from CT.gov:")
        for n in failed:
            print(f"  {n}")
        print("These were added to tracking_list but are missing from organized_trials.")
        print("Run the pipeline to populate their data.")


if __name__ == "__main__":
    main()
