"""backend/app/ingestion/chunk_store.py

Shared Supabase session_chunks writer.

Used by both app.api.session (chat-only uploads) and app.api.review (review
ingestion) so a chunk's insert shape is defined in exactly one place.
"""

from __future__ import annotations

from typing import Any, Dict, List

_DB_BATCH = 50


def insert_session_chunks(db: Any, session_id: str, chunks: List[Dict[str, Any]]) -> None:
    """Batch-insert embedded chunks into Supabase ``session_chunks``.

    Each ``chunks[i]`` must have ``content`` (str), ``embedding`` (list of
    float), and ``metadata`` (dict) with ``metadata["doc_type"]`` set --
    chunker functions in ``app.ingestion.session_chunker`` and
    ``app.api.review``'s ``_bytes_to_keymap_chunks``/
    ``_estimate_chunks_from_extraction`` already set this. ``embedding`` must
    already be attached to each chunk dict -- a caller holding a parallel
    ``vectors`` list (e.g. from ``embeddings.embed_documents(...)``) zips it
    in first: ``[{**c, "embedding": v} for c, v in zip(chunks, vectors)]``.
    """
    rows = [
        {
            "session_id": session_id,
            "doc_type":   c["metadata"]["doc_type"],
            "content":    c["content"],
            "embedding":  c["embedding"],
            "metadata":   c["metadata"],
        }
        for c in chunks
    ]
    for i in range(0, len(rows), _DB_BATCH):
        db.table("session_chunks").insert(rows[i : i + _DB_BATCH]).execute()
