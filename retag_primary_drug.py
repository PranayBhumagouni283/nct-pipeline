"""
retag_primary_drug.py
---------------------
Step 2: Re-tag Primary Drug for every ADC organized_trial.

Reads fresh keywords from dept_keywords (after replace_keywords.py),
re-runs the same matching logic the pipeline uses, and bulk-updates
the "Primary Drug" column in organized_trials.
"""

import db
from combined_pipeline import tag_all_organized_drug

DEPT = "ADC"

# ── Load keywords from DB (same logic as load_drug_keywords in combined_pipeline) ──
print(f"Loading updated ADC keywords from DB...")
drugs: list[tuple[str, list[str]]] = []
for row in db.load_dept_keywords(DEPT):
    name = str(row.get("drug_name", "") or "").strip()
    if not name or name in ("nan", "-"):
        continue
    alias_raw = str(row.get("alias_names", "") or "").strip()
    aliases: list[str] = []
    if alias_raw and alias_raw not in ("-", "nan", "—", ""):
        for a in alias_raw.split("|"):
            a = a.strip()
            if a and a not in ("-", "—", ""):
                aliases.append(a)
    drugs.append((name, aliases))

print(f"Loaded {len(drugs)} drug keywords\n")

# ── Re-tag ────────────────────────────────────────────────────────────────────
print("Re-tagging Primary Drug for all ADC organized_trials...")
tag_all_organized_drug(DEPT, drugs)

print("\nStep 2 complete.")
