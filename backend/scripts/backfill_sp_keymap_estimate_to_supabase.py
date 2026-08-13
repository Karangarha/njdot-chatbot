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

Idempotent: skips any (project, doc_type) pair whose session_chunks row count
already matches (or exceeds) the Neo4j source count, and any review_projects
row whose extraction column is already set. A pair with a nonzero but
incomplete session_chunks count (from a prior run that crashed mid-batch) is
NOT silently skipped or blindly re-copied -- insert_session_chunks has no
dedup/upsert key, so a blind re-copy would duplicate the rows already
committed. Instead it is reported as a mismatch requiring manual review; see
the "MISMATCHES REQUIRING MANUAL REVIEW" summary at the end of a run.

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


def _copy_chunks(
    graph, db, project_id: str, label: str, doc_type: str, dry_run: bool
) -> tuple[int, dict | None]:
    """Copy SP/KeyMap/Estimate chunks for one (project, doc_type) pair.

    Returns ``(n_copied, mismatch)``. ``mismatch`` is ``None`` unless Supabase
    already holds a nonzero-but-incomplete number of rows for this pair (i.e.
    a prior run crashed mid-way through ``insert_session_chunks``'s batched
    writes) -- in that case this function does NOT call
    ``insert_session_chunks`` (which has no dedup/upsert key and would create
    duplicate rows for the chunks that already made it in) and instead
    returns a dict describing the mismatch for the caller to report and skip.
    """
    rows = graph.query(
        f"MATCH (c:{label} {{projectId: $pid}}) "
        "RETURN c.id AS id, c.pagePdf AS pagePdf, c.content AS content, "
        "       c.embedding AS embedding ORDER BY c.id",
        params={"pid": project_id},
    )
    neo4j_count = len(rows)
    if not neo4j_count:
        return 0, None

    existing = (
        db.table("session_chunks").select("id", count="exact")
        .eq("session_id", project_id).eq("doc_type", doc_type).execute()
    )
    supabase_count = existing.count or 0

    if supabase_count >= neo4j_count:
        return 0, None  # already fully copied (e.g. via the reuse-copy path)

    if supabase_count > 0:
        # Partial copy from a prior crashed run -- re-running insert would
        # duplicate the rows already committed. Surface it instead.
        return 0, {
            "project_id": project_id,
            "doc_type": doc_type,
            "neo4j_count": neo4j_count,
            "supabase_count": supabase_count,
        }

    if dry_run:
        return neo4j_count, None

    chunks = [
        {
            "content": r["content"],
            "embedding": r["embedding"],
            "metadata": {"doc_type": doc_type, "page_pdf": r["pagePdf"]},
        }
        for r in rows
    ]
    insert_session_chunks(db, project_id, chunks)
    return len(chunks), None


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
    mismatches: list[dict] = []

    for p in projects:
        pid = p["id"]
        for path_col, (doc_type, label) in _DOC_TYPES.items():
            if not p.get(path_col):
                continue  # document was never uploaded for this project
            n, mismatch = _copy_chunks(graph, db, pid, label, doc_type, args.dry_run)
            if mismatch:
                mismatches.append(mismatch)
                print(
                    f"   [!!] [{pid}] {doc_type}: MISMATCH -- Neo4j has "
                    f"{mismatch['neo4j_count']} rows, Supabase has "
                    f"{mismatch['supabase_count']} (partial prior copy) -- "
                    "skipping, requires manual review"
                )
                continue
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

    if mismatches:
        print("\n-- MISMATCHES REQUIRING MANUAL REVIEW --")
        print(
            "-- The following (project, doc_type) pairs have a nonzero but "
            "incomplete row count in Supabase, indicating a prior run crashed "
            "mid-copy. Auto re-running was skipped to avoid duplicate rows. "
            "Investigate and either delete the partial rows and re-run this "
            "script, or manually complete the copy."
        )
        for m in mismatches:
            print(
                f"   [{m['project_id']}] {m['doc_type']}: "
                f"Neo4j={m['neo4j_count']} rows, Supabase={m['supabase_count']} rows "
                f"({m['neo4j_count'] - m['supabase_count']} missing)"
            )


if __name__ == "__main__":
    main()
