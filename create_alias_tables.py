"""
create_alias_tables.py
-----------------------
Creates normalization alias tables in the DB and seeds them with known variants.

Safe to re-run — uses INSERT ... ON CONFLICT DO NOTHING so existing aliases
are never overwritten.

Tables created in schema "CT":
  org_aliases — organization/company name variants → canonical

Notes:
  - Drug normalization    → dept_keywords (drug_name / alias_names)
  - Condition normalization → dept_indications (indication / keywords)
  No separate drug_aliases or condition_aliases tables are needed.

To add more aliases later, either:
  - Edit the SEED_* lists below and re-run this script.
  - Use manage_aliases.py for individual additions.
  - Import an Excel/CSV: python manage_aliases.py import org org_aliases.xlsx

Usage:
    python create_alias_tables.py
"""

import psycopg2.extras
import db

# ── DDL ───────────────────────────────────────────────────────────────────────

_CREATE = """
CREATE TABLE IF NOT EXISTS org_aliases (
    alias      TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# ── Seed data — (alias, canonical) ───────────────────────────────────────────
# These are the "raw strings from CT.gov" → "preferred display name" mappings.
# norm.py also auto-strips legal suffixes, so these handle trickier cases
# (abbreviations, wholly different names, swapped word order, etc.).

SEED_ORGS: list[tuple[str, str]] = [
    # ── Merck ──────────────────────────────────────────────────────────────
    ("Merck Sharp & Dohme LLC",            "Merck Sharp & Dohme"),
    ("Merck Sharp & Dohme Corp.",          "Merck Sharp & Dohme"),
    ("Merck Sharp & Dohme (I.A.) LLC",     "Merck Sharp & Dohme"),
    ("MSD",                                "Merck Sharp & Dohme"),
    ("MSD K.K.",                           "Merck Sharp & Dohme"),
    ("Merck & Co., Inc.",                  "Merck Sharp & Dohme"),
    ("Merck & Co.",                        "Merck Sharp & Dohme"),
    ("Merck Sharp and Dohme LLC",          "Merck Sharp & Dohme"),
    # ── AstraZeneca ────────────────────────────────────────────────────────
    ("AstraZeneca AB",                     "AstraZeneca"),
    ("AstraZeneca PLC",                    "AstraZeneca"),
    ("AstraZeneca LP",                     "AstraZeneca"),
    ("AstraZeneca UK Limited",             "AstraZeneca"),
    # ── Pfizer ─────────────────────────────────────────────────────────────
    ("Pfizer Inc.",                        "Pfizer"),
    ("Pfizer Inc",                         "Pfizer"),
    ("Pfizer Innovative Research",         "Pfizer"),
    # ── Roche / Genentech ──────────────────────────────────────────────────
    ("F. Hoffmann-La Roche Ltd",           "F. Hoffmann-La Roche"),
    ("F. Hoffmann-La Roche AG",            "F. Hoffmann-La Roche"),
    ("Hoffmann-La Roche",                  "F. Hoffmann-La Roche"),
    ("Roche",                              "F. Hoffmann-La Roche"),
    ("Genentech, Inc.",                    "Genentech"),
    ("Genentech Inc.",                     "Genentech"),
    # ── Novartis ───────────────────────────────────────────────────────────
    ("Novartis Pharmaceuticals Corporation", "Novartis"),
    ("Novartis Pharma AG",                 "Novartis"),
    ("Novartis AG",                        "Novartis"),
    ("Novartis Pharma GmbH",               "Novartis"),
    # ── AbbVie ─────────────────────────────────────────────────────────────
    ("AbbVie Inc.",                        "AbbVie"),
    ("AbbVie Inc",                         "AbbVie"),
    # ── Bristol-Myers Squibb ───────────────────────────────────────────────
    ("Bristol Myers Squibb",               "Bristol-Myers Squibb"),
    ("Bristol-Myers Squibb Company",       "Bristol-Myers Squibb"),
    ("BMS",                                "Bristol-Myers Squibb"),
    # ── Daiichi Sankyo ─────────────────────────────────────────────────────
    ("Daiichi Sankyo, Inc.",               "Daiichi Sankyo"),
    ("Daiichi Sankyo Inc.",                "Daiichi Sankyo"),
    ("Daiichi Sankyo Co., Ltd.",           "Daiichi Sankyo"),
    ("Daiichi Sankyo UK Ltd",              "Daiichi Sankyo"),
    ("Daiichi Sankyo Europe GmbH",         "Daiichi Sankyo"),
    # ── Seagen (acquired by Pfizer 2023) ───────────────────────────────────
    ("Seagen Inc.",                        "Seagen"),
    ("Seattle Genetics, Inc.",             "Seagen"),
    ("Seattle Genetics Inc.",              "Seagen"),
    ("Seattle Genetics",                   "Seagen"),
    # ── Sobi ───────────────────────────────────────────────────────────────
    ("Sobi, Inc.",                         "Sobi"),
    ("Swedish Orphan Biovitrum AB (publ)", "Sobi"),
    ("Swedish Orphan Biovitrum AB",        "Sobi"),
    ("SOBI",                               "Sobi"),
    # ── Gilead / Immunomedics ──────────────────────────────────────────────
    ("Gilead Sciences, Inc.",              "Gilead Sciences"),
    ("Immunomedics, Inc.",                 "Immunomedics"),
    # ── ImmunoGen ──────────────────────────────────────────────────────────
    ("ImmunoGen, Inc.",                    "ImmunoGen"),
    # ── ADC Therapeutics ───────────────────────────────────────────────────
    ("ADC Therapeutics SA",               "ADC Therapeutics"),
    ("ADC Therapeutics America, Inc.",    "ADC Therapeutics"),
    # ── Mersana ────────────────────────────────────────────────────────────
    ("Mersana Therapeutics, Inc.",         "Mersana Therapeutics"),
    # ── Sutro ──────────────────────────────────────────────────────────────
    ("Sutro Biopharma, Inc.",              "Sutro Biopharma"),
    # ── Heidelberg Pharma ──────────────────────────────────────────────────
    ("Heidelberg Pharma AG",               "Heidelberg Pharma"),
    # ── Synaffix ───────────────────────────────────────────────────────────
    ("Synaffix B.V.",                      "Synaffix"),
    # ── Regeneron ──────────────────────────────────────────────────────────
    ("Regeneron Pharmaceuticals, Inc.",    "Regeneron Pharmaceuticals"),
    ("Regeneron Pharmaceuticals Inc.",     "Regeneron Pharmaceuticals"),
    # ── Sanofi ─────────────────────────────────────────────────────────────
    ("Sanofi S.A.",                        "Sanofi"),
    ("sanofi-aventis",                     "Sanofi"),
    ("Sanofi-Aventis",                     "Sanofi"),
    ("Sanofi Genzyme",                     "Sanofi"),
    # ── Johnson & Johnson / Janssen ────────────────────────────────────────
    ("Janssen Research & Development, LLC", "Janssen"),
    ("Janssen Research & Development LLC",  "Janssen"),
    ("Janssen Pharmaceutica",               "Janssen"),
    ("Janssen-Cilag",                       "Janssen"),
    # ── Eisai ──────────────────────────────────────────────────────────────
    ("Eisai Inc.",                         "Eisai"),
    ("Eisai Co., Ltd.",                    "Eisai"),
    ("Eisai Limited",                      "Eisai"),
    # ── Takeda ─────────────────────────────────────────────────────────────
    ("Takeda Pharmaceutical Company Limited", "Takeda"),
    ("Takeda Oncology",                    "Takeda"),
    ("Millennium Pharmaceuticals, Inc.",   "Takeda"),
    # ── GSK ────────────────────────────────────────────────────────────────
    ("GlaxoSmithKline",                    "GSK"),
    ("GlaxoSmithKline LLC",                "GSK"),
    # ── Exelixis ───────────────────────────────────────────────────────────
    ("Exelixis, Inc.",                     "Exelixis"),
    # ── Bicycle Therapeutics ───────────────────────────────────────────────
    ("Bicycle Therapeutics Ltd",           "Bicycle Therapeutics"),
    # ── Byondis ────────────────────────────────────────────────────────────
    ("Byondis B.V.",                       "Byondis"),
    # ── MacroGenics ────────────────────────────────────────────────────────
    ("MacroGenics, Inc.",                  "MacroGenics"),
    # ── Immunovia / SciSparc / misc ────────────────────────────────────────
    ("Agios Pharmaceuticals, Inc.",        "Agios Pharmaceuticals"),
    ("Blueprint Medicines Corporation",    "Blueprint Medicines"),
    ("Merus N.V.",                         "Merus"),
    ("Zymeworks Inc.",                     "Zymeworks"),
    ("Zymeworks BC Inc.",                  "Zymeworks"),
]


def _seed(conn, table: str, rows: list[tuple[str, str]], label: str) -> None:
    if not rows:
        return
    sql = (
        f'INSERT INTO "{table}" (alias, canonical) VALUES %s '
        "ON CONFLICT (alias) DO NOTHING"
    )
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows)
    print(f"  {table}: {len(rows)} {label} aliases seeded (ON CONFLICT DO NOTHING)")


def main():
    print("=" * 60)
    print("Create & Seed Normalization Alias Tables")
    print("=" * 60)

    conn = db._db()

    # Create tables
    with conn.cursor() as cur:
        cur.execute(_CREATE)
    print("  Tables created / verified.")

    # Seed
    _seed(conn, "org_aliases", SEED_ORGS, "org")

    print("\nDone.")
    print("Next: run backfill_normalization.py to apply to all existing rows.")
    print("=" * 60)


if __name__ == "__main__":
    import psycopg2.extras
    main()
