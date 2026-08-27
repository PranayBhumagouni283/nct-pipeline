"""
Backfill countries and sites columns for all existing organized_trials
by re-parsing the stored Locations text field.

Run once after adding the columns:
    python backfill_location_fields.py
"""

import os
import re
import sys
import psycopg2
import psycopg2.extras
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Connection ────────────────────────────────────────────────────────────────
def _conn():
    c = psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")
    c.autocommit = True
    c.cursor().execute('SET search_path TO "CT"')
    return c


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_countries(locations_text: str) -> str:
    """Extract country names from lines like '1) Australia:' """
    if not locations_text:
        return ""
    countries: list[str] = []
    for line in locations_text.splitlines():
        # Match numbered country lines: "1) Country Name:"
        m = re.match(r'^\d+\)\s+(.+?):\s*$', line)
        if m:
            country = m.group(1).strip()
            if country and country not in countries:
                countries.append(country)
    return " | ".join(sorted(countries))


def parse_sites(locations_text: str) -> str:
    """
    Extract facility names from 7-space-indented lines that don't contain commas.
    Strips /ID# suffixes from facility names.
    """
    if not locations_text:
        return ""
    sites: list[str] = []
    for line in locations_text.splitlines():
        # Must be indented with exactly 7 spaces (facility lines)
        if not line.startswith("       "):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        # Address lines always have commas (city, state, country, zip)
        if "," in stripped:
            continue
        # Strip /ID# suffix and leading "1. " numbering
        site = re.sub(r'\s*/ID#\s*\S+', '', stripped).strip()
        site = re.sub(r'^\d+\.\s+', '', site).strip()
        # Skip pure numeric site codes (e.g. Chinese trials storing "015", "016")
        if not site or not re.search(r'[A-Za-z]', site):
            continue
        if site not in sites:
            sites.append(site)
    return " | ".join(sites)


# ── Backfill ──────────────────────────────────────────────────────────────────

def run():
    conn = _conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("Loading organized_trials...")
    cur.execute('SELECT nct_id, "Locations" FROM organized_trials')
    rows = cur.fetchall()
    print(f"  {len(rows)} trials to process")

    updates: list[tuple[str, str, str]] = []
    for row in rows:
        nct_id   = row["nct_id"]
        loc_text = row["Locations"] or ""
        countries = parse_countries(loc_text)
        sites     = parse_sites(loc_text)
        updates.append((countries, sites, nct_id))

    print(f"Updating {len(updates)} rows...")
    batch_size = 500
    for i in range(0, len(updates), batch_size):
        batch = updates[i : i + batch_size]
        cur.executemany(
            'UPDATE organized_trials SET countries = %s, sites = %s WHERE nct_id = %s',
            batch,
        )
        print(f"  {min(i + batch_size, len(updates))} / {len(updates)}")
    print("Done.")

    # Verify
    cur.execute("SELECT COUNT(*) AS n FROM organized_trials WHERE countries != ''")
    filled = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM organized_trials")
    total  = cur.fetchone()["n"]
    print(f"  countries filled: {filled} / {total}")

    cur.execute("SELECT COUNT(*) AS n FROM organized_trials WHERE sites != ''")
    filled_s = cur.fetchone()["n"]
    print(f"  sites filled:     {filled_s} / {total}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
