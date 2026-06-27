"""Hybrid retrieval with Reciprocal Rank Fusion for NJDOT chunks.

Combines vector similarity search (``VectorSearcher``) and BM25-style
keyword search (``KeywordSearcher``) using Reciprocal Rank Fusion (RRF),
with automatic weight selection based on the query type.

Query-type classification
--------------------------
The query string is inspected with two compiled regexes:

``_SECTION_NUM_RE``
    Matches patterns like ``421.03``, ``102.01``, ``SECTION 101``,
    ``section 105.03``.  These are precise document references where
    keyword recall is critical.

``_KEYWORD_TERMS_RE``
    Matches AASHTO standards, rebar grades (``#57``), millimetre
    measurements (``19mm``), Table/Figure references, ASTM codes, and
    AASHTO T-number designations (``T 176``).

When either regex fires → **keyword-heavy** weights (vector=0.3, keyword=0.7).
Otherwise → **semantic** weights (vector=0.6, keyword=0.4).

Reciprocal Rank Fusion
-----------------------
For each candidate chunk *d* found in either result list:

    rrf_score(d) = v_weight × 1/(k + rank_v(d))
                 + k_weight × 1/(k + rank_k(d))

where *k* = 60 (standard smoothing constant), ``rank_v`` is the 1-based
position in the vector result list (∞ if absent), and ``rank_k`` is the
1-based position in the keyword result list (∞ if absent).

The merged list is sorted descending by ``rrf_score`` and truncated to
``match_count``.  The ``similarity`` field in each returned dict contains
the ``rrf_score`` so downstream code can apply a score threshold.

Parallel execution
------------------
Both sub-searches run concurrently in a ``ThreadPoolExecutor`` with
``max_workers=2``.  This is appropriate because both calls are I/O-bound
(network calls to OpenAI and Supabase).  Total latency ≈ max(vector_ms,
keyword_ms) instead of their sum.

Result dict schema
------------------
Identical to ``VectorSearcher`` output:

    id          str   – UUID of the row in ``chunks``
    content     str   – chunk text
    metadata    dict  – {doc, section_id, section_title, division,
                         page_pdf, page_printed, kind}
    similarity  float – RRF score (higher = better combined rank)
    collection  str   – "specs_2019" | "scheduling" | "material_procs"

Usage
-----
    from app.retrieval.hybrid_ranker import HybridRanker, classify_query

    ranker = HybridRanker()

    v_w, k_w, label = classify_query("What is section 105.03?")
    # → (0.3, 0.7, "keyword-heavy")

    results = ranker.search(
        "section 105.03",
        collection="specs_2019",
        match_count=5,
    )
    for r in results:
        print(r["similarity"], r["metadata"]["section_id"])
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# ── Package-root import shim ─────────────────────────────────────────────────
try:
    from app.config   import config
    from app.database import get_db
    from app.retrieval.vector_search import VectorSearcher
    from app.retrieval.bm25_search   import KeywordSearcher
except ImportError:
    _pkg_root = str(Path(__file__).resolve().parent.parent.parent)
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from app.config   import config          # type: ignore[no-redef]
    from app.database import get_db          # type: ignore[no-redef]
    from app.retrieval.vector_search import VectorSearcher  # type: ignore[no-redef]
    from app.retrieval.bm25_search   import KeywordSearcher # type: ignore[no-redef]


# ── Query-type classification patterns ───────────────────────────────────────

# Subsection numbers like "421.03", "102.01" or explicit "SECTION 101"
_SECTION_NUM_RE = re.compile(
    r'\b\d{3}\.\d{2}\b'          # subsection: 421.03, 102.01
    r'|'
    r'\bsection\s+\d+\b',        # section 101, SECTION 105
    re.IGNORECASE,
)

# Exact technical terms that are better served by keyword recall
_KEYWORD_TERMS_RE = re.compile(
    r'\bAASHTO\b'                 # AASHTO standard references
    r'|'
    r'#\s*\d+'                    # aggregate grades: #57, #4, #10
    r'|'
    r'\b\d+\s*mm\b'               # metric measurements: 19mm, 25 mm
    r'|'
    r'\bTable\s+\d+'              # Table 5, Table 10
    r'|'
    r'\bFigure\s+\d+'             # Figure 3, Figure A-2
    r'|'
    r'\bASTM\b'                   # ASTM standard codes
    r'|'
    r'\bT\s*\d{2,3}\b'            # AASHTO T numbers: T 176, T99
    r'|'
    r'\bM\s*\d{2,3}\b'            # AASHTO M numbers: M 85, M182
    r'|'
    r'\bNJDOT\s+[A-Z]-\d'         # NJDOT test methods: NJDOT B-10, NJDOT R-1
    r'|'
    r'\bR\s*\d{2,3}\b',           # AASHTO R numbers: R 30, R62
    re.IGNORECASE,
)

# NJDOT-domain acronyms and codes that appear verbatim in specs.
# These give keyword search an edge over pure semantic embedding but are not
# as pinpoint as section numbers or AASHTO/ASTM codes, so they get a middle
# weight tier rather than the full keyword-heavy split.
_NJDOT_DOMAIN_RE = re.compile(
    r'\bHMA\b'                    # Hot Mix Asphalt
    r'|'
    r'\bIRI\b'                    # International Roughness Index
    r'|'
    r'\bJMF\b'                    # Job Mix Formula
    r'|'
    r'\bRAP\b'                    # Reclaimed Asphalt Pavement
    r'|'
    r'\bRAS\b'                    # Reclaimed Asphalt Shingles
    r'|'
    r'\bQPL\b'                    # Qualified Products List
    r'|'
    r'\bPPA\b'                    # Percent Pay Adjustment
    r'|'
    r'\bMUTCD\b'                  # Manual on Uniform Traffic Control Devices
    r'|'
    r'\bCPM\b'                    # Critical Path Method (scheduling)
    r'|'
    r'\bDBE\b'                    # Disadvantaged Business Enterprise
    r'|'
    r'\bPG\s+\d{2}'               # Performance Grade binder: PG 64-22, PG 76-22
    r'|'
    r'\bQC/QA\b|\bQC\b|\bQA\b',   # Quality Control / Quality Assurance
    re.IGNORECASE,
)

# Weight tuples: (vector_weight, keyword_weight)
_WEIGHTS_KEYWORD_HEAVY: Tuple[float, float] = (0.3, 0.7)
_WEIGHTS_MIXED:         Tuple[float, float] = (0.45, 0.55)
_WEIGHTS_SEMANTIC:      Tuple[float, float] = (0.6, 0.4)

# RRF smoothing constant (standard value from Cormack et al. 2009)
_RRF_K: int = 60

# How many candidates to gather from each sub-searcher before merging.
# 5× gives enough breadth for section deduplication to work — after removing
# continuation chunks from the same section_id we still have ample candidates.
_POOL_MULTIPLIER: int = 5


# ── Public helper ─────────────────────────────────────────────────────────────

def classify_query(query: str) -> Tuple[float, float, str]:
    """
    Inspect *query* and return the recommended retrieval weights.

    Returns
    -------
    (vector_weight, keyword_weight, label)
        ``label`` is ``"keyword-heavy"``, ``"mixed"``, or ``"semantic"``.

    Examples
    --------
    >>> classify_query("section 105.03")
    (0.3, 0.7, 'keyword-heavy')
    >>> classify_query("AASHTO T 176 requirements")
    (0.3, 0.7, 'keyword-heavy')
    >>> classify_query("What are the HMA pay adjustment requirements?")
    (0.45, 0.55, 'mixed')
    >>> classify_query("What are the requirements for proposal bond?")
    (0.6, 0.4, 'semantic')
    """
    if _SECTION_NUM_RE.search(query) or _KEYWORD_TERMS_RE.search(query):
        v, k = _WEIGHTS_KEYWORD_HEAVY
        return v, k, "keyword-heavy"
    if _NJDOT_DOMAIN_RE.search(query):
        v, k = _WEIGHTS_MIXED
        return v, k, "mixed"
    v, k = _WEIGHTS_SEMANTIC
    return v, k, "semantic"


# ── Main class ────────────────────────────────────────────────────────────────

class HybridRanker:
    """
    Merge vector and keyword results with weighted Reciprocal Rank Fusion.

    Parameters
    ----------
    api_key : str or None
        OpenAI API key for the embedding call.  ``None`` → read from config.
    db_client : supabase.Client or None
        Shared Supabase client.  ``None`` → obtain from ``get_db()``.
    vector_searcher : VectorSearcher or None
        Pre-built vector searcher to reuse (avoids a second OpenAI client).
        When supplied, ``api_key`` is ignored for vector search.
    keyword_searcher : KeywordSearcher or None
        Pre-built keyword searcher to reuse.
    """

    def __init__(
        self,
        api_key:          Optional[str]            = None,
        db_client:        Optional[Any]            = None,
        vector_searcher:  Optional[VectorSearcher] = None,
        keyword_searcher: Optional[KeywordSearcher] = None,
    ) -> None:
        # If a shared db_client is supplied, pass it through to any searchers
        # we create ourselves (avoids a second Supabase handshake).
        _db = db_client if db_client is not None else get_db()

        self._vector  = (
            vector_searcher
            if vector_searcher is not None
            else VectorSearcher(api_key=api_key, db_client=_db)
        )
        self._keyword = (
            keyword_searcher
            if keyword_searcher is not None
            else KeywordSearcher(db_client=_db)
        )

    # ── Public ────────────────────────────────────────────────────────────────

    def search(
        self,
        query:       str,
        collection:  Optional[str] = None,
        match_count: int           = 15,
        debug:       bool          = False,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], str]]:
        """
        Run hybrid retrieval and return RRF-merged results.

        Parameters
        ----------
        query : str
            Natural-language question or keyword phrase.
        collection : str or None
            Restrict results to this collection.  ``None`` → all collections.
        match_count : int
            Number of top results to return (default 15).
        debug : bool
            When ``True``, each chunk dict gains ``_vector_rank`` and
            ``_keyword_rank`` keys (1-based; ``None`` if absent from that
            result list), and the return value is a
            ``(chunks, bm25_cleaned_query)`` tuple instead of just the list.
            When ``False`` (default) behaviour is identical to before.

        Returns
        -------
        list[dict]
            When ``debug=False``: sorted by descending RRF score.  Each dict
            has: ``id``, ``content``, ``metadata``, ``similarity``,
            ``collection``.  The ``similarity`` field holds the weighted RRF
            score.
        tuple[list[dict], str]
            When ``debug=True``: ``(chunks, bm25_cleaned_query)`` where each
            chunk also carries ``_vector_rank`` / ``_keyword_rank``.
        """
        v_weight, k_weight, _label = classify_query(query)
        pool = max(match_count * _POOL_MULTIPLIER, 20)

        # ── Run both searches concurrently ────────────────────────────────────
        with ThreadPoolExecutor(max_workers=2) as executor:
            v_future: Future = executor.submit(
                self._vector.search,
                query,
                collection,
                pool,
                0.0,          # no threshold; let RRF decide
            )
            if debug:
                k_future: Future = executor.submit(
                    self._keyword.search_with_debug,
                    query,
                    collection,
                    pool,
                )
            else:
                k_future = executor.submit(
                    self._keyword.search,
                    query,
                    collection,
                    pool,
                )
            v_results: List[Dict[str, Any]] = v_future.result()
            k_raw = k_future.result()

        if debug:
            k_results, bm25_cleaned_query = k_raw
            chunks = self._rrf_merge(
                v_results, k_results, v_weight, k_weight, match_count, debug=True
            )
            return chunks, bm25_cleaned_query

        return self._rrf_merge(v_results, k_raw, v_weight, k_weight, match_count)

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _rrf_merge(
        v_results:   List[Dict[str, Any]],
        k_results:   List[Dict[str, Any]],
        v_weight:    float,
        k_weight:    float,
        match_count: int,
        debug:       bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Merge two ranked lists using weighted Reciprocal Rank Fusion.

        For each unique chunk *d*:

            score(d) = v_weight × 1/(_RRF_K + rank_v(d))
                     + k_weight × 1/(_RRF_K + rank_k(d))

        Chunks that appear in only one list still receive a partial score.

        When ``debug=True``, each returned dict also carries:
            ``_vector_rank``  – 1-based rank in *v_results*; ``None`` if absent.
            ``_keyword_rank`` – 1-based rank in *k_results*; ``None`` if absent.
        """
        scores:   Dict[str, float]          = {}
        data:     Dict[str, Dict[str, Any]] = {}
        v_ranks:  Dict[str, int]            = {}   # rid → 1-based vector rank
        k_ranks:  Dict[str, int]            = {}   # rid → 1-based keyword rank

        for rank, result in enumerate(v_results, start=1):
            rid = result["id"]
            if rid is None:
                continue
            scores[rid] = scores.get(rid, 0.0) + v_weight * (1.0 / (_RRF_K + rank))
            if rid not in data:
                data[rid] = result
            v_ranks[rid] = rank

        for rank, result in enumerate(k_results, start=1):
            rid = result["id"]
            if rid is None:
                continue
            scores[rid] = scores.get(rid, 0.0) + k_weight * (1.0 / (_RRF_K + rank))
            if rid not in data:
                data[rid] = result
            k_ranks[rid] = rank

        # Sort by descending RRF score, then deduplicate by section_id so that
        # continuation chunks from the same subpart don't consume multiple slots.
        # The highest-ranked chunk per section_id is kept; lower-ranked siblings
        # are skipped unless we run out of unique sections before match_count.
        sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)

        merged: List[Dict[str, Any]] = []
        seen_sections: set = set()
        for rid in sorted_ids:
            if len(merged) >= match_count:
                break
            result   = dict(data[rid])
            section  = result.get("metadata", {}).get("section_id") or rid
            if section in seen_sections:
                continue
            seen_sections.add(section)
            result["similarity"] = round(scores[rid], 6)
            if debug:
                result["_vector_rank"]  = v_ranks.get(rid)
                result["_keyword_rank"] = k_ranks.get(rid)
            merged.append(result)

        return merged


# ── Convenience wrapper ───────────────────────────────────────────────────────

def hybrid_search_chunks(
    query:       str,
    collection:  Optional[str] = None,
    match_count: int           = 15,
) -> List[Dict[str, Any]]:
    """
    One-shot helper: instantiate ``HybridRanker`` and run a single search.

    Parameters
    ----------
    query : str
        Natural-language or keyword search phrase.
    collection : str or None
        Optional collection filter.
    match_count : int
        Maximum results (default 15).

    Returns
    -------
    list[dict]
        RRF-merged chunks ordered by descending hybrid score.
    """
    return HybridRanker().search(
        query,
        collection=collection,
        match_count=match_count,
    )
