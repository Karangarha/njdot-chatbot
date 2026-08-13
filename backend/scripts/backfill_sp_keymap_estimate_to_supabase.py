"""backend/scripts/backfill_sp_keymap_estimate_to_supabase.py

One-time backfill: copies Special Provision / Key Map / Estimate chunks and
extraction JSON that still only exist in Neo4j (SPChunk/KeyMapChunk/KeyMapDoc/
EstimateChunk/EstimateDoc -- written by the pre-migration
seed_special_provision/seed_key_map/seed_estimate) into their new
Supabase-only home: session_chunks for chunk text+embeddings,
review_projects.key_map_extraction/estimate_extraction for the structured
extraction JSON.

Most review_projects rows already have this in Supabase -- the frontend calls
POST /api/session/upload with project_id right after every review completes
(DocumentReview.tsx), which runs _process_session_reuse and copies the same
data. This script is the safety net for rows where that call failed, was
skipped, or the project predates the chat feature.

Idempotent: skips any (project, doc_type) pair that already has session_chunks
rows, and any review_projects row whose extraction column is already set.

Usage
-----
    python scripts/backfill_sp_keymap_estimate_to_supabase.py [--dry-run]

Run AFTER migration 003 and AFTER Tasks 3-5 are deployed (so new reviews stop
needing this path), BEFORE Task 7 deletes the Neo4j read paths and Task 8
deletes the Neo4j SPChunk/KeyMapChunk/KeyMapDoc/EstimateChunk/EstimateDoc data
-- this script is what reads that data one last time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.database import get_db                              # noqa: E402
from app.ingestion.chunk_store import insert_session_chunks   # noqa: E402
from app.neo4j_client import get_neo4j                        # noqa: E402

_DOC_TYPES = {
    "special_provision_pdf_path": ("special_provision", "SPChunk"),
    "key_map_pdf_path":           ("key_map",            "KeyMapChunk"),
    "estimate_pdf_path":          ("estimate",            "EstimateChunk"),
}


def _copy_chunks(graph, db, project_id: str, label: str, doc_type: str, dry_run: bool) -> int:
    rows = graph.query(
        f"MATCH (c:{label} {{projectId: $pid}}) "
        "RETURN c.id AS id, c.pagePdf AS pagePdf, c.content AS content, "
        "       c.embedding AS embedding ORDER BY c.id",
        params={"pid": project_id},
    )
    if not rows:
        return 0
    if dry_run:
        return len(rows)
    chunks = [
        {
            "content": r["content"],
            "embedding": r["embedding"],
            "metadata": {"doc_type": doc_type, "page_pdf": r["pagePdf"]},
        }
        for r in rows
    ]
    insert_session_chunks(db, project_id, chunks)
    return len(chunks)


def _copy_extraction(graph, db, project_id: str, doc_label: str, column: str, dry_run: bool) -> bool:
    rows = graph.query(
        f"MATCH (d:{doc_label} {{projectId: $pid}}) RETURN d.extractionJson AS jsonStr LIMIT 1",
        params={"pid": project_id},
    )
    if not rows or not rows[0].get("jsonStr"):
        return False
    if dry_run:
        return True
    extraction = json.loads(rows[0]["jsonStr"])
    db.table("review_projects").update({column: extraction}).eq("id", project_id).execute()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill SP/KeyMap/Estimate data into Supabase.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be copied without writing.")
    args = parser.parse_args()

    db = get_db()
    graph = get_neo4j()

    projects = db.table("review_projects").select(
        "id, special_provision_pdf_path, key_map_pdf_path, estimate_pdf_path, "
        "key_map_extraction, estimate_extraction"
    ).execute().data

    print(f"-- {len(projects)} review_projects rows")
    chunk_copies = 0
    extraction_copies = 0

    for p in projects:
        pid = p["id"]
        for path_col, (doc_type, label) in _DOC_TYPES.items():
            if not p.get(path_col):
                continue  # document was never uploaded for this project
            existing = (
                db.table("session_chunks").select("id", count="exact")
                .eq("session_id", pid).eq("doc_type", doc_type).limit(1).execute()
            )
            if existing.count:
                continue  # already in Supabase (e.g. via the reuse-copy path)
            n = _copy_chunks(graph, db, pid, label, doc_type, args.dry_run)
            if n:
                chunk_copies += n
                verb = "would copy" if args.dry_run else "copied"
                print(f"   [{pid}] {doc_type}: {verb} {n} chunks")

        if p.get("key_map_pdf_path") and not p.get("key_map_extraction"):
            if _copy_extraction(graph, db, pid, "KeyMapDoc", "key_map_extraction", args.dry_run):
                extraction_copies += 1
                verb = "would copy" if args.dry_run else "copied"
                print(f"   [{pid}] key_map_extraction: {verb}")

        if p.get("estimate_pdf_path") and not p.get("estimate_extraction"):
            if _copy_extraction(graph, db, pid, "EstimateDoc", "estimate_extraction", args.dry_run):
                extraction_copies += 1
                verb = "would copy" if args.dry_run else "copied"
                print(f"   [{pid}] estimate_extraction: {verb}")

    verb = "Would copy" if args.dry_run else "Copied"
    print(f"\n{verb} {chunk_copies} chunks and {extraction_copies} extraction documents.")
    if args.dry_run:
        print("-- dry run: no changes written")


if __name__ == "__main__":
    main()
