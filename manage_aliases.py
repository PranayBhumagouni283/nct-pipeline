"""
manage_aliases.py
-----------------
CLI tool to add, update, list, or remove normalization aliases.

Usage:
    # List all org aliases
    python manage_aliases.py list org

    # List all drug aliases
    python manage_aliases.py list drug

    # Add a single org alias
    python manage_aliases.py add org "Pfizer Inc." "Pfizer"

    # Import from an Excel file (.xlsx) — two columns: alias | canonical
    python manage_aliases.py import org org_aliases.xlsx

    # Import from a CSV file — same column layout
    python manage_aliases.py import org orgs.csv

    # Note:
    #   Drug normalization   → managed via dept_keywords (drug_name / alias_names)
    #   Condition normalization → managed via dept_indications (indication / keywords)

    # Remove an alias
    python manage_aliases.py remove org "Pfizer Inc."

    # Search aliases (partial match on alias or canonical)
    python manage_aliases.py search org "merck"
"""

import sys
import csv
import db

TABLES = {
    "org": "org_aliases",
    # drug normalization    → dept_keywords (drug_name / alias_names)
    # condition normalization → dept_indications (indication / keywords)
}


def _table(type_arg: str) -> str:
    t = TABLES.get(type_arg.lower())
    if not t:
        print(f"Unknown type '{type_arg}'. Choose from: org, drug, condition")
        sys.exit(1)
    return t


def cmd_list(type_arg: str) -> None:
    table = _table(type_arg)
    conn = db._db()
    with conn.cursor() as cur:
        cur.execute(f'SELECT alias, canonical FROM "{table}" ORDER BY canonical, alias')
        rows = cur.fetchall()
    if not rows:
        print(f"No aliases in {table}.")
        return
    col_w = max((len(r["alias"]) for r in rows), default=40)
    print(f"\n{'ALIAS':<{col_w}}  CANONICAL")
    print("-" * (col_w + 30))
    for r in rows:
        print(f"{r['alias']:<{col_w}}  {r['canonical']}")
    print(f"\nTotal: {len(rows)}")


def cmd_search(type_arg: str, term: str) -> None:
    table = _table(type_arg)
    conn = db._db()
    term_lower = f"%{term.lower()}%"
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT alias, canonical FROM "{table}" '
            "WHERE LOWER(alias) LIKE %s OR LOWER(canonical) LIKE %s "
            "ORDER BY canonical, alias",
            (term_lower, term_lower),
        )
        rows = cur.fetchall()
    if not rows:
        print(f"No matches for '{term}' in {table}.")
        return
    col_w = max((len(r["alias"]) for r in rows), default=40)
    print(f"\n{'ALIAS':<{col_w}}  CANONICAL")
    print("-" * (col_w + 30))
    for r in rows:
        print(f"{r['alias']:<{col_w}}  {r['canonical']}")
    print(f"\nTotal: {len(rows)}")


def cmd_add(type_arg: str, alias: str, canonical: str) -> None:
    table = _table(type_arg)
    conn = db._db()
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{table}" (alias, canonical) VALUES (%s, %s) '
            "ON CONFLICT (alias) DO UPDATE SET canonical = EXCLUDED.canonical",
            (alias, canonical),
        )
    print(f"  Added/updated: '{alias}' → '{canonical}' in {table}")


def cmd_remove(type_arg: str, alias: str) -> None:
    table = _table(type_arg)
    conn = db._db()
    with conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{table}" WHERE alias = %s', (alias,))
        deleted = cur.rowcount
    if deleted:
        print(f"  Removed '{alias}' from {table}")
    else:
        print(f"  '{alias}' not found in {table}")


def _read_file(file_path: str) -> list[tuple[str, str]]:
    """Read alias/canonical pairs from .xlsx or .csv. Returns [(alias, canonical), ...]."""
    ext = file_path.lower().rsplit('.', 1)[-1]

    if ext in ('xlsx', 'xls', 'xlsm'):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        # Detect header row
        header = [str(c).strip().lower() if c is not None else '' for c in next(rows_iter, [])]
        if 'alias' not in header or 'canonical' not in header:
            print(f"Excel must have 'alias' and 'canonical' column headers. Found: {header}")
            sys.exit(1)
        ai = header.index('alias')
        ci = header.index('canonical')
        pairs: list[tuple[str, str]] = []
        for row in rows_iter:
            alias     = str(row[ai]).strip() if row[ai] is not None else ''
            canonical = str(row[ci]).strip() if row[ci] is not None else ''
            if alias and canonical and alias.lower() != 'none' and canonical.lower() != 'none':
                pairs.append((alias, canonical))
        wb.close()
        return pairs

    # Default: CSV
    pairs = []
    with open(file_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or 'alias' not in reader.fieldnames or 'canonical' not in reader.fieldnames:
            print("CSV must have 'alias' and 'canonical' column headers.")
            sys.exit(1)
        for row in reader:
            alias     = (row.get('alias') or '').strip()
            canonical = (row.get('canonical') or '').strip()
            if alias and canonical:
                pairs.append((alias, canonical))
    return pairs


def cmd_import(type_arg: str, file_path: str) -> None:
    table = _table(type_arg)
    rows  = _read_file(file_path)
    if not rows:
        print("No valid rows found in file.")
        return
    import psycopg2.extras
    conn = db._db()
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f'INSERT INTO "{table}" (alias, canonical) VALUES %s '
            "ON CONFLICT (alias) DO UPDATE SET canonical = EXCLUDED.canonical",
            rows,
        )
    print(f"  Imported/updated {len(rows)} aliases into {table}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0].lower()

    if cmd == "list" and len(args) >= 2:
        cmd_list(args[1])
    elif cmd == "search" and len(args) >= 3:
        cmd_search(args[1], args[2])
    elif cmd == "add" and len(args) >= 4:
        cmd_add(args[1], args[2], args[3])
    elif cmd == "remove" and len(args) >= 3:
        cmd_remove(args[1], args[2])
    elif cmd == "import" and len(args) >= 3:
        cmd_import(args[1], args[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
