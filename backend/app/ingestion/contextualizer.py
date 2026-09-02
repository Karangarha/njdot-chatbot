"""Contextual retrieval for NJDOT chunk dicts.

WHY THIS EXISTS
---------------
A chunk like "The mix shall contain no more than 20% RAP" is semantically weak
in isolation — its embedding lands near other RAP sentences in the corpus, not
necessarily in the right section.  When we prepend a generated context sentence:

    "This chunk is from 902.02.03 Mix Design under Division 900 Hot Mix Asphalt
    in Spec2019.  It specifies RAP content limits for HMA base mixtures."

the embedding now captures *what the chunk is about and where it sits*, which
significantly improves retrieval precision for ambiguous or terse passages.

WHAT THIS MODULE DOES
---------------------
1. Accepts the list of chunk dicts produced by ``Chunker.chunk()``.
2. For each chunk, calls the LLM with the chunk content + its metadata to
   generate a one-sentence context description (``context_summary``).
3. Adds ``context_summary`` to ``chunk["metadata"]``.
4. Marks ``chunk["embed_text"]`` as ``context_summary + "\\n\\n" + content``
   so the embedder uses the enriched text without altering ``content`` itself.

BATCHING
--------
To minimise LLM round-trips, multiple chunks are packed into a single prompt
using a numbered list format.  Default batch size is 20 chunks per call.

RETRY
-----
On failure the batch is retried once with individual per-chunk calls so one
bad batch never silently skips chunks.

USAGE
-----
    from app.ingestion.contextualizer import Contextualizer

    contextualizer = Contextualizer()
    chunks = contextualizer.contextualize(chunks, doc_name="Spec2019")
    # Each chunk now has metadata["context_summary"] and chunk["embed_text"]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Package-root import shim ──────────────────────────────────────────────────
try:
    from app.config import config
    from app.generation.llm_client import LLMClient
except ImportError:
    _pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from app.config import config          # type: ignore[no-redef]
    from app.generation.llm_client import LLMClient  # type: ignore[no-redef]


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_BATCH_SIZE:  int = 20
DEFAULT_MAX_ATTEMPTS: int = 2
_BACKOFF_SECONDS:    int = 2

# Character limit for chunk content sent to the LLM — keeps prompts cheap.
# The LLM only needs enough to understand what the chunk is about, not the
# full 750-token body.
_CONTENT_PREVIEW_CHARS: int = 600

_SYSTEM_PROMPT = """\
You are a document indexing assistant for NJDOT (New Jersey Department of \
Transportation) construction specification documents.

Given one or more numbered chunks from these documents, write ONE concise \
sentence (max 30 words) for each chunk that describes:
  - what topic or requirement the chunk covers
  - where it sits in the document hierarchy (division, section, subsection)
  - the document it comes from

Output ONLY the numbered sentences, one per line, matching the input numbers.
Do not include any explanation, preamble, or extra text.

Example input:
1. [doc=Spec2019 | division=DIVISION 900 – HOT MIX ASPHALT | section_id=902.02.03 | title=Mix Design | kind=text]
   The mix shall be designed using the Superpave gyratory compactor...

