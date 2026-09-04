"""Retrieval comparison test: Vector vs Hybrid (RRF).

Runs four queries — one per collection plus a section-reference query —
through both ``VectorSearcher`` and ``HybridRanker`` and prints a
side-by-side compact comparison for each.

Both searchers share one Supabase client and one OpenAI client so there
are no redundant handshakes.

Usage
-----
    python scripts/test_retrieval.py          # from backend/ directory
    python -m scripts.test_retrieval

Queries
-------
1. "What are the requirements for proposal bond?"    → specs_2019      (semantic)
2. "What is the purpose of MP1-25?"                 → material_procs  (semantic)
3. "What are the scheduling requirements for a contractor?" → scheduling (semantic)
4. "section 105.03"                                 → specs_2019      (keyword-heavy)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Ensure backend/ package root is on sys.path ──────────────────────────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config                          import config
from app.database                        import get_db
from app.retrieval.vector_search         import VectorSearcher
from app.retrieval.bm25_search           import KeywordSearcher
from app.retrieval.hybrid_ranker         import HybridRanker, classify_query
from app.generation.llm_client           import LLMClient
from app.generation.prompt_builder       import PromptBuilder
from app.generation.citation_serializer  import CitationSerializer


# ── Test queries ─────────────────────────────────────────────────────────────

_TESTS: list[dict[str, Any]] = [
    {
        "label":      "Proposal bond requirements",
        "query":      "What are the requirements for proposal bond?",
        "collection": "specs_2019",
    },
    {
        "label":      "Purpose of MP1-25",
        "query":      "What is the purpose of MP1-25?",
        "collection": "material_procs",
    },
    {
        "label":      "Contractor scheduling requirements",
        "query":      "What are the scheduling requirements for a contractor?",
        "collection": "scheduling",
    },
    {
        "label":      "Section reference lookup",
        "query":      "section 105.03",
        "collection": "specs_2019",
    },
]


# ── Display helpers ───────────────────────────────────────────────────────────

_BOX_W = 66   # inner width of each result box line


def _result_line(rank: int, result: Dict[str, Any], score_label: str = "sim") -> str:
    """
    Format one result as a compact single line:
      #1 sim=0.5080  SECTION 151 · PERFORMANCE BOND  p90
    """
    meta          = result.get("metadata", {})
    score         = result.get("similarity", 0.0)
    section_id    = meta.get("section_id", "?")
    section_title = meta.get("section_title", "?")
    page_printed  = meta.get("page_printed", "?")
    doc           = meta.get("doc", "?")

    # Truncate title so the line stays readable
    title_max = 32
    title = section_title if len(section_title) <= title_max else section_title[:title_max - 1] + "…"

    # For material_procs the "section_id" IS the doc name already; avoid duplication
    if section_id == doc:
        label = f"{section_id} · {title}"
    else:
        label = f"{section_id} · {title}"

    return f"  #{rank} {score_label}={score:.4f}  {label}  p{page_printed}"


def _print_comparison(
    query:      str,
    collection: str,
    v_results:  List[Dict[str, Any]],
    h_results:  List[Dict[str, Any]],
    v_ms:       float,
    h_ms:       float,
    top_k:      int,
    v_weight:   float,
    k_weight:   float,
    qtype:      str,
) -> None:
    """Print a two-block comparison (VECTOR then HYBRID) for one query."""
    w = _BOX_W

    # ── VECTOR block ──────────────────────────────────────────────────────────
    header_v = f" VECTOR  ({len(v_results)} hits, {v_ms:.0f}ms) "
    print(f"  ┌─{header_v:─<{w}}┐")
    if v_results:
        for rank, r in enumerate(v_results[:top_k], start=1):
            line = _result_line(rank, r, score_label="sim")
            print(f"  │  {line:<{w - 2}}│")
    else:
        print(f"  │  {'⚠️  No results':^{w - 2}}│")
    print(f"  └{'─' * (w + 2)}┘")

    print()

    # ── HYBRID block ─────────────────────────────────────────────────────────
    header_h = f" HYBRID RRF  ({len(h_results)} hits, {h_ms:.0f}ms)  v={v_weight:.1f}  k={k_weight:.1f}  [{qtype}] "
    print(f"  ┌─{header_h:─<{w}}┐")
    if h_results:
        for rank, r in enumerate(h_results[:top_k], start=1):
            # Mark chunks present in both lists
            h_id = r.get("id")
            in_vector = any(vr.get("id") == h_id for vr in v_results[:top_k])
            flag = " ★" if in_vector else "  "
            line = _result_line(rank, r, score_label="rrf") + flag
            print(f"  │  {line:<{w - 2}}│")
    else:
        print(f"  │  {'⚠️  No results':^{w - 2}}│")
    print(f"  └{'─' * (w + 2)}┘")
    print("        ★ = also in vector top-3")


# ── Main test runner ──────────────────────────────────────────────────────────

def run_tests(
    match_count: int   = 10,
    threshold:   float = 0.3,
    top_k:       int   = 3,
) -> None:
    """
    Run all test queries through both VectorSearcher and HybridRanker.

    Parameters
    ----------
    match_count : int
        Max results fetched from each searcher (default 10).
    threshold : float
        Minimum cosine similarity for vector search (default 0.3).
    top_k : int
        How many results to display per query (default 3).
    """
    if not config.validate():
        sys.exit(1)

    # Shared resources — one DB handshake, one OpenAI client
    db_client = get_db()
    vector    = VectorSearcher(db_client=db_client)
    keyword   = KeywordSearcher(db_client=db_client)
    hybrid    = HybridRanker(vector_searcher=vector, keyword_searcher=keyword)

    total = len(_TESTS)

    print()
    print("═" * 72)
    print("🔍  NJDOT Retrieval Comparison  —  Vector  vs  Hybrid RRF")
    print(f"    match_count={match_count}  threshold={threshold}  top_k={top_k}")
    print("═" * 72)

    for i, test in enumerate(_TESTS, start=1):
        label      = test["label"]
        query      = test["query"]
        collection = test["collection"]

        v_weight, k_weight, qtype = classify_query(query)

        print()
        print(f"── Query {i}/{total}: {label}  {'─' * max(0, 52 - len(label))}")
        print(f"   query      : {query!r}")
        print(f"   collection : {collection!r}")
        print(f"   query type : {qtype}  →  vector={v_weight:.1f}  keyword={k_weight:.1f}")
        print()

        # Vector search
        t0 = time.time()
        v_results = vector.search(
            query,
            collection=collection,
            match_count=match_count,
            threshold=threshold,
        )
        v_ms = (time.time() - t0) * 1000

        # Hybrid search  (vector embedding already cached in-process via OpenAI
        # — the _embed_query call will re-embed, which is a single fast API call)
        t0 = time.time()
        h_results = hybrid.search(
            query,
            collection=collection,
            match_count=match_count,
        )
        h_ms = (time.time() - t0) * 1000

        _print_comparison(
            query, collection,
            v_results, h_results,
            v_ms, h_ms,
            top_k, v_weight, k_weight, qtype,
        )
        print()

    print("═" * 72)
    print("✅  Retrieval comparison complete.\n")


# ── End-to-end generation test ────────────────────────────────────────────────

_E2E_QUERY      = "What are the requirements for proposal bond?"
_E2E_COLLECTION = "specs_2019"


def run_generation_test(match_count: int = 5) -> None:
    """
    End-to-end smoke test: hybrid retrieval → prompt → LLM → citation parse.

    Uses the proposal-bond query against ``specs_2019`` as a realistic
    fixture.  Prints the answer and all validated citations.

    Parameters
    ----------
    match_count : int
        Number of chunks to retrieve and pass as context (default 5).
    """
    if not config.validate():
        sys.exit(1)

    # ── Shared resources ──────────────────────────────────────────────────────
    db_client  = get_db()
    vector     = VectorSearcher(db_client=db_client)
    keyword    = KeywordSearcher(db_client=db_client)
    hybrid     = HybridRanker(vector_searcher=vector, keyword_searcher=keyword)
    builder    = PromptBuilder()
    llm        = LLMClient()
    serializer = CitationSerializer()

    print()
    print("═" * 72)
    print("🤖  NJDOT End-to-End Generation Test")
    print(f"    model={llm.model}  chunks={match_count}  collection={_E2E_COLLECTION!r}")
    print("═" * 72)
    print()
    print(f"  Query: {_E2E_QUERY!r}")
    print()

    # ── Step 1: Retrieve ──────────────────────────────────────────────────────
    t0     = time.time()
    chunks = hybrid.search(_E2E_QUERY, collection=_E2E_COLLECTION, match_count=match_count)
    ret_ms = (time.time() - t0) * 1000
    print(f"  ── Retrieval  ({len(chunks)} chunks, {ret_ms:.0f}ms) ──────────────────────────")
    for i, c in enumerate(chunks, start=1):
        meta = c.get("metadata") or {}
        sid  = meta.get("section_id", "?")
        ttl  = (meta.get("section_title") or "")[:40]
        pg   = meta.get("page_printed", "?")
        rrf  = c.get("similarity", 0.0)
        print(f"    [{i}] rrf={rrf:.4f}  {sid} — {ttl}  p{pg}")
    print()

    # ── Step 2: Build prompt ──────────────────────────────────────────────────
    system_prompt, user_message = builder.build(_E2E_QUERY, chunks)
    ctx_lines = user_message.count("\n") + 1
    print(f"  ── Prompt built  ({ctx_lines} lines in user message) ─────────────────────")
    print()

    # ── Step 3: Call LLM ──────────────────────────────────────────────────────
    print(f"  ── Calling {llm.model} … ", end="", flush=True)
    t0     = time.time()
    raw    = llm.complete(system_prompt, user_message)
    llm_ms = (time.time() - t0) * 1000
    print(f"done  ({llm_ms:.0f}ms)")
    print()

    # ── Step 4: Parse + validate citations ───────────────────────────────────
    result = serializer.serialize(raw, chunks)
    print("  ── Result ───────────────────────────────────────────────────────────")

    if result.get("parse_error"):
        print("  ⚠️  JSON parse failed — raw LLM output:")
        print(f"  {raw}")
    else:
        answer    = result["answer"]
        citations = result["citations"]

        # Wrap answer at ~70 chars for readability
        print("  ANSWER:")
        for line in _wrap_text(answer, width=68, indent="    "):
            print(line)
        print()

        if citations:
            print(f"  CITATIONS ({len(citations)}):")
            for i, cit in enumerate(citations, start=1):
                verified = "✅" if cit.get("verified") else "⚠️ unverified"
                doc      = cit.get("document") or "?"
                sec      = cit.get("section")  or "?"
                pg_p     = cit.get("page_printed")
                pg_pdf   = cit.get("page_pdf")
                cid      = (cit.get("chunk_id") or "")[:8]
                pg_str   = f"p{pg_p}" if pg_p is not None else "p?"
                if pg_pdf is not None:
                    pg_str += f" (PDF p{pg_pdf})"
                print(f"    [{i}] {verified}  {sec} — {doc},  {pg_str}  chunk={cid}…")
        else:
            print("  CITATIONS: (none)")

    print()
    print("═" * 72)
    print("✅  Generation test complete.\n")


def _wrap_text(text: str, width: int = 70, indent: str = "") -> List[str]:
    """Naive word-wrap: split on spaces, respecting newlines in text."""
    lines: List[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append(indent)
            continue
        words  = paragraph.split()
        current: List[str] = []
        for word in words:
            test = " ".join(current + [word])
            if len(test) > width and current:
                lines.append(indent + " ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(indent + " ".join(current))
    return lines


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_tests()
    run_generation_test()
