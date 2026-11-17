"""
change_parser.py — Parse field_changes TEXT from field_changes_log into structured events.

Input format (produced by NCT_Changes_Tracker.format_field_changes):

    [Module Name]
      Field Name: Old: <old_value>  |  New: <new_value>
      Field Name: Added: <new_value>
      Field Name: Removed: <old_value>

    [Contacts/Locations]
      Added (3):
        + Contact Name (role)
      Removed (1):
        - Old Contact Name
      Updated details (2):
        Email: Old: old@example.com  |  New: new@example.com

    [Outcome Measures]
      Primary Outcome #1:
        Measure: Old: Old Measure  |  New: New Measure
      Secondary Outcome #2:
        Description: Added: New text

    [Participant Flow]
      3 data points added; 2 values updated

Public API:
    parse(text: str) -> list[dict]

Each returned dict:
    {
        "module":      str,   # e.g. "Study Status"
        "field_name":  str,   # e.g. "Overall Status"
        "change_type": str,   # "MODIFIED" | "ADDED" | "REMOVED" | "SUMMARY"
        "old_value":   str | None,
        "new_value":   str | None,
    }
"""

from __future__ import annotations
import re
from typing import Optional

# Modules whose content is summarised (e.g. "3 data points added")
_SUMMARY_MODULES = {
    "Participant Flow",
    "Baseline Characteristics",
    "Outcome Measures (Results)",
    "Adverse Events",
}

# Regex: module header  →  [Module Name]
_RE_MODULE   = re.compile(r"^\[(.+)\]\s*$")
# Regex: outcome index  →  "Primary Outcome #1:" or "Secondary Outcome #2:"
_RE_OUTCOME  = re.compile(r"^(Primary|Secondary) Outcome #(\d+):\s*$", re.IGNORECASE)
# Regex: change line    →  "Field: Old: X  |  New: Y"
_RE_MODIFIED = re.compile(r"^(.+?):\s+Old:\s+(.*?)\s+\|\s+New:\s+(.*?)\s*$")
# Regex: added line     →  "Field: Added: X"
_RE_ADDED    = re.compile(r"^(.+?):\s+Added:\s+(.*?)\s*$")
# Regex: removed line   →  "Field: Removed: X"
_RE_REMOVED  = re.compile(r"^(.+?):\s+Removed:\s+(.*?)\s*$")
# Regex: contact added  →  "+  Name (role)"
_RE_CON_ADD  = re.compile(r"^\+\s+(.+)$")
# Regex: contact removed → "-  Name"
_RE_CON_REM  = re.compile(r"^-\s+(.+)$")


def _parse_standard_line(line: str, module: str) -> Optional[dict]:
    """Try to parse a single indented line as MODIFIED / ADDED / REMOVED."""
    m = _RE_MODIFIED.match(line)
    if m:
        return {
            "module":      module,
            "field_name":  m.group(1).strip(),
            "change_type": "MODIFIED",
            "old_value":   m.group(2).strip() or None,
            "new_value":   m.group(3).strip() or None,
        }
    m = _RE_ADDED.match(line)
    if m:
        return {
            "module":      module,
            "field_name":  m.group(1).strip(),
            "change_type": "ADDED",
            "old_value":   None,
            "new_value":   m.group(2).strip() or None,
        }
    m = _RE_REMOVED.match(line)
    if m:
        return {
            "module":      module,
            "field_name":  m.group(1).strip(),
            "change_type": "REMOVED",
            "old_value":   m.group(2).strip() or None,
            "new_value":   None,
        }
    return None


def _parse_contacts_block(lines: list[str], module: str) -> list[dict]:
    """Parse a Contacts/Locations section with Added/Removed/Updated sub-headers."""
    events: list[dict] = []
    current_op: Optional[str] = None   # "ADDED" | "REMOVED" | "MODIFIED"

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if re.match(r"^Added\s*\(\d+\):", stripped, re.IGNORECASE):
            current_op = "ADDED"
            continue
        if re.match(r"^Removed\s*\(\d+\):", stripped, re.IGNORECASE):
            current_op = "REMOVED"
            continue
        if re.match(r"^Updated details\s*\(\d+\):", stripped, re.IGNORECASE):
            current_op = "MODIFIED"
            continue

        if current_op in ("ADDED", "REMOVED"):
            m = _RE_CON_ADD.match(stripped) or _RE_CON_REM.match(stripped)
            if m:
                name = m.group(1).strip()
                events.append({
                    "module":      module,
                    "field_name":  "Contact",
                    "change_type": current_op,
                    "old_value":   name if current_op == "REMOVED" else None,
                    "new_value":   name if current_op == "ADDED"   else None,
                })
            continue

        if current_op == "MODIFIED":
            ev = _parse_standard_line(stripped, module)
            if ev:
                ev["field_name"] = f"Contact.{ev['field_name']}"
                events.append(ev)

    return events


