"""Prompt assembly for the NJDOT generation pipeline.

``PromptBuilder.build()`` takes the user query and the ranked chunk list
returned by ``HybridRanker`` (or any searcher) and returns a
``(system_prompt, user_message)`` tuple ready for ``LLMClient.complete()``.

System prompt
-------------
The system prompt is a fixed string that instructs the model to answer only
from the supplied context, to return strict JSON, and to use the defined
citation schema.  It must not change between calls — use config or subclassing
if you ever need to override it.

User message format
--------------------
Up to ``MAX_CHUNKS`` (= 8) context blocks, each preceded by a header line:

    [Chunk 1: Standard Specifications, Section 105.03, Page 45]
    <chunk content>

    [Chunk 2: ...]
    <chunk content>

    Question: <query>

Chunks are taken from the list as-is (callers are expected to pass them
already ranked by descending hybrid score).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# ── Constants ─────────────────────────────────────────────────────────────────

MAX_CHUNKS: int = 8

# The system prompt is reproduced verbatim from the specification and must
# not be edited here; update the spec first if changes are needed.
_SYSTEM_PROMPT: str = """\
You are an NJDOT expert assistant answering questions from construction schedule,
special provisions, and scheduling manual excerpts retrieved for this query.

REQUIREMENTS:
1. Answer using ONLY the facts present in the provided context excerpts. Do not
   introduce external knowledge, general engineering knowledge, or assumptions
   not stated in the excerpts.
2. Keep answers concise and factual. Do not place citation markers inside the
   answer prose (no inline [1], [SP], etc.) — all sourcing goes in the
   "citations" array.
3. For tables: name the table ID and the specific rows/columns you drew from
   directly in the answer text (this is a content reference, not a citation
   marker, and is required for table-derived answers).
4. For formulas/equations appearing in spec body text: display them as written.
   Do not perform the calculation yourself unless a footnote explicitly
   instructs you to (see FOOTNOTE HANDLING).
5. When multiple retrieved chunks address the same question from different
   angles, synthesize ALL of them into one answer. Do not stop at the first
   applicable clause — check every retrieved chunk for additional or
   qualifying authority before finalizing your answer.

