"""Seed the sandbox's data dependencies into your local Supabase.

Runs inside the backend image (same Python, same requirements, same app
code as the Azure deployment) via `./sandbox.sh seed`. Every step skips work
that is already done, so it is safe to re-run and never overwrites data:

  1. Reference PDFs  -> public "pdfs" bucket   (GET /api/pdf/{doc} serves these)
  2. Built-in checks -> compliance_checks      (scripts/seed_compliance_checks.py)
  3. Scheduling manual chunks -> chunks        (scripts/ingest_specs.py), only
     if that collection is empty. --all ingests every collection instead.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path("/home/site/wwwroot")
sys.path.insert(0, str(APP_ROOT))

from app.database import get_db  # noqa: E402

PY = str(APP_ROOT / "antenv" / "bin" / "python")


def seed_pdfs(db) -> None:
    raw = APP_ROOT / "data" / "raw_pdfs"
    existing = {o["name"] for o in db.storage.from_("pdfs").list("", {"limit": 1000})}
    todo = [p for p in sorted(raw.glob("*.pdf")) if p.name not in existing]
    print(f"[pdfs] {len(existing)} already in bucket, uploading {len(todo)}")
    for p in todo:
        db.storage.from_("pdfs").upload(
            p.name, p.read_bytes(), {"content-type": "application/pdf", "upsert": "false"}
        )
        print(f"[pdfs]   + {p.name}")


def seed_checks(db) -> None:
    rows = db.table("compliance_checks").select("id").is_("user_id", "null").limit(1).execute().data
    if rows:
        print("[checks] built-in checks present, skipping")
        return
    print("[checks] no built-ins found, seeding")
    subprocess.run([PY, "scripts/seed_compliance_checks.py"], cwd=APP_ROOT, check=True)


def seed_chunks(db, ingest_all: bool) -> None:
    collections = ["scheduling", "material_procs", "specs_2019"] if ingest_all else ["scheduling"]
    for coll in collections:
        rows = db.table("chunks").select("id").eq("collection", coll).limit(1).execute().data
        if rows:
            print(f"[chunks] {coll}: already ingested, skipping")
            continue
        print(f"[chunks] {coll}: empty, ingesting (embeddings come from the configured OPENAI_BASE_URL)")
        subprocess.run([PY, "scripts/ingest_specs.py", "--collection", coll], cwd=APP_ROOT, check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ingest every collection, not just scheduling")
    args = ap.parse_args()
    db = get_db()
    seed_pdfs(db)
    seed_checks(db)
    seed_chunks(db, args.all)
    print("[seed] done")


if __name__ == "__main__":
    main()
