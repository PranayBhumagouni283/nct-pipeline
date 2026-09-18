from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
import psycopg2, psycopg2.extras, os

conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute('SET search_path TO "CT"')
cur.execute('SELECT dept, indication FROM dept_indications ORDER BY dept, indication')
for r in cur.fetchall():
    print(f"{r['dept']} | {r['indication']}")
conn.close()
