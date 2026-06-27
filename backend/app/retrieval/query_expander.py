"""Multi-query expansion for the NJDOT RAG pipeline.

WHY THIS EXISTS — THE EMBEDDING GAP PROBLEM
--------------------------------------------
When a user asks "What is the definition of RE?", the OpenAI embedding model
converts that question into a point in 1536-dimensional vector space.

The problem: questions embed near other *questions*. Specification prose embeds
near other *specification prose*. They live in different neighborhoods.

So when we search for "What is the definition of RE?", the nearest vectors in
our database are chunks that *sound like questions* — not the chunk that
actually says "RE: The Resident Engineer assigned by the Department...".

THE FIX — REPHRASE IN THE STYLE OF THE ANSWER
----------------------------------------------
We ask the LLM to generate 2 alternative phrasings of the query:

  1. A *spec-style phrase* — written the way the answer would appear in a
     technical specification. This embedding lands close to the answer text.
     Example: "RE Resident Engineer definition means assigned by Department"

  2. A *keyword string* — the core nouns and identifiers stripped of question
     words, tuned for BM25 keyword matching.
     Example: "RE Resident Engineer 101.03"

We then run retrieval on all 3 versions (original + 2 rephrasings) in parallel.
Results from all 3 searches are pooled and re-ranked: a chunk that appears in
multiple searches gets a higher combined score — it was relevant from different
angles, which is strong evidence it's the right answer.

HOW THE SCORING WORKS
----------------------
We use a simple form of Reciprocal Rank Fusion (RRF) across queries:

    combined_score(chunk) = sum over each query of: 1 / (60 + rank_in_that_query)

A chunk ranked #1 in one search contributes 1/(60+1) ≈ 0.016.
A chunk ranked #1 in TWO searches contributes 0.032 — double the signal.
A chunk ranked #5 in all three searches contributes 3 × 1/65 ≈ 0.046,
which is still higher than a chunk only seen in one search at rank #1.

This means multi-query consensus naturally floats the right chunks to the top
without any manual tuning of weights.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from app.generation.llm_client import LLMClient
    from app.retrieval.hybrid_ranker import HybridRanker
except ImportError:
    _pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from app.generation.llm_client import LLMClient   # type: ignore[no-redef]
    from app.retrieval.hybrid_ranker import HybridRanker  # type: ignore[no-redef]


# ── RRF constant (same value used in hybrid_ranker.py) ───────────────────────
_RRF_K = 60

# ── Prompt for generating query variants ─────────────────────────────────────
#
# We keep this prompt short and laser-focused. The LLM's job here is not to
# answer the question — it's only to rephrase it in two specific styles.
# Shorter prompt = faster, cheaper response.
#
_EXPANSION_SYSTEM = (
    "You are a search query rewriter for a construction specification database. "
    "Given a user question, output a JSON object with exactly two fields:\n"
    '  "spec_style": the question rephrased as a short phrase that sounds like '
    "text from a technical specification (no question words, imperative/declarative style)\n"
    '  "keywords": the 3-6 most important nouns and identifiers from the question, '
    "space-separated, no question words\n"
    "Output only valid JSON. No explanation."
)

# Few-shot examples embedded in the user turn to guide the model
_EXPANSION_FEW_SHOTS = [
    (
        "What is the definition of RE?",
        '{"spec_style": "RE Resident Engineer definition assigned by Department", '
        '"keywords": "RE Resident Engineer definition 101.03"}',
    ),
    (
        "How many days before paving must the Contractor submit the plan of operation?",
        '{"spec_style": "Contractor submit plan of operation days before HMA paving", '
        '"keywords": "plan operation submit days paving HMA"}',
    ),
    (
        "Can prime coat be applied to asphalt-stabilized drainage course?",
        '{"spec_style": "prime coat prohibited asphalt stabilized drainage course", '
        '"keywords": "prime coat asphalt stabilized drainage course prohibited"}',
    ),
]


class QueryExpander:
    """
    Generates rephrased variants of a query and merges multi-search results.

    Parameters
    ----------
    llm_client : LLMClient
        The same LLM client used for answer generation (reused to avoid
        opening a second API connection).
    hybrid_ranker : HybridRanker
        The retrieval component to call for each query variant.
    """

    def __init__(self, llm_client: LLMClient, hybrid_ranker: HybridRanker) -> None:
        self._llm    = llm_client
        self._ranker = hybrid_ranker

    # ── Public ────────────────────────────────────────────────────────────────

    def expand_and_search(
        self,
        query:       str,
        collection:  Optional[str] = None,
        match_count: int           = 12,
    ) -> List[Dict[str, Any]]:
        """
        Expand *query* into variants, search with each, merge results.

        Parameters
        ----------
        query : str
            The original user question.
        collection : str or None
            Restrict search to this collection (None = all).
        match_count : int
            Number of final chunks to return after merging.

        Returns
        -------
        list[dict]
            Chunks ordered by combined multi-query RRF score. Each dict has
            the same schema as HybridRanker output plus a ``_query_count``
            field showing how many query variants retrieved this chunk.
        """
        # Step 1: Generate alternative phrasings
        variants = self._generate_variants(query)
        # variants is always [original, spec_style, keywords] — 3 queries total

        # Step 2: Run retrieval for all 3 variants in parallel
        # We fetch 2× match_count per variant so the merge pool is wide enough.
        per_query_k = match_count * 2
        all_ranked_lists = self._search_all_variants(variants, collection, per_query_k)

        # Step 3: Merge using RRF across all query lists, then deduplicate by section
        merged = self._multi_rrf_merge(all_ranked_lists, match_count)

        return merged

    # ── Private ───────────────────────────────────────────────────────────────

    def _generate_variants(self, query: str) -> List[str]:
        """
        Call the LLM to produce 2 rephrased versions of *query*.

        Returns [original_query, spec_style_variant, keyword_variant].
        Falls back to [original_query] only if the LLM call fails — the
        pipeline degrades gracefully to single-query retrieval rather than
        crashing.
        """
        # Build the user message with few-shot examples so the model
        # knows exactly what format we want.
        few_shot_text = "\n\n".join(
            f"Question: {q}\nOutput: {a}" for q, a in _EXPANSION_FEW_SHOTS
        )
        user_message = f"{few_shot_text}\n\nQuestion: {query}\nOutput:"

        try:
            raw = self._llm.complete(_EXPANSION_SYSTEM, user_message)
            text = raw.strip()
            # Strip markdown code fences (```json ... ``` or ``` ... ```)
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text.strip())
            parsed = json.loads(text)
            spec_style = parsed.get("spec_style", "").strip()
            keywords   = parsed.get("keywords", "").strip()

            variants = [query]
            if spec_style and spec_style != query:
                variants.append(spec_style)
            if keywords and keywords not in variants:
                variants.append(keywords)

            logger.debug("Query variants for %r: %s", query, variants)
            return variants

        except Exception as exc:
            # If LLM or JSON parsing fails, fall back to the original query only.
            # The pipeline still works — just without expansion.
            logger.warning("Query expansion failed for %r: %s", query, exc)
            return [query]

    def _search_all_variants(
        self,
        variants:    List[str],
        collection:  Optional[str],
        per_query_k: int,
    ) -> List[List[Dict[str, Any]]]:
        """
        Run hybrid search for each variant concurrently.

        Returns a list of ranked result lists, one per variant.
        Failed searches return an empty list (not an exception) so one
        bad variant never kills the whole pipeline.
        """
        ranked_lists: List[List[Dict[str, Any]]] = [[] for _ in variants]

        # ThreadPoolExecutor lets all 3 searches run at the same time.
        # Total latency ≈ slowest single search, not sum of all three.
        with ThreadPoolExecutor(max_workers=len(variants)) as executor:
            future_to_idx = {
                executor.submit(
                    self._ranker.search, v, collection, per_query_k
                ): i
                for i, v in enumerate(variants)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    ranked_lists[idx] = future.result()
                except Exception as exc:
                    logger.warning(
                        "Search failed for variant %r: %s", variants[idx], exc
                    )

        return ranked_lists

    @staticmethod
    def _multi_rrf_merge(
        ranked_lists: List[List[Dict[str, Any]]],
        match_count:  int,
    ) -> List[Dict[str, Any]]:
        """
        Merge N ranked lists using Reciprocal Rank Fusion.

        For each chunk that appears in any list:
            score = sum of 1/(60 + rank) across every list it appears in

        Chunks found in multiple lists naturally score higher because they
        accumulate contributions from each list they appear in. This is the
        "multi-query consensus" effect — no manual weight tuning needed.

        Also deduplicates by section_id (same logic as HybridRanker) so
        continuation chunks from the same subpart don't take multiple slots.
        """
        scores:       Dict[str, float]          = {}
        data:         Dict[str, Dict[str, Any]] = {}
        query_counts: Dict[str, int]            = {}

        for ranked_list in ranked_lists:
            for rank, chunk in enumerate(ranked_list, start=1):
                cid = chunk.get("id")
                if not cid:
                    continue
                scores[cid]       = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
                query_counts[cid] = query_counts.get(cid, 0) + 1
                if cid not in data:
                    data[cid] = chunk

        sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)

        merged: List[Dict[str, Any]] = []
        seen_sections: set = set()

        for cid in sorted_ids:
            if len(merged) >= match_count:
                break
            chunk   = dict(data[cid])
            section = chunk.get("metadata", {}).get("section_id") or cid
            if section in seen_sections:
                continue
            seen_sections.add(section)
            chunk["similarity"]    = round(scores[cid], 6)
            chunk["_query_count"]  = query_counts[cid]  # how many variants found this
            merged.append(chunk)

        return merged
