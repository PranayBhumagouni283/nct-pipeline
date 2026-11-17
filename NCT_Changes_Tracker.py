import html
import json
import re
import time
import requests
from threading import Lock
try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
except ImportError:
    _scraper = None
from collections import defaultdict
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

CSV_COLUMNS = [
    "nct_id",
    "note",               # empty = OK; otherwise explains why comparison is missing
    "total_versions",
    "prev_version",
    "curr_version",
    "prev_date",
    "curr_date",
    "curr_status",
    "modules_changed",
    "field_change_count",
    "field_changes",
]


# ── Cookie session (CT.gov api/int requires JS-generated cookies) ──────────────

_session: requests.Session | None = None
_session_lock = Lock()


def _harvest_cookies() -> requests.Session:
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
    print(f"  [cookies] Harvested {len(names)} cookie(s): {names}")
    return session


def _get_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = _harvest_cookies()
    return _session


def _invalidate_session() -> None:
    global _session
    with _session_lock:
        _session = None


# ── API ────────────────────────────────────────────────────────────────────────

def _get(url: str) -> requests.Response:
    session = _get_session()
    resp    = session.get(url, timeout=30)
    if resp.status_code != 403:
        return resp
    print(f"  [cookies] Session cookies rejected (403) — trying Playwright intercept for {url}")
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
            study_url = re.sub(r"api/int/studies/", "https://clinicaltrials.gov/study/", url)
            study_url = re.sub(r"/history.*$", "?tab=history", study_url)
            page.goto(study_url, wait_until="networkidle", timeout=30000)
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
        return resp
    class _FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return result["data"]
    return _FakeResp()


def get_history_list(nct_id: str) -> list[dict]:
    url = f"{BASE_URL}/{nct_id}/history"
    resp = _get(url)
    resp.raise_for_status()
    return resp.json().get("changes", [])


def get_version_data(nct_id: str, version: int) -> dict:
    url = f"{BASE_URL}/{nct_id}/history/{version}"
    resp = _get(url)
    resp.raise_for_status()
    return resp.json()


# ── Diffing ────────────────────────────────────────────────────────────────────

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


def _decode(value: any) -> str:
    if value is None:
        return "(none)"
    return html.unescape(str(value))


