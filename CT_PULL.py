import concurrent.futures
import requests
import pandas as pd
import json
import time
import re
from pathlib import Path

# ============================================================================
# 1. CONFIGURATION
# ============================================================================

_HERE = Path(__file__).parent

INPUT_CSV_PATH   = _HERE / "ADC_All_NCTIDs-14May2026.xlsx"
OUTPUT_DIR       = _HERE / "NCT_output"
JSON_CACHE_DIR   = OUTPUT_DIR / "nct_cache"
FAILED_JSON_FILE = OUTPUT_DIR / "failed_ncts.json"
OUTPUT_CSV_RAW   = OUTPUT_DIR / "Clinical_Trials_Raw.csv"

# API Limits & Retries
MAX_WORKERS = 2
DELAY = 2.5           # Safe API rate limit spacing
MAX_RETRIES = 3       # Times to retry a failed API call
RETRY_DELAY = 5.0     # Seconds to wait between retries


# ============================================================================
# 2. API & WORKER LOGIC
# ============================================================================

def fetch_study_data(nct_id):
    """Fetches the COMPLETE data packet from the API with Retries."""
    url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    last_error = None
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            return response.json(), None  # Success!
            
        except requests.exceptions.HTTPError as e:
            last_error = f"HTTP Error: {e.response.status_code}"
            # 404 means the NCT ID doesn't exist. Break early.
            if e.response.status_code == 404:
                break
        except Exception as e:
            last_error = f"Error: {str(e)}"
            
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
            
    return None, f"{last_error} (After {MAX_RETRIES} attempts)"

def build_raw_row(data: dict) -> dict:
    """Build the flat raw-row dict from a public API response."""
    p = data.get('protocolSection', {})
    d = data.get('derivedSection', {})
    ident     = p.get('identificationModule', {})
    status    = p.get('statusModule', {})
    sponsor   = p.get('sponsorCollaboratorsModule', {})
    oversight = p.get('oversightModule', {})
    desc      = p.get('descriptionModule', {})
    cond      = p.get('conditionsModule', {})
    design    = p.get('designModule', {})
    arms      = p.get('armsInterventionsModule', {})
    outcomes  = p.get('outcomesModule', {})
    elig      = p.get('eligibilityModule', {})
    contacts  = p.get('contactsLocationsModule', {})
    refs      = p.get('referencesModule', {})
    ipd       = p.get('ipdSharingStatementModule', {})
    return {
        'NCT ID':                        ident.get('nctId'),
        'Title':                         ident.get('officialTitle'),
        'RAW_Identification':            json.dumps(ident,     indent=2),
        'RAW_Status':                    json.dumps(status,    indent=2),
        'RAW_SponsorCollaborators':      json.dumps(sponsor,   indent=2),
        'RAW_Oversight':                 json.dumps(oversight, indent=2),
        'RAW_Description':               json.dumps(desc,      indent=2),
        'RAW_Conditions':                json.dumps(cond,      indent=2),
        'RAW_Design':                    json.dumps(design,    indent=2),
        'RAW_ArmsInterventions':         json.dumps(arms,      indent=2),
        'RAW_Outcomes':                  json.dumps(outcomes,  indent=2),
        'RAW_Eligibility':               json.dumps(elig,      indent=2),
        'RAW_ContactsLocations':         json.dumps(contacts,  indent=2),
        'RAW_References':                json.dumps(refs,      indent=2),
        'RAW_IPDSharing':                json.dumps(ipd,       indent=2),
        'RAW_Derived_ConditionBrowse':   json.dumps(d.get('conditionBrowseModule',      {}), indent=2),
        'RAW_Derived_InterventionBrowse':json.dumps(d.get('interventionBrowseModule',   {}), indent=2),
        'RAW_Derived_Misc':              json.dumps(d.get('miscInfoModule',             {}), indent=2),
    }


