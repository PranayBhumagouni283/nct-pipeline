"""
ClinicalTrials.gov -- All Version Pairs Comparison
--------------------------------------------------
For each NCT ID, compares EVERY consecutive version pair.
ONE row per NCT is written into the Excel workbook.

  NCT with 34 versions  ->  1 row with 33 "Field Changes (vX->vY)" columns
  NCT with 1 version    ->  1 row (note: SINGLE_VERSION)
  NCT not found         ->  1 row (note: NCT_NOT_FOUND)

Fixed output columns:
  NCT ID              -- e.g. NCT05646706
  Note                -- empty = OK; otherwise explains why comparison is missing
  Total Versions      -- total number of recorded versions for this NCT
  From Date           -- all version-pair from-dates stacked (one per line)
  To Date             -- all version-pair to-dates stacked (one per line)
  Status              -- all version-pair statuses stacked (one per line)
  Modules Changed     -- all version-pair modules stacked (one per line)
  Field Change Count  -- all version-pair change counts stacked (one per line)

Dynamic output columns (one per consecutive version pair found across all NCTs):
  Field Changes (v0->v1), Field Changes (v1->v2), ...

Use latest_compare.py if you only need the most recent update per NCT.

Install:
    pip install requests openpyxl
"""

import html
import re
import time
import requests
try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
except ImportError:
    _scraper = None
from collections import defaultdict
from threading import Lock
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

try:
    from openpyxl.cell.rich_text import CellRichText, TextBlock, InlineFont as _InlineFont
    _RICH_TEXT = True
except ImportError:
    _RICH_TEXT = False


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://clinicaltrials.gov/",
}

BASE_URL = "https://clinicaltrials.gov/api/int/studies"

MODULE_LABEL_MAP: dict[str, tuple[str, str] | None] = {
    "Study Status":               ("protocolSection", "statusModule"),
    "Contacts/Locations":         ("protocolSection", "contactsLocationsModule"),
    "Outcome Measures":           ("protocolSection", "outcomesModule"),
    "Eligibility":                ("protocolSection", "eligibilityModule"),
    "Arms/Interventions":         ("protocolSection", "armsInterventionsModule"),
    "Study Design":               ("protocolSection", "designModule"),
    "Description":                ("protocolSection", "descriptionModule"),
    "Sponsor/Collaborators":      ("protocolSection", "sponsorCollaboratorsModule"),
    "Identification":             ("protocolSection", "identificationModule"),
    "Conditions":                 ("protocolSection", "conditionsModule"),
    "References":                 ("protocolSection", "referencesModule"),
    "More Information":           ("protocolSection", "referencesModule"),
    "IPD Sharing":                ("protocolSection", "ipdSharingStatementModule"),
    "Participant Flow":           ("resultsSection", "participantFlowModule"),
    "Baseline Characteristics":   ("resultsSection", "baselineCharacteristicsModule"),
    "Outcome Measures (Results)": ("resultsSection", "outcomeMeasuresModule"),
    "Adverse Events":             ("resultsSection", "adverseEventsModule"),
    "Document Section":           None,
}

# â”€â”€ Excel config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_FIXED_COLS = [
    ("NCT ID",             16),
    ("Note",               24),
    ("Total Versions",     14),
    ("From Date",          28),
    ("To Date",            28),
    ("Status",             30),
    ("Modules Changed",    45),
    ("Field Change Count", 20),
]

_NOTE_FILLS = {
    "NCT_NOT_FOUND":         PatternFill("solid", fgColor="FFD7D7"),
    "INVALID_NCT_ID":        PatternFill("solid", fgColor="FFD7D7"),
    "NO_HISTORY":            PatternFill("solid", fgColor="FFF2CC"),
    "SINGLE_VERSION":        PatternFill("solid", fgColor="FFF2CC"),
    "NO_CHANGES_IN_VERSION": PatternFill("solid", fgColor="E2EFDA"),
}
_THIN   = Side(style="thin", color="CCCCCC")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