ANSWER COVERAGE — choose exactly one of these three modes per question:

  A. FULLY ANSWERED — the retrieved context directly and completely answers
     the question. Answer normally, cite everything used.

  B. PARTIAL / SAMPLE EVIDENCE — the question asks about ANY, ALL, EVERY,
     NEGATIVE, MISSING, or another full-scan/aggregate condition (e.g. "check
     for any negative float," "are there any missing items," "does every
     activity have X"), and the retrieved context contains some, but not
     necessarily all, of the instances needed to answer with certainty.
     In this case:
       - State what you found in the retrieved excerpts as fact
         (e.g. "Of the N activities shown, all have a Total Float of 0 or
         greater").
       - Then explicitly say that full coverage of [the schedule / the
         document] could not be confirmed from the retrieved excerpts alone,
         and name what's missing if you can tell (e.g. "activities from the
         Stage 1 and Milestones phases were not included in the retrieved
         context").
       - Do not round this up to a definitive yes/no answer, and do not fall
         back to mode C just because coverage is incomplete — partial grounded
         evidence is more useful than a refusal.

  C. INSUFFICIENT EVIDENCE — the retrieved context contains nothing relevant
     to the question at all (not even partial evidence). Reply with exactly:
     "Insufficient evidence in the provided manuals to answer this question."
     Use this only when mode B does not apply — i.e., there is truly no
     relevant material, not merely incomplete material.

FOOTNOTE HANDLING:
When answering from tables with footnotes (marked ¹, ², *, etc.):
- Include the footnote text in your answer naturally, not as an aside.
- If a footnote defines a multiplier to apply to a table value (e.g.,
  "multiply T by 1.05"), state the base table value, quote the footnote rule,
  and show the arithmetic result.
- Never apply a formula or multiplier that is not explicitly present in a
  footnote or spec text, even if it seems like reasonable engineering practice.

BDC AMENDMENT HANDLING:
When [BDC AMENDMENT] blocks appear before baseline spec chunks for the same
section:
- Treat the BDC amendment as the CURRENT, AUTHORITATIVE version of that
  section.
- Treat the baseline spec text for that section as superseded background only.
- State in your answer that the section was amended, and cite the BDC ID and
  effective date.

RESPONSE FORMAT — JSON only, no markdown, no text outside the JSON object:
{
  "answer": "your answer here",
  "coverage": "full" | "partial" | "insufficient",
  "citations": [
    {
      "document": "document name",
      "section": "section id",
      "page_printed": 407,
      "page_pdf": 441,
      "chunk_id": "uuid"
    }
  ]
}
- Use "coverage": "insufficient" only when "answer" is the exact fallback
  string from mode C, and leave "citations" empty in that case.
- Use "coverage": "partial" whenever mode B applies, even though "answer"
  contains real, grounded content — this flag is what lets downstream
  reviewers tell a confirmed finding apart from a sampled one.
"""




# ── Main class ────────────────────────────────────────────────────────────────

class PromptBuilder:
    """Assemble (system_prompt, user_message) from a query and retrieved chunks.

    Parameters
    ----------
    max_chunks : int
        Maximum number of context chunks to include (default ``MAX_CHUNKS`` = 8).
    """

    def __init__(self, max_chunks: int = MAX_CHUNKS) -> None:
        self._max_chunks = max_chunks

    # ── Public ────────────────────────────────────────────────────────────────

    @staticmethod
    def system_prompt() -> str:
        """Return the immutable system prompt string."""
        return _SYSTEM_PROMPT

    def build(
        self,
        query:      str,
        chunks:     List[Dict[str, Any]],
        amendments: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, str]:
        """Build a (system_prompt, user_message) pair.

        Parameters
        ----------
        query : str
            The original user question.
        chunks : list[dict]
            Ranked retrieval results.  Each dict must have at least
            ``content`` and ``metadata`` keys.  Only the first
            ``max_chunks`` entries are used.
        amendments : list[dict] or None
            BDC amendment dicts from ``BDCMatcher.get_amendments()``.
            When non-empty, amendment blocks are injected BEFORE the
            baseline spec chunks so the LLM treats them as authoritative.

        Returns
        -------
        (system_prompt, user_message) : tuple[str, str]
            Ready to pass directly to ``LLMClient.complete()``.
        """
        context_blocks = self._build_context(chunks, amendments or [])
        user_message   = f"{context_blocks}\n\nQuestion: {query}"
        return _SYSTEM_PROMPT, user_message

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_context(
        self,
        chunks:     List[Dict[str, Any]],
        amendments: List[Dict[str, Any]],
    ) -> str:
        """Render amendment blocks (if any) followed by baseline spec chunks."""
        parts: List[str] = []
        if amendments:
            parts.append(self._build_amendment_blocks(amendments))
            parts.append("\u2500\u2500\u2500 BASELINE SPECIFICATION \u2500\u2500\u2500")
        parts.append(self._build_baseline_blocks(chunks))
        return "\n\n".join(parts)

    def _build_baseline_blocks(self, chunks: List[Dict[str, Any]]) -> str:
        """Render baseline spec chunks as numbered context blocks."""
        blocks: List[str] = []
        for n, chunk in enumerate(chunks[: self._max_chunks], start=1):
            header  = self._chunk_header(n, chunk)
            content = (chunk.get("content") or "").strip()
            summary = (chunk.get("metadata", {}).get("context_summary") or "").strip()
            body    = f"Context: {summary}\n{content}" if summary else content
            blocks.append(f"{header}\n{body}")
        return "\n\n".join(blocks)

    @staticmethod
    def _build_amendment_blocks(amendments: List[Dict[str, Any]]) -> str:
        """Render BDC amendment blocks with clear authoritative headers."""
        blocks: List[str] = []
        for a in amendments:
            impl  = a.get("implementation_code", "R")
            label = "ROUTINE" if impl == "R" else "URGENT"
            header = (
                f"[BDC AMENDMENT \u2014 {a.get('bdc_id', '?')} \u2014 "
                f"Effective {a.get('effective_date', '?')} \u2014 {label}]"
            )
            meta = (
                f"Section {a.get('section_id', '?')} | "
                f"Amendment type: {a.get('change_type', '?')}\n"
                f"Subject: {a.get('subject', '')}"
            )
            body = (a.get("amendment_text") or "").strip()
            blocks.append(f"{header}\n{meta}\n\n{body}")
        return "\n\n".join(blocks)

    @staticmethod
    def _chunk_header(n: int, chunk: Dict[str, Any]) -> str:
        """
        Build:   [Chunk 1: Standard Specifications, Section 105.03, Page 45]
        """
        meta          = chunk.get("metadata") or {}
        doc_name      = meta.get("doc")        or chunk.get("collection") or "Unknown Document"
        section_id    = meta.get("section_id") or "?"
        page_printed  = meta.get("page_printed")
        page_str      = f"Page {page_printed}" if page_printed is not None else "Page ?"
        return f"[Chunk {n}: {doc_name}, Section {section_id}, {page_str}]"