def _natural_sort_key(s: str) -> list:
    """Sort key that orders embedded numbers numerically: [2] before [10]."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]


def _humanize_field(field: str) -> str:
    field = re.sub(r'^\w+Module\.', '', field)
    field = re.sub(r'\[(\d+)\]', lambda m: f' #{int(m.group(1)) + 1}', field)
    field = field.replace(".", " - ")
    field = re.sub(r'([a-z])([A-Z])', r'\1 \2', field)
    field = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', field)
    return field.strip().title()


def _fmt(old: any, new: any) -> str:
    """Format one field change. Distinguishes added, removed, and modified."""
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


def _compare_locations(old_module: dict, new_module: dict) -> list[dict]:
    """
    Smart location diff: match locations by facility name, not array index.

    Array-index diffing causes hundreds of phantom changes whenever a location
    is inserted or removed (every subsequent index shifts). Matching by facility
    name shows only real additions, removals, and detail changes.
    """
    def extract_by_facility(module: dict) -> dict[str, dict]:
        flat = _flatten(module)
        by_index: dict[int, dict] = {}
        for key, val in flat.items():
            m = re.match(r'locations\[(\d+)\]\.(.+)', key)
            if m:
                idx, subfield = int(m.group(1)), m.group(2)
                by_index.setdefault(idx, {})[subfield] = val
        # Key each location by its facility name (fallback: city or index)
        keyed: dict[str, dict] = {}
        for idx, loc in by_index.items():
            key = loc.get("facility") or loc.get("city") or f"location_{idx}"
            keyed[key] = loc
        return keyed

    old_locs = extract_by_facility(old_module)
    new_locs = extract_by_facility(new_module)
    changes: list[dict] = []

    added   = sorted(set(new_locs) - set(old_locs))
    removed = sorted(set(old_locs) - set(new_locs))
    common  = sorted(set(old_locs) & set(new_locs))

    for facility in added:
        loc = new_locs[facility]
        location_str = ", ".join(filter(None, [facility, loc.get("city"), loc.get("country")]))
        changes.append({"module": "Contacts/Locations", "field": "__added__",
                         "oldValue": None, "newValue": location_str})

    for facility in removed:
        loc = old_locs[facility]
        location_str = ", ".join(filter(None, [facility, loc.get("city"), loc.get("country")]))
        changes.append({"module": "Contacts/Locations", "field": "__removed__",
                         "oldValue": location_str, "newValue": None})

    for facility in common:
        old_loc = old_locs[facility]
        new_loc = new_locs[facility]
        for subfield in sorted(set(old_loc) | set(new_loc)):
            if subfield in ("geoPoint.lat", "geoPoint.lon"):
                continue   # skip coordinate noise
            if old_loc.get(subfield) != new_loc.get(subfield):
                changes.append({
                    "module":   "Contacts/Locations",
                    "field":    f"location.{subfield} [{facility}]",
                    "oldValue": old_loc.get(subfield),
                    "newValue": new_loc.get(subfield),
                })

    return changes


def compare_versions(
    old_data: dict, new_data: dict, module_labels: list[str]
) -> list[dict]:
    """Returns list of {module, field, oldValue, newValue} for every changed field."""
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
                    changes.append({
                        "module":   label,
                        "field":    field,
                        "oldValue": old_val,
                        "newValue": new_val,
                    })
    return changes


# Modules whose data is too granular to show field-by-field — show a count summary instead
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

        # ── Contacts/Locations: smart added/removed/changed ──
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

        # ── Results sections: summary count only ──
        elif module in _RESULTS_MODULES:
            n_added   = sum(1 for c in items if c["oldValue"] is None)
            n_changed = sum(1 for c in items if c["oldValue"] is not None and c["newValue"] is not None)
            n_removed = sum(1 for c in items if c["newValue"] is None)
            parts = []
            if n_added:   parts.append(f"{n_added} data points added")
            if n_changed: parts.append(f"{n_changed} values updated")
            if n_removed: parts.append(f"{n_removed} data points removed")
            lines.append(f"  {'; '.join(parts)}")

        # ── Outcome Measures: group by outcome number ──
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

        # ── All other modules: flat sorted list ──
        else:
            for c in sorted(items, key=lambda c: _natural_sort_key(c["field"])):
                lines.append(f"  {_humanize_field(c['field'])}: {_fmt(c['oldValue'], c['newValue'])}")

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ── Core ───────────────────────────────────────────────────────────────────────

def _blank_row(nct_id: str, note: str, total_versions: int = 0) -> dict:
    """Skeleton row used whenever a full comparison cannot be produced."""
    return {
        "nct_id":             nct_id,
        "note":               note,
        "total_versions":     total_versions,
        "prev_version":       "",
        "curr_version":       "",
        "prev_date":          "",
        "curr_date":          "",
        "curr_status":        "",
        "modules_changed":    "",
        "field_change_count": "",
        "field_changes":      "",
    }


def get_latest_comparison(nct_id: str) -> dict:
    """
    Fetch the history list, then compare the last two versions.
    Always returns a dict — never raises. The `note` field explains any issue.

    note values:
      ""                    — comparison completed normally
      "NO_HISTORY"          — API returned 0 versions
      "SINGLE_VERSION"      — only 1 version exists, nothing to compare
      "NO_CHANGES_IN_LATEST"— latest version has no module changes recorded
      "NCT_NOT_FOUND"       — HTTP 404 (valid ID format, but NCT does not exist)
      "INVALID_NCT_ID"      — HTTP 400 (malformed ID, e.g. NCT00000000)
      "ERROR: <msg>"        — any other failure
    """
    try:
        changes = get_history_list(nct_id)
    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 404:
            print(f"  [{nct_id}] Not found (404).")
            return _blank_row(nct_id, "NCT_NOT_FOUND")
        if code == 400:
            print(f"  [{nct_id}] Invalid NCT ID format (400).")
            return _blank_row(nct_id, "INVALID_NCT_ID")
        print(f"  [{nct_id}] HTTP {code}.")
        return _blank_row(nct_id, f"ERROR: HTTP {code}")
    except Exception as e:
        print(f"  [{nct_id}] ERROR: {e}")
        return _blank_row(nct_id, f"ERROR: {e}")

    if len(changes) == 0:
        print(f"  [{nct_id}] No version history available.")
        return _blank_row(nct_id, "NO_HISTORY")

    if len(changes) == 1:
        c = changes[0]
        print(f"  [{nct_id}] Only 1 version (v{c['version']}, {c['date']}), no comparison possible.")
        row = _blank_row(nct_id, "SINGLE_VERSION", total_versions=1)
        row["curr_version"] = c["version"]
        row["curr_date"]    = c["date"]
        row["curr_status"]  = c["status"]
        return row

    prev_meta = changes[-2]
    curr_meta = changes[-1]
    module_labels = curr_meta.get("moduleLabels", [])

    if not module_labels:
        print(f"  [{nct_id}] v{curr_meta['version']} has no module changes recorded.")
        row = _blank_row(nct_id, "NO_CHANGES_IN_LATEST", total_versions=len(changes))
        row["prev_version"] = prev_meta["version"]
        row["curr_version"] = curr_meta["version"]
        row["prev_date"]    = prev_meta["date"]
        row["curr_date"]    = curr_meta["date"]
        row["curr_status"]  = curr_meta["status"]
        return row

    print(f"  [{nct_id}] Comparing v{prev_meta['version']} -> v{curr_meta['version']}...")

    try:
        prev_data = get_version_data(nct_id, prev_meta["version"])
        curr_data = get_version_data(nct_id, curr_meta["version"])
    except Exception as e:
        print(f"  [{nct_id}] ERROR fetching version data: {e}")
        return _blank_row(nct_id, f"ERROR: {e}", total_versions=len(changes))

    field_changes = compare_versions(prev_data, curr_data, module_labels)

    return {
        "nct_id":             nct_id,
        "note":               "",
        "total_versions":     len(changes),
        "prev_version":       prev_meta["version"],
        "curr_version":       curr_meta["version"],
        "prev_date":          prev_meta["date"],
        "curr_date":          curr_meta["date"],
        "curr_status":        curr_meta["status"],
        "modules_changed":    "; ".join(module_labels),
        "field_change_count": len(field_changes),
        "field_changes":      format_field_changes(field_changes),
        "curr_full_data":     curr_data,
    }


# ── Excel writer ───────────────────────────────────────────────────────────────

# Column config: (header label, column width)
COLUMN_CONFIG = [
    ("NCT ID",             16),
    ("Note",               24),
    ("Total Versions",     14),
    ("Prev Version",       13),
    ("Curr Version",       13),
    ("Prev Date",          13),
    ("Curr Date",          13),
    ("Current Status",     22),
    ("Modules Changed",    40),
    ("Field Change Count", 17),
    ("Field Changes",      80),
]

# Note cell fill colours
_NOTE_FILLS = {
    "NCT_NOT_FOUND":        PatternFill("solid", fgColor="FFD7D7"),   # light red
    "INVALID_NCT_ID":       PatternFill("solid", fgColor="FFD7D7"),
    "NO_HISTORY":           PatternFill("solid", fgColor="FFF2CC"),   # light yellow
    "SINGLE_VERSION":       PatternFill("solid", fgColor="FFF2CC"),
    "NO_CHANGES_IN_LATEST": PatternFill("solid", fgColor="E2EFDA"),   # light green
}
_THIN = Side(style="thin", color="CCCCCC")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _write_sheet(ws, rows: list[dict]) -> None:
    header_fill  = PatternFill("solid", fgColor="1F4E79")
    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, (label, width) in enumerate(COLUMN_CONFIG, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = _BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    for row_idx, r in enumerate(rows, start=2):
        values = [
            r["nct_id"],
            r["note"],
            r["total_versions"],
            r["prev_version"],
            r["curr_version"],
            r["prev_date"],
            r["curr_date"],
            r["curr_status"],
            r["modules_changed"],
            r["field_change_count"],
            _rich(r["field_changes"]),
        ]
        note      = r["note"]
        note_fill = _NOTE_FILLS.get(note)

        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = _BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=(col_idx == 11))
            if col_idx == 2 and note_fill:
                cell.fill = note_fill

        if note == "" and row_idx % 2 == 0:
            row_fill = PatternFill("solid", fgColor="F5F9FF")
            for col_idx in range(1, len(COLUMN_CONFIG) + 1):
                if col_idx != 2:
                    ws.cell(row=row_idx, column=col_idx).fill = row_fill

        line_count = len(str(r["field_changes"]).splitlines())
        ws.row_dimensions[row_idx].height = min(max(line_count * 15, 20), 400)


def _write_redirects_sheet(
    ws,
    redirects: list[dict],
    rows: list[dict],
    newly_compared: list[dict],
) -> None:
    headers = [
        "Requested NCT ID", "Canonical NCT ID", "Canonical In Input File",
        "Action Required", "Change Status",
        "Current Version", "Curr Date", "Current Status",
    ]
    widths = [18, 18, 22, 50, 25, 15, 15, 22]

    header_fill  = PatternFill("solid", fgColor="1F4E79")
    header_font  = Font(bold=True, color="FFFFFF", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, (hdr, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=col_idx, value=hdr)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = _BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    if not redirects:
        msg_cell = ws.cell(row=2, column=1, value="No redirects detected this run — all input NCT IDs returned their expected canonical IDs.")
        msg_cell.font      = Font(italic=True, color="888888")
        msg_cell.alignment = Alignment(vertical="top")
        return

    newly_ids = {r["nct_id"] for r in newly_compared}
    all_rows  = {r["nct_id"]: r for r in rows}

    _GREEN  = PatternFill("solid", fgColor="E2EFDA")
    _YELLOW = PatternFill("solid", fgColor="FFF2CC")
    _RED    = PatternFill("solid", fgColor="FFD7D7")

    for row_idx, r in enumerate(redirects, start=2):
        requested = r["requested"]
        canonical = r["canonical"]
        in_input  = r["in_input"]

        action = (
            f"Remove {requested} from input (canonical already tracked)"
            if in_input else
            f"Replace {requested} with {canonical} in input file"
        )

        if canonical in newly_ids:
            change_status = "Newly Updated This Run"
            status_fill   = _GREEN
        elif canonical in all_rows:
            change_status = "Existing — No New Version This Run"
            status_fill   = _YELLOW
        else:
            change_status = "Not Tracked in Input"
            status_fill   = _RED

        canon_row = all_rows.get(canonical, {})
        values = [
            requested,
            canonical,
            "Yes" if in_input else "No",
            action,
            change_status,
            canon_row.get("curr_version", ""),
            canon_row.get("curr_date", ""),
            canon_row.get("curr_status", ""),
        ]

        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = _BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if col_idx == 5:
                cell.fill = status_fill


def _save_excel(
    rows: list[dict],
    newly_compared: list[dict],
    path: Path,
    redirects: list[dict] | None = None,
) -> None:
    wb = Workbook()

    # Sheet 1 — all NCTs (full snapshot, updated + carried forward)
    ws_all = wb.active
    ws_all.title = "All NCTs"
    _write_sheet(ws_all, rows)

    # Sheet 2 — only NCTs that got a NEW version detected in this run
    ws_changed = wb.create_sheet(title="New Updates This Run")
    _write_sheet(ws_changed, newly_compared)

    # Sheet 3 — redirected NCTs (always present; empty message when none detected)
    ws_redir = wb.create_sheet(title="Redirected NCTs")
    _write_redirects_sheet(ws_redir, redirects or [], rows, newly_compared)

    wb.save(path)


# ── Main ───────────────────────────────────────────────────────────────────────

def compare_latest_versions(
    nct_ids: str | list[str],
    out_dir: str = "output",
    filename: str = "latest_comparison.xlsx",
) -> Path:
    """
    For each NCT ID, compare only the last 2 versions and write one row per NCT
    into an Excel workbook.

    Args:
        nct_ids:   single NCT ID string, or a list of them.
        out_dir:   output directory.
        filename:  output Excel filename.

    Returns the path to the written .xlsx file.
    """
    from collections import Counter

    if isinstance(nct_ids, str):
        nct_ids = [nct_ids]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename

    total = len(nct_ids)

    # Load previous run's output so we can skip NCTs whose version hasn't changed
    prev_curr_version: dict[str, int] = {}
    prev_rows: dict[str, dict] = {}
    if path.exists():
        try:
            wb_prev = load_workbook(path, read_only=True, data_only=True)
            ws_prev = wb_prev.active
            headers = None
            for row in ws_prev.iter_rows(values_only=True):
                if headers is None:
                    headers = list(row)
                    continue
                r = dict(zip(headers, row))
                nct = str(r.get("NCT ID", "") or "")
                ver = r.get("Curr Version")
                if nct and ver is not None:
                    try:
                        prev_curr_version[nct] = int(ver)
                    except (ValueError, TypeError):
                        pass
                    prev_rows[nct] = {
                        "nct_id":             nct,
                        "note":               str(r.get("Note", "") or ""),
                        "total_versions":     r.get("Total Versions", ""),
                        "prev_version":       r.get("Prev Version", ""),
                        "curr_version":       r.get("Curr Version", ""),
                        "prev_date":          r.get("Prev Date", ""),
                        "curr_date":          r.get("Curr Date", ""),
                        "curr_status":        r.get("Current Status", ""),
                        "modules_changed":    str(r.get("Modules Changed", "") or ""),
                        "field_change_count": r.get("Field Change Count", ""),
                        "field_changes":      str(r.get("Field Changes", "") or ""),
                    }
            wb_prev.close()
            print(f"Loaded previous run: {len(prev_curr_version)} NCT(s) cached.")
        except Exception as e:
            print(f"Could not load previous run ({e}), running fresh.")

    # Load pre-computed version map written by update_pipeline.py.
    # This avoids calling the history API twice for every NCT on the same run.
    _detection_cache = Path(__file__).parent / "_nct_detection_cache.json"
    precomputed: dict[str, int] = {}
    if _detection_cache.exists():
        try:
            with open(_detection_cache, encoding="utf-8") as f:
                precomputed = {k: int(v) for k, v in json.load(f).items()}
            _detection_cache.unlink(missing_ok=True)
            print(f"Loaded detection cache ({len(precomputed)} NCTs) — skipping duplicate API calls.")
        except Exception as e:
            print(f"Could not load detection cache ({e}), will poll API for all NCTs.")

    # Load redirect info written by update_pipeline.py (if any redirects were detected).
    _redirects_cache = Path(__file__).parent / "_nct_redirects.json"
    redirects: list[dict] = []
    if _redirects_cache.exists():
        try:
            with open(_redirects_cache, encoding="utf-8") as f:
                redirects = json.load(f)
            _redirects_cache.unlink(missing_ok=True)
            print(f"Loaded {len(redirects)} redirect(s) — will add 'Redirected NCTs' sheet.")
        except Exception as e:
            print(f"Could not load redirects ({e}).")

    rows: list[dict] = []
    newly_compared: list[dict] = []  # only NCTs with a new version detected this run
    skipped = 0

    for i, nct_id in enumerate(nct_ids, 1):
        # Use pre-computed version from update_pipeline if available; else call API
        if nct_id in precomputed:
            api_latest = precomputed[nct_id]
        else:
            try:
                changes = get_history_list(nct_id)
                api_latest = changes[-1]["version"] if changes else None
            except Exception:
                api_latest = None

        if api_latest is not None and nct_id in prev_curr_version:
            if api_latest == prev_curr_version[nct_id]:
                # Same version as last run — carry forward, no API calls needed
                rows.append(prev_rows[nct_id])
                skipped += 1
                print(f"[{i}/{total}] {nct_id} — v{api_latest} unchanged, carried forward")
                if nct_id not in precomputed:
                    time.sleep(0.1)  # only sleep when we actually made an API call
                continue

        # New version (or first run) — do the full comparison
        print(f"[{i}/{total}] {nct_id} — new version detected, comparing...")
        row = get_latest_comparison(nct_id)
        rows.append(row)
        newly_compared.append(row)
        if i < total:
            time.sleep(0.3)

    _save_excel(rows, newly_compared, path, redirects=redirects or None)

    notes = Counter(r["note"] for r in newly_compared)
    ok_count = notes.pop("", 0)
    print(f"\nDone. {len(rows)} NCT(s) written -> {path}")
    print(f"  Carried forward (no new version) : {skipped}")
    print(f"  New versions compared            : {len(newly_compared)}")
    print(f"    of which had field changes     : {ok_count}")
    for note, count in sorted(notes.items()):
        print(f"    {note:<32}: {count}")

    return path


# ── Utilities ──────────────────────────────────────────────────────────────────

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


# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _input = Path(__file__).parent / "ADC_All_NCTIDs-14May2026.xlsx"
    _out   = str(Path(__file__).parent / "output")

    nct_ids = load_nct_ids_from_excel(str(_input))
    compare_latest_versions(
        nct_ids,
        out_dir=_out,
        filename="latest_comparison.xlsx",
    )
