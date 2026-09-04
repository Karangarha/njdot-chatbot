"""Deploy SQL function files to Supabase via direct Postgres connection.

Reads DATABASE_URL from environment (or .env file) and applies every .sql
file in the ``sql/`` directory.

Setup
-----
Add this to your .env file:

    DATABASE_URL=postgresql://postgres.[project-ref]:[db-password]@aws-0-us-east-1.pooler.supabase.com:6543/postgres

The DB password is the one you set when creating your Supabase project.
It is available in:  Supabase dashboard → Project Settings → Database → Connection string

Usage
-----
    python scripts/deploy_sql.py                    # apply all sql/*.sql files
    python scripts/deploy_sql.py --dry-run          # print SQL without running
"""

from __future__ import annotations

import argparse
import sys
import os
from pathlib import Path

# ── Ensure backend/ is on sys.path ───────────────────────────────────────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from dotenv import load_dotenv
load_dotenv()


def main(dry_run: bool = False) -> None:
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url and not dry_run:
        print(
            "❌  DATABASE_URL not set.\n"
            "    Add it to your .env file:\n\n"
            "    DATABASE_URL=postgresql://postgres.[ref]:[password]@"
            "aws-0-us-east-1.pooler.supabase.com:6543/postgres\n\n"
            "    (Connection string → Supabase dashboard → Project Settings "
            "→ Database → URI)\n\n"
            "    Alternatively, paste the contents of sql/match_chunks.sql\n"
            "    and sql/keyword_search_chunks.sql directly into the\n"
            "    Supabase SQL editor."
        )
        sys.exit(1)

    sql_dir  = _BACKEND / "sql"
    sql_files = sorted(sql_dir.glob("*.sql"))
    if not sql_files:
        print("⚠️  No .sql files found in sql/")
        return

    if dry_run:
        for f in sql_files:
            print(f"\n{'═' * 60}")
            print(f"── {f.name}")
            print('═' * 60)
            print(f.read_text())
        return

    try:
        import psycopg2
    except ImportError:
        print("❌  psycopg2 not installed.  Run:  pip install psycopg2-binary")
        sys.exit(1)

    try:
        conn = psycopg2.connect(db_url, connect_timeout=15)
        conn.autocommit = True
        print(f"✅  Connected to Postgres")
    except Exception as e:
        print(f"❌  Connection failed: {e}")
        sys.exit(1)

    cur = conn.cursor()
    for f in sql_files:
        sql = f.read_text()
        print(f"\n── Deploying {f.name} …", end=" ", flush=True)
        try:
            cur.execute(sql)
            print("✅")
        except Exception as e:
            print(f"\n   ❌  {e}")

    cur.close()
    conn.close()
    print("\n✅  All SQL files deployed.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print SQL without executing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
