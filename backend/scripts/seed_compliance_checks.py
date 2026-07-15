"""seed_compliance_checks.py — Seed the built-in compliance checklist into Supabase.

Reads the canonical catalog from ``app.compliance.catalog.BUILTIN_CHECKS`` and
(re)writes it into the ``compliance_checks`` table as global rows
(``user_id IS NULL``, ``is_builtin = TRUE``).  These are the shared template the
frontend lists and users fork/customise (copy-on-write).

Idempotent: it deletes the existing built-in rows and re-inserts the current
catalog, so renamed/removed built-ins stay in sync.  User-owned rows
(``user_id`` set) are never touched.  Runs with the service-role client, which
bypasses RLS.

Usage
-----
    python scripts/seed_compliance_checks.py [--dry-run]

Run scripts/migrate_compliance_checks.sql first to create the table.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.catalog import BUILTIN_CHECKS  # noqa: E402
from app.database import get_db  # noqa: E402

TABLE = "compliance_checks"


def _rows() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "user_id": None,
            "check_key": c.check_key,
            "category": c.category,
            "name": c.name,
            "instruction": c.instruction,
            "check_type": c.check_type,
            "source_files": c.source_files,
            "is_builtin": True,
            "enabled": True,
            "sort_order": i,
            "created_at": now,
            "updated_at": now,
        }
        for i, c in enumerate(BUILTIN_CHECKS)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed built-in compliance checks.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without touching the DB.")
    args = parser.parse_args()

    rows = _rows()
    print(f"-- {len(rows)} built-in checks in catalog")

    if args.dry_run:
        for r in rows:
            print(f"   [{r['sort_order']:>2}] {r['category']:<40} {r['check_key']}")
        print("-- dry run: no changes written")
        return

    db = get_db()
    # Refresh the global built-in set (user rows are left untouched).
    db.table(TABLE).delete().is_("user_id", "null").execute()
    db.table(TABLE).insert(rows).execute()
    print(f"OK seeded {len(rows)} built-in checks into '{TABLE}' (user_id IS NULL)")


if __name__ == "__main__":
    main()
