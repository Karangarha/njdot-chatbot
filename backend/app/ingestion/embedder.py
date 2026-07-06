"""OpenAI embedder for NJDOT chunk dicts.

Takes the list of chunk dicts produced by ``Chunker.chunk()`` and adds an
``embedding`` field (``list[float]``, 1536 dimensions) to each one.

Model
-----
text-embedding-3-small  (1536 dimensions, reads from Config.EMBEDDING_MODEL)

Batching
--------
Chunks are sent to the API in groups of 100 (configurable).  Each batch
prints a one-line progress report:

    Embedding batch 1/9... ✅

Retries
-------
On ``RateLimitError`` or ``APIStatusError`` the batch is retried up to
``max_attempts`` times (default 3) with exponential back-off:
    attempt 1 fails → wait 1 s
    attempt 2 fails → wait 2 s
    attempt 3 fails → raise

Usage
-----
    from app.ingestion.embedder import embed_chunks

    chunks = Chunker().chunk(pages)               # list of chunk dicts
    chunks = embed_chunks(chunks)                 # adds "embedding" key in-place
    print(len(chunks[0]["embedding"]))            # 1536
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ── Package-root import shim (supports both -m and direct script execution) ───
try:
    from app.config import config
except ImportError:
    _pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from app.config import config  # type: ignore[no-redef]

import openai


# ── Module-level constants ────────────────────────────────────────────────────

DEFAULT_BATCH_SIZE:  int = 100
DEFAULT_MAX_ATTEMPTS: int = 3
# Back-off wait in seconds before attempt n (index 0 = before attempt 2, etc.)
_BACKOFF_SECONDS: list[int] = [1, 2]


# ── Main class ────────────────────────────────────────────────────────────────

class Embedder:
    """
    Embed a list of chunk dicts using the OpenAI embeddings API.

    Parameters
    ----------
    api_key : str or None
        OpenAI API key.  ``None`` → read from ``Config.OPENAI_API_KEY``.
    model : str
        Embedding model name (default ``Config.EMBEDDING_MODEL``).
    batch_size : int
        Maximum number of chunks per API call (default 100).
    max_attempts : int
        How many times to try a failing batch before raising (default 3).
    """

    def __init__(
        self,
        api_key:      str | None = None,
        model:        str        = config.EMBEDDING_MODEL,
        batch_size:   int        = DEFAULT_BATCH_SIZE,
        max_attempts: int        = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        _key = api_key or config.OPENAI_API_KEY
        if not _key:
            raise ValueError(
                "OpenAI API key not found.  Set OPENAI_API_KEY in your .env file "
                "or pass api_key= explicitly."
            )
        self._client      = openai.OpenAI(api_key=_key)
        self.model        = model
        self.batch_size   = batch_size
        self.max_attempts = max_attempts

    # ── Public ────────────────────────────────────────────────────────────────

    def embed(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Add an ``"embedding"`` field to every chunk dict in *chunks*.

        The operation is performed in-place; the same list is returned so the
        call can be chained.

        Parameters
        ----------
        chunks : list[dict]
            Output of ``Chunker.chunk()`` (optionally post-processed by
            ``Contextualizer``).  Each dict must contain ``"content"``.
            If ``"embed_text"`` is present (set by ``Contextualizer``),
            it is used as the embedding input instead of ``"content"``
            so the vector carries contextual signal without altering the
            stored content.

        Returns
        -------
        list[dict]
            The same list, each element now containing
            ``"embedding": list[float]`` of length 1536.
        """
        if not chunks:
            return chunks

        total_batches = (len(chunks) + self.batch_size - 1) // self.batch_size

        for batch_idx in range(total_batches):
            start = batch_idx * self.batch_size
            end   = min(start + self.batch_size, len(chunks))
            batch = chunks[start:end]

            print(
                f"  Embedding batch {batch_idx + 1}/{total_batches}...",
                end=" ",
                flush=True,
            )

            # Use embed_text (context + content) when present; else raw content.
            texts      = [c.get("embed_text") or c["content"] for c in batch]
            embeddings = self._embed_batch_with_retry(texts)

            for chunk, embedding in zip(batch, embeddings):
                chunk["embedding"] = embedding

            print("✅")

        return chunks

    # ── Private ───────────────────────────────────────────────────────────────

    def _embed_batch_with_retry(self, texts: List[str]) -> List[List[float]]:
        """
        Call the embeddings API for *texts*, retrying on transient errors.

        Raises the last exception if all attempts are exhausted.
        """
        last_exc: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.embeddings.create(
                    model=self.model,
                    input=texts,
                )
                # response.data is ordered to match the input list
                return [item.embedding for item in response.data]

            except (openai.RateLimitError, openai.APIStatusError) as exc:
                last_exc = exc
                if attempt == self.max_attempts:
                    break
                wait = _BACKOFF_SECONDS[attempt - 1]
                print(
                    f"\n    ⚠️  API error on attempt {attempt}/{self.max_attempts} "
                    f"— retrying in {wait}s…",
                    flush=True,
                )
                time.sleep(wait)

            except openai.APIConnectionError as exc:
                # Connection errors are raised immediately (not rate-limit related)
                raise

        raise last_exc  # type: ignore[misc]

    def embed_parallel(
        self,
        chunks:      List[Dict[str, Any]],
        max_workers: int = 3,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Embed chunks using concurrent batch API calls.

        Faster than ``embed()`` for session ingestion because multiple
        batches are in-flight simultaneously.  Uses the same per-batch
        retry logic as ``embed()``.

        Parameters
        ----------
        chunks : list[dict]
            Chunks to embed (must have ``"content"`` key).
        max_workers : int
            Number of concurrent OpenAI API calls (default 3).
        on_progress : callable or None
            Called after each batch completes as ``on_progress(done, total)``.

        Returns
        -------
        list[dict]
            Same list with ``"embedding": list[float]`` added in-place.
        """
        if not chunks:
            return chunks

        batches   = [chunks[i : i + self.batch_size] for i in range(0, len(chunks), self.batch_size)]
        total     = len(batches)
        completed = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_batch = {
                executor.submit(
                    self._embed_batch_with_retry,
                    [c["content"] for c in batch],
                ): batch
                for batch in batches
            }
            for future in as_completed(future_to_batch):
                batch      = future_to_batch[future]
                embeddings = future.result()
                for chunk, emb in zip(batch, embeddings):
                    chunk["embedding"] = emb
                completed += 1
                if on_progress:
                    on_progress(completed, total)

        return chunks


# ── Convenience wrapper ───────────────────────────────────────────────────────

def embed_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One-shot helper: instantiate ``Embedder`` and embed *chunks* in a single call.

    Parameters
    ----------
    chunks : list[dict]
        Output of ``Chunker.chunk()``.

    Returns
    -------
    list[dict]
        Same list with ``"embedding"`` added to each element.
    """
    return Embedder().embed(chunks)


# ── Dry-run self-test ─────────────────────────────────────────────────────────
# Run from the backend/ directory:
#
#     python -m app.ingestion.embedder
#
# Embeds 3 chunks from the scheduling manual and confirms 1536 dimensions.

if __name__ == "__main__":
    _root = Path(__file__).resolve().parent.parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

    from app.ingestion.pdf_parser import PDFParser   # noqa: E402
    from app.ingestion.chunker    import Chunker     # noqa: E402

    _PDF = _root / "data" / "raw_pdfs" / "constructionschedulingmanual.pdf"
    if not _PDF.exists():
        raise FileNotFoundError(f"PDF not found: {_PDF}")

    print("\n🧪 Embedder dry-run — constructionschedulingmanual.pdf")
    print("=" * 60)

    _pages  = PDFParser(str(_PDF)).extract_text()
    _chunks = Chunker(doc_type="scheduling").chunk(_pages)
    print(f"   Total chunks in document: {len(_chunks)}")

    # Only embed the first 3 to keep API cost minimal
    _sample = _chunks[:3]
    print(f"   Embedding {len(_sample)} sample chunks…\n")

    _embedder = Embedder()
    _embedded = _embedder.embed(_sample)

    print()
    for _i, _chunk in enumerate(_embedded, start=1):
        _emb  = _chunk["embedding"]
        _meta = _chunk["metadata"]
        print(f"  Chunk {_i}")
        print(f"    section_id    : {_meta['section_id']}")
        print(f"    section_title : {_meta['section_title']}")
        print(f"    embedding dim : {len(_emb)}")
        assert len(_emb) == 1536, f"❌ Expected 1536 dimensions, got {len(_emb)}"
        print(f"    ✅ 1536 dimensions confirmed")
        print(f"    first 5 values: {[round(v, 6) for v in _emb[:5]]}")

    print("\n✅ Embedder dry-run complete.\n")
