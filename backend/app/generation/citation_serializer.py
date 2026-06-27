"""Citation validation and response serialization for NJDOT generation.

``CitationSerializer.serialize()`` takes the raw text returned by the LLM
(expected to be JSON, possibly fenced in markdown) and the list of retrieved
chunks that were included in the context.  It:

1. Strips any markdown code fences (`` ```json … ``` `` or `` ``` … ``` ``).
2. Parses the JSON into ``{"answer": str, "citations": list}``.
3. Validates each citation against the retrieved chunks:
   - First tries to match by ``chunk_id`` (UUID → ``chunk["id"]``).
   - Falls back to matching by ``section`` (→ ``chunk["metadata"]["section_id"]``).
   - If matched, corrects ``page_printed`` and ``page_pdf`` from the ground-truth
     chunk metadata so hallucinated page numbers are fixed.
4. Returns the cleaned dict.  **Never raises** — if JSON parsing fails, returns
   ``{"answer": <raw_text>, "citations": [], "parse_error": true}``.

Result schema
-------------
::

    {
        "answer":    str,
        "citations": [
            {
                "document":     str,
                "section":      str,
                "page_printed": int | None,
                "page_pdf":     int | None,
                "chunk_id":     str | None,
                "verified":     bool     # True if matched a retrieved chunk
            },
            …
        ]
    }
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


# Strips opening/closing markdown code fences like ```json … ``` or ``` … ```
_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL,
)


class CitationSerializer:
    """Parse, validate, and serialize the LLM generation response.

    This class is stateless; a single instance can be reused across calls.
    """

    # ── Public ────────────────────────────────────────────────────────────────

    def serialize(
        self,
        llm_response_text: str,
        chunks:            List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Parse the LLM response and validate citations against retrieved chunks.

        Parameters
        ----------
        llm_response_text : str
            Raw text from ``LLMClient.complete()``.  Expected to be JSON,
            optionally wrapped in markdown code fences.
        chunks : list[dict]
            The *same* chunk list that was passed to ``PromptBuilder.build()``,
            used as ground truth for citation validation.

        Returns
        -------
        dict
            ``{"answer": str, "citations": list[dict]}``.
            On parse failure adds ``"parse_error": True`` and returns the
            raw text in ``"answer"`` with an empty ``"citations"`` list.
        """
        cleaned = self._strip_fences(llm_response_text.strip())

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            # Graceful fallback — never crash
            return {
                "answer":      llm_response_text,
                "citations":   [],
                "parse_error": True,
            }

        answer     = str(payload.get("answer", ""))
        raw_cites  = payload.get("citations") or []

        # Build lookup indexes from the retrieved chunks for fast validation
        by_id      = self._index_by_id(chunks)
        by_section = self._index_by_section(chunks)

        validated = [
            self._validate_citation(c, by_id, by_section)
            for c in raw_cites
            if isinstance(c, dict)
        ]

        return {"answer": answer, "citations": validated}

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove markdown code fences if present."""
        m = _FENCE_RE.match(text)
        return m.group(1).strip() if m else text

    @staticmethod
    def _index_by_id(
        chunks: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Map chunk UUID → chunk dict."""
        return {c["id"]: c for c in chunks if isinstance(c, dict) and c.get("id")}

    @staticmethod
    def _index_by_section(
        chunks: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Map section_id (case-insensitive) → chunk dict (first match wins)."""
        idx: Dict[str, Dict[str, Any]] = {}
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            sid = (chunk.get("metadata") or {}).get("section_id")
            if sid and sid.lower() not in idx:
                idx[sid.lower()] = chunk
        return idx

    @staticmethod
    def _validate_citation(
        citation:   Dict[str, Any],
        by_id:      Dict[str, Dict[str, Any]],
        by_section: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Attempt to match the LLM citation to a retrieved chunk and correct
        any hallucinated page numbers.

        Matching priority
        -----------------
        1. ``chunk_id``  → exact UUID match on ``chunk["id"]``
        2. ``section``   → case-insensitive match on ``metadata.section_id``
        3. No match      → return citation as-is with ``verified=False``
        """
        cid     = citation.get("chunk_id")
        section = (citation.get("section") or "").lower()

        # Try chunk_id first
        matched: Optional[Dict[str, Any]] = None
        if cid and cid in by_id:
            matched = by_id[cid]
        elif section and section in by_section:
            matched = by_section[section]

        result: Dict[str, Any] = {
            "document":     citation.get("document"),
            "section":      citation.get("section"),
            "page_printed": citation.get("page_printed"),
            "page_pdf":     citation.get("page_pdf"),
            "chunk_id":     cid,
            "verified":     False,
        }

        if matched is not None:
            meta = matched.get("metadata") or {}
            # Correct page numbers from ground truth — LLMs hallucinate these
            result["page_printed"] = meta.get("page_printed", citation.get("page_printed"))
            result["page_pdf"]     = meta.get("page_pdf",     citation.get("page_pdf"))
            result["chunk_id"]     = matched.get("id", cid)
            # Also correct document name from metadata if available
            if meta.get("doc"):
                result["document"] = meta["doc"]
            result["verified"] = True

        return result
