import os
import psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Single clean connection test — run this ONCE after waiting for IP block to clear
try:
    conn = psycopg2.connect(
        os.environ["DATABASE_URL"],
        sslmode="require",
        connect_timeout=15,
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('SET search_path TO "CT"')
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'CT'
        ORDER BY table_name
    """)
    tables = cur.fetchall()
    print(f"SUCCESS — {len(tables)} tables in CT schema:")
    for t in tables:
        print(f"  {t[0]}")
    conn.close()
except Exception as e:
    print(f"FAIL: {e}")