# â”€â”€ API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CT.gov added a session cookie gate on api/int â€” plain HTTP requests return 403.
# Fix: Playwright visits clinicaltrials.gov/search once to let JS set the required
# cookies (pinger_sid, ncbi_sid, etc.), then we inject them into a requests.Session.
# Fallback: if cookies stop working, Playwright navigates directly to the study history
# page and intercepts the api/int response in-flight.

_session: requests.Session | None = None
_session_lock = Lock()


def _harvest_cookies() -> requests.Session:
    """Use Playwright headless Chromium to collect CT.gov session cookies, return a Session."""
    from playwright.sync_api import sync_playwright
    print("  [cookies] Launching Playwright to harvest CT.gov session cookies...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()
        page.goto("https://clinicaltrials.gov/search", wait_until="networkidle", timeout=30000)
        cookies = page.context.cookies()
        browser.close()

    session = requests.Session()
    session.headers.update(HEADERS)
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
    names = [c["name"] for c in cookies]
    print(f"  [cookies] Harvested {len(cookies)} cookie(s): {names}")
    return session


def _get_session() -> requests.Session:
    """Return the shared cookie session, creating it once on first call."""
    global _session
    with _session_lock:
        if _session is None:
            _session = _harvest_cookies()
    return _session


def _invalidate_session() -> None:
    """Force cookie re-harvest on next call (e.g. after a 403 with cookies)."""
    global _session
    with _session_lock:
        _session = None


def _get(url: str) -> requests.Response:
    """GET via cookie session (fast path). Falls back to Playwright intercept on 403."""
    session = _get_session()
    resp    = session.get(url, timeout=30)
    if resp.status_code != 403:
        return resp

    # Fast-path cookies no longer valid â€” try Playwright intercept (slow path)
    print(f"  [cookies] Session cookies rejected (403) â€” trying Playwright intercept for {url}")
    _invalidate_session()
    result: dict = {}
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page()

            def _intercept(response):
                if "api/int/studies" in response.url and response.status == 200:
                    try:
                        result["data"] = response.json()
                    except Exception:
                        pass

            page.on("response", _intercept)
            study_url = url.replace(BASE_URL + "/", "https://clinicaltrials.gov/study/")
            study_url = re.sub(r"/history.*$", "?tab=history", study_url)
            page.goto(study_url, wait_until="networkidle", timeout=30000)

            # Re-harvest cookies for future requests
            new_cookies = page.context.cookies()
            browser.close()

        new_session = requests.Session()
        new_session.headers.update(HEADERS)
        for c in new_cookies:
            new_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
        with _session_lock:
            global _session
            _session = new_session

    except Exception as exc:
        print(f"  [cookies] Playwright intercept failed: {exc}")

    if "data" not in result:
        # Return a fake 403 response so callers can raise_for_status as before
        resp.status_code = 403
        return resp

    # Wrap intercepted JSON in a fake successful response
    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return result["data"]

    return _FakeResp()


def get_history_list(nct_id: str) -> list[dict]:
    url  = f"{BASE_URL}/{nct_id}/history"
    resp = _get(url)
    resp.raise_for_status()
    return resp.json().get("changes", [])


def get_version_data(nct_id: str, version: int) -> dict:
    url  = f"{BASE_URL}/{nct_id}/history/{version}"
    resp = _get(url)
    resp.raise_for_status()
    return resp.json()


# â”€â”€ Diffing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _decode(value: any) -> str:
    if value is None:
        return "(none)"
    return html.unescape(str(value))


def _natural_sort_key(s: str) -> list:
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]


def _humanize_field(field: str) -> str:
    field = re.sub(r'^\w+Module\.', '', field)
    field = re.sub(r'\[(\d+)\]', lambda m: f' #{int(m.group(1)) + 1}', field)
    field = field.replace(".", " - ")
    field = re.sub(r'([a-z])([A-Z])', r'\1 \2', field)
    field = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', field)
    return field.strip().title()