def _parse_outcomes_block(lines: list[str], module: str) -> list[dict]:
    """Parse an Outcome Measures section with Primary/Secondary index headers."""
    events:  list[dict]       = []
    context: Optional[str]    = None  # e.g. "Primary #1"

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        m = _RE_OUTCOME.match(stripped)
        if m:
            context = f"{m.group(1).capitalize()} #{m.group(2)}"
            continue

        ev = _parse_standard_line(stripped, module)
        if ev:
            if context:
                ev["field_name"] = f"{context}.{ev['field_name']}"
            events.append(ev)

    return events


def parse(text: str) -> list[dict]:
    """
    Parse the full field_changes TEXT string into a list of structured event dicts.
    Returns [] for empty or whitespace-only input.
    """
    if not text or not text.strip():
        return []

    events:       list[dict]       = []
    current_mod:  Optional[str]    = None
    current_lines: list[str]       = []

    def _flush(module: str, lines: list[str]) -> None:
        if not module:
            return

        if module in _SUMMARY_MODULES:
            summary = " ".join(l.strip() for l in lines if l.strip())
            if summary:
                events.append({
                    "module":      module,
                    "field_name":  "_summary",
                    "change_type": "SUMMARY",
                    "old_value":   None,
                    "new_value":   summary,
                })
            return

        if module == "Contacts/Locations":
            events.extend(_parse_contacts_block(lines, module))
            return

        if module == "Outcome Measures":
            events.extend(_parse_outcomes_block(lines, module))
            return

        # Standard module: parse each indented line
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            ev = _parse_standard_line(stripped, module)
            if ev:
                events.append(ev)

    for raw_line in text.splitlines():
        m = _RE_MODULE.match(raw_line.strip())
        if m:
            _flush(current_mod, current_lines)
            current_mod   = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(raw_line)

    _flush(current_mod, current_lines)
    return events


# ── Convenience: extract specific signal fields ────────────────────────────────

def extract_status_change(events: list[dict]) -> Optional[tuple[str, str]]:
    """Return (old_status, new_status) if a status change event exists, else None."""
    for ev in events:
        if (
            ev["module"] in ("Study Status", "Status")
            and "status" in ev["field_name"].lower()
            and ev["change_type"] == "MODIFIED"
        ):
            return ev["old_value"], ev["new_value"]
    return None


def extract_phase_change(events: list[dict]) -> Optional[tuple[str, str]]:
    """Return (old_phase, new_phase) if a phase change event exists, else None."""
    for ev in events:
        if (
            ev["module"] in ("Study Design", "Design")
            and "phase" in ev["field_name"].lower()
            and ev["change_type"] == "MODIFIED"
        ):
            return ev["old_value"], ev["new_value"]
    return None


_DATE_FIELD_MAP = {
    "completion date":         "completion_date",
    "primary completion date": "primary_completion_date",
    "start date":              "start_date",
    "study start date":        "start_date",
}

def extract_date_changes(events: list[dict]) -> list[tuple[str, str, str]]:
    """Return list of (date_field_key, old_date, new_date) for any date field changes."""
    results = []
    for ev in events:
        if ev["change_type"] != "MODIFIED":
            continue
        key = ev["field_name"].lower().strip()
        canonical = _DATE_FIELD_MAP.get(key)
        if canonical:
            results.append((canonical, ev["old_value"], ev["new_value"]))
    return results


if __name__ == "__main__":
    # Quick smoke test
    sample = """
[Study Status]
  Overall Status: Old: RECRUITING  |  New: ACTIVE_NOT_RECRUITING

[Study Design]
  Phase: Old: PHASE2  |  New: PHASE3

[Description]
  Brief Summary: Old: Phase 2 study of...  |  New: Phase 3 study of...

[Contacts/Locations]
  Added (2):
    + Dr. Jane Smith (Principal Investigator)
    + Site Boston (site)
  Removed (1):
    - Dr. Old Name (site)
  Updated details (1):
    Email: Old: old@site.com  |  New: new@site.com

[Outcome Measures]
  Primary Outcome #1:
    Measure: Old: PFS  |  New: OS
    Time Frame: Old: 12 weeks  |  New: 24 weeks
  Secondary Outcome #1:
    Description: Added: New secondary endpoint text

[Participant Flow]
  5 data points added; 3 values updated
"""
    evts = parse(sample)
    for e in evts:
        print(e)
    print(f"\nTotal: {len(evts)} events")
    print("Status change:", extract_status_change(evts))
    print("Phase change:", extract_phase_change(evts))
    print("Date changes:", extract_date_changes(evts))