def process_nct(nct):
    """Worker function: Checks cache first, otherwise fetches from API."""
    cache_file = JSON_CACHE_DIR / f"{nct}.json"

    # 1. Check cache
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
                raw_data = cached_data.get("raw", cached_data)
                return raw_data, True, None
        except json.JSONDecodeError:
            pass

    # 2. Fetch from API
    data, error_msg = fetch_study_data(nct)
    time.sleep(DELAY)

    if data:
        raw_row = build_raw_row(data)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"raw": raw_row}, f, indent=2, ensure_ascii=False)
        return raw_row, False, None
    else:
        err_row = {'NCT ID': nct, 'Title': 'Error Fetching'}
        return err_row, False, error_msg


# ============================================================================
# 3. MAIN EXECUTION
# ============================================================================

def save_failed_json(failed_dict):
    """Helper to save failed NCTs to JSON."""
    if failed_dict:
        with open(FAILED_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(failed_dict, f, indent=4)
        print(f"\nâš ï¸ Saved {len(failed_dict)} failed NCT IDs to {FAILED_JSON_FILE}.")
    elif FAILED_JSON_FILE.exists():
        FAILED_JSON_FILE.unlink()

if __name__ == '__main__':
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("="*60)
    print("ClinicalTrials.gov Raw Data Extractor")
    print("="*60)
    
    if FAILED_JSON_FILE.exists():
        try:
            with open(FAILED_JSON_FILE, "r", encoding="utf-8") as f:
                prev_failed = json.load(f)
                if prev_failed:
                    print(f"ðŸ”„ Found {len(prev_failed)} failed NCT IDs from a previous run. Retrying...")
        except json.JSONDecodeError:
            pass

    print(f"Reading NCT IDs from {INPUT_CSV_PATH}...")

    try:
        input_df = pd.read_excel(INPUT_CSV_PATH)

        if 'Trial Identifier' not in input_df.columns:
            print("âŒ Error: Could not find a column named 'Trial Identifier' in the Excel file.")
            exit()

        raw_ids = input_df['Trial Identifier'].dropna().astype(str).tolist()
        
        nct_ids = []
        for item in raw_ids:
            found = re.findall(r'NCT\d+', item)
            if found:
                nct_ids.extend(found)
        
        nct_ids = sorted(list(set(nct_ids)))
        total = len(nct_ids)
        print(f"âœ… Total Unique NCT IDs found: {total:,}")

    except Exception as e:
        print(f"âŒ Error reading the CSV file: {e}")
        exit()

    print(f"\nðŸš€ Starting Data Fetch with {MAX_WORKERS} Workers...")
    raw_data_rows = []
    failed_ncts_dict = {}  
    completed = 0

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = executor.map(process_nct, nct_ids)
            
            for raw_row, from_cache, error_msg in results:
                completed += 1
                nct_id = raw_row.get('NCT ID')
                
                if error_msg:
                    print(f"[{completed}/{total}] âŒ FAILED {nct_id} ({error_msg})")
                    failed_ncts_dict[nct_id] = error_msg
                else:
                    status_icon = "ðŸ“¦" if from_cache else "ðŸŒ"
                    source_text = "Cache" if from_cache else "API"
                    print(f"[{completed}/{total}] {status_icon} Processed {nct_id} (via {source_text})")
                
                raw_data_rows.append(raw_row)
                
    except KeyboardInterrupt:
        print("\n\nâš ï¸  INTERRUPTED BY USER â€” Data fetched so far is safely saved in the cache.")
        save_failed_json(failed_ncts_dict)
        print("Run the script again to resume exactly where you left off.\n")

    # --- SAVE FAILED JSON ---
    save_failed_json(failed_ncts_dict)

    # --- SAVE TO CSV ---
    if raw_data_rows:
        print(f"\nðŸ’¾ Saving {len(raw_data_rows)} rows to CSV file...")
        
        pd.DataFrame(raw_data_rows).to_csv(OUTPUT_CSV_RAW, index=False, encoding='utf-8')
        print(f"  âœ“ Raw JSON data saved to: {OUTPUT_CSV_RAW.name}")

        print("\nðŸŽ‰ Success! Process finished.")
    else:
        print("No data extracted.")