def _fmt(old: any, new: any) -> str:
    if old is None:
        return f"Added: {_decode(new)}"
    if new is None:
        return f"Removed: {_decode(old)}"
    return f"Old: {_decode(old)}  |  New: {_decode(new)}"


def _rich(text: str):
    """Return a CellRichText object with bold Old:/New: labels, or plain str as fallback."""
    if not _RICH_TEXT or not text:
        return text
    bold = _InlineFont(b=True)
    parts = []
    for seg in re.split(r'(Old: |New: )', text):
        if seg in ("Old: ", "New: "):
            parts.append(TextBlock(bold, seg))
        elif seg:
            parts.append(seg)
    return CellRichText(*parts) if parts else text


def _flatten(obj: any, prefix: str = "") -> dict[str, any]:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            out.update(_flatten(item, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _extract_module(version_data: dict, section: str, module_key: str) -> dict:
    return version_data.get("study", {}).get(section, {}).get(module_key, {})


def _compare_locations(old_module: dict, new_module: dict) -> list[dict]:
    def extract_by_facility(module: dict) -> dict[str, dict]:
        flat = _flatten(module)
        by_index: dict[int, dict] = {}
        for key, val in flat.items():
            m = re.match(r'locations\[(\d+)\]\.(.+)', key)
            if m:
                idx, subfield = int(m.group(1)), m.group(2)
                by_index.setdefault(idx, {})[subfield] = val
        keyed: dict[str, dict] = {}
        for idx, loc in by_index.items():
            key = loc.get("facility") or loc.get("city") or f"location_{idx}"
            keyed[key] = loc
        return keyed

    old_locs = extract_by_facility(old_module)
    new_locs = extract_by_facility(new_module)
    changes: list[dict] = []

    for facility in sorted(set(new_locs) - set(old_locs)):
        loc = new_locs[facility]
        location_str = ", ".join(filter(None, [facility, loc.get("city"), loc.get("country")]))
        changes.append({"module": "Contacts/Locations", "field": "__added__",
                        "oldValue": None, "newValue": location_str})

    for facility in sorted(set(old_locs) - set(new_locs)):
        loc = old_locs[facility]
        location_str = ", ".join(filter(None, [facility, loc.get("city"), loc.get("country")]))
        changes.append({"module": "Contacts/Locations", "field": "__removed__",
                        "oldValue": location_str, "newValue": None})

    for facility in sorted(set(old_locs) & set(new_locs)):
        old_loc, new_loc = old_locs[facility], new_locs[facility]
        for subfield in sorted(set(old_loc) | set(new_loc)):
            if subfield in ("geoPoint.lat", "geoPoint.lon"):
                continue
            if old_loc.get(subfield) != new_loc.get(subfield):
                changes.append({
                    "module":   "Contacts/Locations",
                    "field":    f"location.{subfield} [{facility}]",
                    "oldValue": old_loc.get(subfield),
                    "newValue": new_loc.get(subfield),
                })

    return changes


def compare_versions(old_data: dict, new_data: dict, module_labels: list[str]) -> list[dict]:
    changes = []
    for label in module_labels:
        path = MODULE_LABEL_MAP.get(label)
        if path is None:
            continue
        section, module_key = path
        old_module = _extract_module(old_data, section, module_key)
        new_module = _extract_module(new_data, section, module_key)
        if label == "Contacts/Locations":
            changes.extend(_compare_locations(old_module, new_module))
        else:
            old_flat = _flatten(old_module)
            new_flat = _flatten(new_module)
            for field in sorted(set(old_flat) | set(new_flat), key=_natural_sort_key):
                old_val = old_flat.get(field)
                new_val = new_flat.get(field)
                if old_val != new_val:
                    changes.append({"module": label, "field": field,
                                    "oldValue": old_val, "newValue": new_val})
    return changes


_RESULTS_MODULES = {
    "Participant Flow", "Baseline Characteristics",
    "Outcome Measures (Results)", "Adverse Events",
}


def format_field_changes(changes: list[dict]) -> str:
    by_module: dict[str, list] = defaultdict(list)
    for c in changes:
        by_module[c["module"]].append(c)

    sections = []
    for module, items in by_module.items():
        lines = [f"[{module}]"]

        if module == "Contacts/Locations":
            added   = [c for c in items if c["field"] == "__added__"]
            removed = [c for c in items if c["field"] == "__removed__"]
            changed = [c for c in items if c["field"] not in ("__added__", "__removed__")]
            if added:
                lines.append(f"  Added ({len(added)}):")
                for c in added:
                    lines.append(f"    + {_decode(c['newValue'])}")
            if removed:
                lines.append(f"  Removed ({len(removed)}):")
                for c in removed:
                    lines.append(f"    - {_decode(c['oldValue'])}")
            if changed:
                lines.append(f"  Updated details ({len(changed)}):")
                for c in changed:
                    lines.append(f"    {_humanize_field(c['field'])}: {_fmt(c['oldValue'], c['newValue'])}")

        elif module in _RESULTS_MODULES:
            n_added   = sum(1 for c in items if c["oldValue"] is None)
            n_changed = sum(1 for c in items if c["oldValue"] is not None and c["newValue"] is not None)
            n_removed = sum(1 for c in items if c["newValue"] is None)
            parts = []
            if n_added:   parts.append(f"{n_added} data points added")
            if n_changed: parts.append(f"{n_changed} values updated")
            if n_removed: parts.append(f"{n_removed} data points removed")
            lines.append(f"  {'; '.join(parts)}")

        elif module == "Outcome Measures":
            outcome_groups: dict[tuple, list] = defaultdict(list)
            other_items = []
            for c in items:
                m = re.match(r'(primary|secondary)Outcomes\[(\d+)\]\.(.+)', c["field"], re.IGNORECASE)
                if m:
                    kind = "Primary Outcome" if m.group(1).lower() == "primary" else "Secondary Outcome"
                    idx  = int(m.group(2)) + 1
                    outcome_groups[(kind, idx)].append((m.group(3), c))
                else:
                    other_items.append(c)

            for (kind, idx), sub_items in sorted(outcome_groups.items(),
                    key=lambda item: (0 if item[0][0] == "Primary Outcome" else 1, item[0][1])):
                lines.append(f"  {kind} #{idx}:")
                for sub, c in sub_items:
                    lines.append(f"    {_humanize_field(sub)}: {_fmt(c['oldValue'], c['newValue'])}")
            for c in other_items:
                lines.append(f"  {_humanize_field(c['field'])}: {_fmt(c['oldValue'], c['newValue'])}")

        else:
            for c in sorted(items, key=lambda c: _natural_sort_key(c["field"])):
                lines.append(f"  {_humanize_field(c['field'])}: {_fmt(c['oldValue'], c['newValue'])}")

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# â”€â”€ Core â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _blank_row(nct_id: str, note: str, total_versions: int = 0) -> dict:
    return {
        "nct_id":             nct_id,
        "note":               note,
        "total_versions":     total_versions,
        "from_version":       "",
        "to_version":         "",
        "from_date":          "",
        "to_date":            "",
        "status":             "",
        "modules_changed":    "",
        "field_change_count": "",
        "field_changes":      "",
        "curr_full_data":     None,
    }


def get_all_comparisons(nct_id: str) -> list[dict]:
    """
    Fetch history + all version data for one NCT, compare every consecutive pair.
    Always returns at least one row. The `note` field explains any issues.

    note values:
      ""                     -- comparison completed normally
      "NO_CHANGES_IN_VERSION"-- this version pair had no module changes recorded
      "NO_HISTORY"           -- API returned 0 versions
      "SINGLE_VERSION"       -- only 1 version, nothing to compare
      "NCT_NOT_FOUND"        -- HTTP 404
      "INVALID_NCT_ID"       -- HTTP 400 (malformed ID)
      "ERROR: <msg>"         -- any other failure
    """
    try:
        changes = get_history_list(nct_id)
    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 404:
            print(f"  [{nct_id}] Not found (404).")
            return [_blank_row(nct_id, "NCT_NOT_FOUND")]
        if code == 400:
            print(f"  [{nct_id}] Invalid NCT ID format (400).")
            return [_blank_row(nct_id, "INVALID_NCT_ID")]
        if code == 403:
            # CT.gov internal API returns 403 for trials whose history is not yet
            # available (typically brand-new trials with only 1 version). Distinguish
            # from a real transient block by checking the public API.
            pub = requests.get(
                f"https://clinicaltrials.gov/api/v2/studies/{nct_id}",
                headers=HEADERS, timeout=10,
            )
            if pub.status_code == 200:
                # Trial exists and is public â€” history just isn't available yet (1 version).
                print(f"  [{nct_id}] History restricted (403) but trial is public â€” saving SINGLE_VERSION sentinel.")
                row = _blank_row(nct_id, "SINGLE_VERSION", total_versions=1)
                return [row]
            # Genuine transient block (trial itself is also inaccessible) â€” retry next run.
            print(f"  [{nct_id}] HTTP 403 (transient) â€” will retry next run.")
            return []
        if code in (429, 503):
            # Rate-limited or server overload â€” retry next run.
            print(f"  [{nct_id}] HTTP {code} (transient) â€” will retry next run.")
            return []
        print(f"  [{nct_id}] HTTP {code}.")
        return [_blank_row(nct_id, f"ERROR: HTTP {code}")]
    except Exception as e:
        print(f"  [{nct_id}] ERROR: {e}")
        return [_blank_row(nct_id, f"ERROR: {e}")]

    if len(changes) == 0:
        print(f"  [{nct_id}] No version history.")
        return [_blank_row(nct_id, "NO_HISTORY")]

    if len(changes) == 1:
        c = changes[0]
        print(f"  [{nct_id}] Only 1 version (v{c['version']}, {c['date']}).")
        row = _blank_row(nct_id, "SINGLE_VERSION", total_versions=1)
        row["to_version"] = c["version"]
        row["to_date"]    = c["date"]
        row["status"]     = c["status"]
        return [row]

    total = len(changes)
    print(f"  [{nct_id}] {total} versions â€” fetching all and comparing {total - 1} pairs...")

    rows: list[dict] = []

    time.sleep(0.8)  # pause after history list before first version fetch
    try:
        prev_data = get_version_data(nct_id, changes[0]["version"])
    except Exception as e:
        print(f"  [{nct_id}] ERROR fetching v{changes[0]['version']}: {e}")
        return [_blank_row(nct_id, f"ERROR: {e}", total_versions=total)]

    prev_meta = changes[0]
    for c in changes[1:]:
        v = c["version"]
        print(f"  [{nct_id}] v{v}/{total - 1}...", end="\r")
        time.sleep(0.8)  # avoid rate-limiting on rapid version fetches
        try:
            curr_data     = get_version_data(nct_id, v)
            module_labels = c.get("moduleLabels", [])

            if not module_labels:
                row = _blank_row(nct_id, "NO_CHANGES_IN_VERSION", total_versions=total)
                row["from_version"]   = prev_meta["version"]
                row["to_version"]     = v
                row["from_date"]      = prev_meta["date"]
                row["to_date"]        = c["date"]
                row["status"]         = c["status"]
                row["curr_full_data"] = curr_data
            else:
                field_changes = compare_versions(prev_data, curr_data, module_labels)
                row = {
                    "nct_id":             nct_id,
                    "note":               "",
                    "total_versions":     total,
                    "from_version":       prev_meta["version"],
                    "to_version":         v,
                    "from_date":          prev_meta["date"],
                    "to_date":            c["date"],
                    "status":             c["status"],
                    "modules_changed":    "; ".join(module_labels),
                    "field_change_count": len(field_changes),
                    "field_changes":      format_field_changes(field_changes),
                    "curr_full_data":     curr_data,
                }

            rows.append(row)
            prev_data = curr_data
            prev_meta = c

        except Exception as e:
            print(f"\n  [{nct_id}] ERROR at v{v}: {e}")
            rows.append(_blank_row(nct_id, f"ERROR: {e}", total_versions=total))

    print(f"  [{nct_id}] Done â€” {len(rows)} version pairs.")
    return rows


# â”€â”€ Excel writer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _save_excel(nct_data: dict[str, list[dict]], path: Path) -> None:
    # Collect all unique (from_version, to_version) pairs across all NCTs
    seen_pairs: list[tuple] = []
    seen_set: set[tuple] = set()
    for pairs in nct_data.values():
        for p in pairs:
            fv = p.get("from_version", "")
            tv = p.get("to_version", "")
            if fv not in ("", None) and tv not in ("", None):
                key = (int(fv), int(tv))
                if key not in seen_set:
                    seen_set.add(key)
                    seen_pairs.append(key)
    seen_pairs.sort()

    dyn_cols = [(f"Field Changes (v{fv}->v{tv})", 80) for fv, tv in seen_pairs]
    pair_col_idx = {pair: len(_FIXED_COLS) + i + 1 for i, pair in enumerate(seen_pairs)}
    all_cols = _FIXED_COLS + dyn_cols

    wb = Workbook()
    ws = wb.active
    ws.title = "All Version Comparisons"

    header_fill  = PatternFill("solid", fgColor="1F4E79")
    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, (label, width) in enumerate(all_cols, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = _BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    for row_idx, (nct_id, pairs) in enumerate(nct_data.items(), start=2):
        first = pairs[0] if pairs else {}
        first_note = first.get("note", "")
        row_note = (
            first_note
            if first_note in ("NCT_NOT_FOUND", "INVALID_NCT_ID", "NO_HISTORY", "SINGLE_VERSION")
               or first_note.startswith("ERROR")
            else ""
        )
        total_versions = first.get("total_versions", 0)

        # Build stacked summary columns (one line per version pair)
        from_date_lines: list[str] = []
        to_date_lines:   list[str] = []
        status_lines:    list[str] = []
        modules_lines:   list[str] = []
        count_lines:     list[str] = []

        for p in pairs:
            fv    = p.get("from_version", "")
            tv    = p.get("to_version", "")
            pnote = p.get("note", "")
            if pnote in ("NCT_NOT_FOUND", "INVALID_NCT_ID", "NO_HISTORY", "SINGLE_VERSION") \
                    or pnote.startswith("ERROR"):
                continue
            vlabel = f"v{fv}->v{tv}: " if fv not in ("", None) and tv not in ("", None) else ""
            from_date_lines.append(f"{vlabel}{p.get('from_date', '')}")
            to_date_lines.append(f"{vlabel}{p.get('to_date', '')}")
            status_lines.append(f"{vlabel}{p.get('status', '')}")
            if pnote == "NO_CHANGES_IN_VERSION":
                modules_lines.append(f"{vlabel}(no module changes)")
                count_lines.append(f"{vlabel}0")
            else:
                modules_lines.append(f"{vlabel}{p.get('modules_changed', '')}")
                count_lines.append(f"{vlabel}{p.get('field_change_count', '')}")

        fixed_values = [
            nct_id,
            row_note,
            total_versions,
            "\n".join(from_date_lines),
            "\n".join(to_date_lines),
            "\n".join(status_lines),
            "\n".join(modules_lines),
            "\n".join(count_lines),
        ]

        note_fill = _NOTE_FILLS.get(row_note)
        row_fill  = PatternFill("solid", fgColor="F0F4FA") if row_idx % 2 == 0 else None

        for col_idx, value in enumerate(fixed_values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = _BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=(col_idx >= 4))
            if col_idx == 2 and note_fill:
                cell.fill = note_fill
            elif row_fill and col_idx != 2:
                cell.fill = row_fill

        # Dynamic Field Changes columns -- one column per version pair
        for p in pairs:
            fv = p.get("from_version", "")
            tv = p.get("to_version", "")
            if fv in ("", None) or tv in ("", None):
                continue
            key = (int(fv), int(tv))
            if key not in pair_col_idx:
                continue
            fc = p.get("field_changes", "") or ""
            if not fc:
                continue
            col_idx = pair_col_idx[key]
            cell = ws.cell(row=row_idx, column=col_idx, value=_rich(fc))
            cell.border    = _BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if row_fill:
                cell.fill = row_fill

        max_lines = max((len(str(v).splitlines()) for v in fixed_values[3:]), default=1)
        ws.row_dimensions[row_idx].height = min(max(max_lines * 15, 20), 600)

    wb.save(path)


# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def compare_all_versions(
    nct_ids: str | list[str],
    out_dir: str = "output",
    filename: str = "all_versions_comparison.xlsx",
) -> Path:
    """
    For each NCT ID, compare every consecutive version pair.
    Writes ONE row per NCT into an Excel workbook with:
      - Fixed stacked columns (From Date, To Date, Status, Modules Changed, Field Change Count)
        showing all version pairs, one line each.
      - Dynamic "Field Changes (vX->vY)" columns, one per consecutive pair found
        across all NCTs in the batch.

    Args:
        nct_ids:  single NCT ID string, or a list of them.
        out_dir:  output directory.
        filename: output Excel filename.

    Returns the path to the written .xlsx file.
    """
    from collections import Counter

    if isinstance(nct_ids, str):
        nct_ids = [nct_ids]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename

    total = len(nct_ids)
    nct_data: dict[str, list[dict]] = {}
    for i, nct_id in enumerate(nct_ids, 1):
        print(f"[{i}/{total}] {nct_id}")
        nct_data[nct_id] = get_all_comparisons(nct_id)
        if i < total:
            time.sleep(0.3)

    _save_excel(nct_data, path)

    all_notes = Counter(p["note"] for pairs in nct_data.values() for p in pairs)
    ok_count  = all_notes.pop("", 0)
    print(f"\nDone. {len(nct_data)} NCT(s) written -> {path}")
    print(f"  OK version pairs               : {ok_count}")
    for note, count in sorted(all_notes.items()):
        print(f"  {note:<30}: {count}")

    return path


# â”€â”€ Utilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def load_nct_ids_from_excel(path: str, column: str = "Trial Identifier") -> list[str]:
    """Read NCT IDs from a column in an Excel file. Skips blank cells."""
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    header = None
    col_idx = None
    nct_ids = []
    for row in ws.iter_rows(values_only=True):
        if header is None:
            header = list(row)
            col_idx = header.index(column)
            continue
        val = row[col_idx] if col_idx < len(row) else None
        if val:
            nct_ids.append(str(val).strip())
    wb.close()
    print(f"Loaded {len(nct_ids)} NCT IDs from {path}")
    return nct_ids


# â”€â”€ Run â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if __name__ == "__main__":
    _input = Path(__file__).parent / "ADC_All_NCTIDs-14May2026.xlsx"
    _out   = str(Path(__file__).parent / "output")

    nct_ids = load_nct_ids_from_excel(str(_input))
    compare_all_versions(
        nct_ids,
        out_dir=_out,
        filename="all_versions_comparison.xlsx",
    )
