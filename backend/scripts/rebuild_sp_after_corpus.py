"""Rebuild the Special Provision "after" corpus for the hybrid-retrieval probe.

Task 11's before/after comparison needs the SAME document text run through
both the pre-section-aware chunker that produced a real project's existing
Special Provision chunks and the current ``chunk_special_provision`` -- but
no source PDF is available in this environment. Instead:

  1. Read a baseline project's ~245 ``special_provision`` chunks back out of
     ``session_chunks``, in ``chunk_index`` order -- real production text,
     produced by whatever pre-Task-6 chunker ingested it (its metadata
     carries no ``section_id``).
  2. Stitch them into one document string, undoing the sliding-window
     overlap between adjacent chunks: for every consecutive pair, the
     longest exact token-for-token match between chunk[i]'s tail and
     chunk[i+1]'s head (search bounded at 300 tokens; the SP chunker's own
     overlap constant is 100) is dropped from chunk[i+1] before
     concatenating. Empirically, this baseline's adjacent pairs show either
     a clean 0-token or a clean 100-token overlap and nothing in between --
     an exact (not fuzzy) match is the right tool here, and it never
     silently drops real text: where no exact match is found, nothing is
     removed.
  3. Run the stitched text through the CURRENT
     ``app.ingestion.session_chunker.chunk_special_provision`` (no
     ``pdf_path`` -- there is no PDF, so the table-extraction path is
     skipped, same as a review upload with ``pdf_path=None`` already does
     elsewhere in this codebase).
  4. Embed and insert the result under a fresh session id via the same
     ``app.ingestion.chunk_store.insert_session_chunks`` every real
     ingestion path uses.

Known distortion this introduces (see the results doc for the full
discussion): the stitched text is handed to ``chunk_special_provision`` as
ONE synthetic page, not ~165 real PDF pages, so:
  - ``page_pdf`` metadata on every "after" chunk reads 1 -- not used by the
    probe (which compares section_id/table anchors and similarity, not page
    numbers), but wrong if read for anything else.
  - ``_detect_sp_boilerplate`` samples the first 15 *pages* for lines that
    repeat across pages; with one page it can never find a repeat, so it
    strips nothing here. The baseline text already carries unstripped
    boilerplate itself (e.g. literal "Page 2 of 103" inline), so this
    changes nothing relative to the baseline.
  - ``_is_gantt_page`` filtering runs once instead of once per real page;
    irrelevant here since none of this project's SP text reads as a Gantt
    printout.
None of these bear on section/table identification, which is what the
probe measures.

Usage
-----
    cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe \\
        scripts/rebuild_sp_after_corpus.py --from-session <baseline-id> --new-session <uuid>

Prints the new session id and a stitching summary. Does not delete
anything -- the caller cleans up the inserted rows when done comparing:
    db.table("session_chunks").delete().eq("session_id", new_session_id).execute()
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

import tiktoken

# ── Ensure backend/ package root is on sys.path ──────────────────────────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Local-container env: backend/.env first, then backend/local.env.local with
# override=True -- same convention as tests/integration/conftest.py -- done
# BEFORE importing app.* so app.config's own weaker default loading doesn't win.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_BACKEND / ".env")
load_dotenv(_BACKEND / "local.env.local", override=True)

from app.ingestion.chunk_store import insert_session_chunks  # noqa: E402
from app.ingestion.session_chunker import chunk_special_provision  # noqa: E402

_DOC_TYPE = "special_provision"
_ENC = tiktoken.get_encoding("cl100k_base")
_MAX_OVERLAP_SEARCH = 300  # generous upper bound; the SP chunker's own overlap constant is 100


def _fetch_ordered_chunks(db: Any, session_id: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    start, page = 0, 1000
    while True:
        batch = (
            db.table("session_chunks").select("content,metadata")
            .eq("session_id", session_id).eq("doc_type", _DOC_TYPE)
            .range(start, start + page - 1).execute().data
        ) or []
        rows.extend(batch)
        if len(batch) < page:
            break
        start += page
    rows.sort(key=lambda r: (r.get("metadata") or {}).get("chunk_index", 0))
    return rows


def _overlap_len(a_tokens: List[int], b_tokens: List[int]) -> int:
    """Longest exact suffix-of-a / prefix-of-b match, up to _MAX_OVERLAP_SEARCH."""
    a_tail = a_tokens[-_MAX_OVERLAP_SEARCH:]
    for length in range(min(len(a_tail), len(b_tokens)), 0, -1):
        if a_tail[-length:] == b_tokens[:length]:
            return length
    return 0


def stitch(rows: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Undo the sliding-window overlap and return (stitched_text, stats)."""
    token_lists = [_ENC.encode(r["content"]) for r in rows]
    stitched: List[int] = list(token_lists[0]) if token_lists else []
    overlaps: List[int] = []
    for i in range(1, len(token_lists)):
        ov = _overlap_len(token_lists[i - 1], token_lists[i])
        overlaps.append(ov)
        stitched.extend(token_lists[i][ov:])
    stats = {
        "num_source_chunks": len(rows),
        "stitched_tokens": len(stitched),
        "pairs_with_overlap": sum(1 for o in overlaps if o > 0),
        "pairs_without_overlap": sum(1 for o in overlaps if o == 0),
        "overlap_lengths_seen": sorted(set(overlaps)),
    }
    return _ENC.decode(stitched), stats


def rebuild(from_session: str, new_session: str) -> str:
    from app.config import config
    from app.database import get_db
    from langchain_openai import OpenAIEmbeddings

    db = get_db()
    rows = _fetch_ordered_chunks(db, from_session)
    if not rows:
        raise SystemExit(f"no special_provision chunks found for session {from_session}")

    text, stats = stitch(rows)
    print(
        f"stitched {stats['num_source_chunks']} chunks -> {stats['stitched_tokens']} tokens "
        f"({stats['pairs_with_overlap']} pairs overlapped, {stats['pairs_without_overlap']} did not, "
        f"overlap lengths seen: {stats['overlap_lengths_seen']})"
    )

    pages = [{"page_num": 1, "text": text}]
    chunks = chunk_special_provision(pages)  # no pdf_path -- no PDF is available
    print(f"chunk_special_provision produced {len(chunks)} chunks")
    with_section = sum(1 for c in chunks if c["metadata"].get("section_id"))
    print(f"  {with_section}/{len(chunks)} carry a section_id")

    emb = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)
    vectors = emb.embed_documents([c["content"] for c in chunks])
    insert_session_chunks(db, new_session, [{**c, "embedding": v} for c, v in zip(chunks, vectors)])
    print(f"inserted {len(chunks)} chunks under session {new_session}")
    return new_session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-session", required=True, help="baseline session_id to read and stitch")
    parser.add_argument("--new-session", default=None, help="session_id for the after-corpus (default: a fresh uuid4)")
    args = parser.parse_args()
    rebuild(args.from_session, args.new_session or str(uuid.uuid4()))


if __name__ == "__main__":
    main()