Example output:
1. Spec2019 section 902.02.03 Mix Design (Division 900) specifies Superpave gyratory compaction requirements for HMA mixtures.\
"""


# ── Main class ────────────────────────────────────────────────────────────────

class Contextualizer:
    """
    Generate context summaries for chunk dicts and inject them before embedding.

    Parameters
    ----------
    llm_client : LLMClient or None
        Pre-built LLM client to reuse.  ``None`` → create a fresh one.
    batch_size : int
        Number of chunks packed into one LLM call (default 20).
    max_attempts : int
        How many times to retry a failing batch (default 2).
    model : str or None
        LLM model override.  ``None`` → use LLMClient defaults.
    """

    def __init__(
        self,
        llm_client:  Optional[LLMClient] = None,
        batch_size:  int                 = DEFAULT_BATCH_SIZE,
        max_attempts: int                = DEFAULT_MAX_ATTEMPTS,
        model:       Optional[str]       = None,
    ) -> None:
        self._llm         = llm_client or LLMClient(model=model)
        self.batch_size   = batch_size
        self.max_attempts = max_attempts

    # ── Public ────────────────────────────────────────────────────────────────

    def contextualize(
        self,
        chunks:   List[Dict[str, Any]],
        doc_name: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Add ``context_summary`` to each chunk's metadata and set ``embed_text``.

        Parameters
        ----------
        chunks : list[dict]
            Output of ``Chunker.chunk()``.  Mutated in-place; same list returned.
        doc_name : str
            Document identifier injected into the prompt (e.g. ``"Spec2019"``).

        Returns
        -------
        list[dict]
            Same list; each element gains:
              ``metadata["context_summary"]`` — generated 1-sentence description
              ``embed_text``                  — context_summary + "\\n\\n" + content
        """
        if not chunks:
            return chunks

        total_batches = (len(chunks) + self.batch_size - 1) // self.batch_size
        print(f"  Contextualizing {len(chunks)} chunks in {total_batches} batch(es)…")

        for batch_idx in range(total_batches):
            start = batch_idx * self.batch_size
            end   = min(start + self.batch_size, len(chunks))
            batch = chunks[start:end]

            print(
                f"    Batch {batch_idx + 1}/{total_batches} "
                f"(chunks {start + 1}–{end})…",
                end=" ",
                flush=True,
            )

            summaries = self._contextualize_batch(batch, doc_name)

            for chunk, summary in zip(batch, summaries):
                chunk["metadata"]["context_summary"] = summary
                chunk["embed_text"] = f"{summary}\n\n{chunk['content']}"

            print("✅")

        return chunks

    # ── Private ───────────────────────────────────────────────────────────────

    def _contextualize_batch(
        self,
        batch:    List[Dict[str, Any]],
        doc_name: str,
    ) -> List[str]:
        """
        Call the LLM for *batch* with retry.

        On failure after all attempts, falls back to individual per-chunk calls
        so no chunk is left without a summary.
        """
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._call_llm_batch(batch, doc_name)
            except Exception as exc:
                if attempt == self.max_attempts:
                    break
                print(
                    f"\n    ⚠️  Batch attempt {attempt} failed ({exc}); "
                    f"retrying in {_BACKOFF_SECONDS}s…",
                    end=" ",
                    flush=True,
                )
                time.sleep(_BACKOFF_SECONDS)

        # Final fallback: call one chunk at a time
        print("\n    ⚠️  Batch failed; falling back to per-chunk calls…")
        return [
            self._call_llm_single(chunk, doc_name)
            for chunk in batch
        ]

    def _call_llm_batch(
        self,
        batch:    List[Dict[str, Any]],
        doc_name: str,
    ) -> List[str]:
        """
        Pack *batch* into a single numbered-list prompt and parse the response.

        Raises ``ValueError`` if the response has the wrong number of lines
        so the caller can retry.
        """
        user_message = self._build_batch_prompt(batch, doc_name)
        raw          = self._llm.complete(_SYSTEM_PROMPT, user_message)

        summaries = self._parse_numbered_response(raw, expected=len(batch))
        return summaries

    def _call_llm_single(
        self,
        chunk:    Dict[str, Any],
        doc_name: str,
    ) -> str:
        """Generate a context summary for a single chunk; never raises."""
        try:
            prompt = self._build_single_prompt(chunk, doc_name)
            raw    = self._llm.complete(_SYSTEM_PROMPT, prompt)
            # Strip any leading "1. " the model may add
            line = raw.strip().splitlines()[0].strip()
            if line.startswith("1."):
                line = line[2:].strip()
            return line or self._fallback_summary(chunk, doc_name)
        except Exception:
            return self._fallback_summary(chunk, doc_name)

    # ── Prompt builders ───────────────────────────────────────────────────────

    @staticmethod
    def _chunk_header(chunk: Dict[str, Any], doc_name: str) -> str:
        """Build the metadata header line for a chunk's prompt entry."""
        meta = chunk.get("metadata", {})
        parts = [
            f"doc={doc_name or meta.get('doc', 'unknown')}",
            f"division={meta.get('division', '')}",
            f"section_id={meta.get('section_id', '')}",
            f"title={meta.get('section_title', '')}",
            f"kind={meta.get('kind', 'text')}",
        ]
        return "[" + " | ".join(p for p in parts if not p.endswith("=")) + "]"

    def _build_batch_prompt(
        self,
        batch:    List[Dict[str, Any]],
        doc_name: str,
    ) -> str:
        lines: List[str] = []
        for i, chunk in enumerate(batch, start=1):
            header  = self._chunk_header(chunk, doc_name)
            preview = chunk.get("content", "")[:_CONTENT_PREVIEW_CHARS]
            lines.append(f"{i}. {header}\n   {preview}")
        return "\n\n".join(lines)

    def _build_single_prompt(
        self,
        chunk:    Dict[str, Any],
        doc_name: str,
    ) -> str:
        header  = self._chunk_header(chunk, doc_name)
        preview = chunk.get("content", "")[:_CONTENT_PREVIEW_CHARS]
        return f"1. {header}\n   {preview}"

    # ── Response parser ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_numbered_response(raw: str, expected: int) -> List[str]:
        """
        Extract numbered lines ``"N. <sentence>"`` from *raw*.

        Raises ``ValueError`` if fewer than *expected* lines are found.
        """
        import re
        pattern = re.compile(r'^\d+\.\s+(.+)$', re.MULTILINE)
        matches = pattern.findall(raw.strip())

        if len(matches) < expected:
            raise ValueError(
                f"Expected {expected} context lines, got {len(matches)}. "
                f"Raw response: {raw[:200]!r}"
            )
        # Trim to exactly expected in case the model added extras
        return [m.strip() for m in matches[:expected]]

    # ── Fallback ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fallback_summary(chunk: Dict[str, Any], doc_name: str) -> str:
        """Rule-based summary used when the LLM call fails entirely."""
        meta  = chunk.get("metadata", {})
        sid   = meta.get("section_id", "")
        title = meta.get("section_title", "")
        div   = meta.get("division", "")
        kind  = meta.get("kind", "text")
        doc   = doc_name or meta.get("doc", "")

        if kind == "table":
            return (
                f"{doc} table {sid} {title} ({div}) — tabular data."
            )
        return (
            f"{doc} section {sid} {title} ({div})."
        )


# ── Convenience wrapper ───────────────────────────────────────────────────────

def contextualize_chunks(
    chunks:   List[Dict[str, Any]],
    doc_name: str = "",
    model:    Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    One-shot helper: instantiate ``Contextualizer`` and run in a single call.

    Parameters
    ----------
    chunks : list[dict]
        Output of ``Chunker.chunk()``.
    doc_name : str
        Document identifier (e.g. ``"Spec2019"``).
    model : str or None
        LLM model override.  ``None`` → LLMClient default.

    Returns
    -------
    list[dict]
        Same list with ``metadata["context_summary"]`` and ``embed_text`` added.
    """
    return Contextualizer(model=model).contextualize(chunks, doc_name=doc_name)
