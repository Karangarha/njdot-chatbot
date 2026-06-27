"""NJDOT RAG pipeline evaluation script.

Runs the full pipeline (hybrid retrieval → prompt → LLM → citation parse)
for every question in the eval set and scores each answer using an LLM judge.

Usage
-----
    # From backend/ directory:
    python scripts/run_eval.py
    python scripts/run_eval.py
    python scripts/run_eval.py --dry-run                          # first 5 questions only
    python scripts/run_eval.py --category table_lookup            # one category only
    python scripts/run_eval.py --ids 1,5,15,28                   # specific IDs only
    python scripts/run_eval.py --collection specs_2019_v2         # target a specific collection
    python scripts/run_eval.py --dry-run --category semantic

Scoring rules
-------------
* insufficient_evidence category : correct iff answer contains "Insufficient evidence"
* All other categories            : LLM judge (gpt-4o-mini) rates correct/incorrect

Output
------
* Live progress line per question printed to stdout
* Full summary (overall + by category + failure list) at the end
* Detailed results saved to backend/data/eval/results_latest.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Ensure backend/ package root is on sys.path ──────────────────────────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.config                         import config
from app.database                       import get_db
from app.retrieval.vector_search        import VectorSearcher
from app.retrieval.bm25_search          import KeywordSearcher
from app.retrieval.hybrid_ranker        import HybridRanker
from app.retrieval.query_expander       import QueryExpander
from app.generation.llm_client          import LLMClient
from app.generation.prompt_builder      import PromptBuilder
from app.generation.citation_serializer import CitationSerializer


# ── Constants ─────────────────────────────────────────────────────────────────

_EVAL_JSON   = _BACKEND / "data" / "eval" / "njdot_eval_set_100_questions.json"
_RESULTS_OUT = _BACKEND / "data" / "eval" / "results_latest.json"

_RETRIEVE_K: int = 15  # raised for better recall

# The exact phrase the system prompt instructs the LLM to use when it cannot answer.
_INSUF_MARKER = "Insufficient evidence"

# Category display order for the summary table
_CATEGORY_ORDER = [
    "table_lookup",
    "section_reference",
    "exact_numeric",
    "semantic",
    "footnote_dependent",
    "multi_section",
    "insufficient_evidence",
]

_SEP = "=" * 60


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(
    query:      str,
    expander:   "QueryExpander",
    builder:    PromptBuilder,
    llm:        LLMClient,
    serializer: CitationSerializer,
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the full improved pipeline (multi-query expansion + cross-ref)."""
    import re as _re
    _XREF_RE = _re.compile(r'\b(\d{3}\.\d{2}(?:\.\d{2})?)\b')

    # Step 1: Multi-query expansion
    chunks = expander.expand_and_search(query, collection=collection, match_count=_RETRIEVE_K)

    # Step 1.5: Cross-reference follow-up
    already = {c.get("metadata", {}).get("section_id") for c in chunks}
    xref_ids = []
    for c in chunks:
        text = c.get("content", "")
        for sec in _XREF_RE.findall(text):
            if sec not in already and sec not in xref_ids:
                xref_ids.append(sec)
    hybrid_inst = expander._ranker
    for sec in xref_ids[:3]:
        xref_q = f"Section {sec} specifications requirements"
        xref_chunks = hybrid_inst.search(xref_q, collection=collection, match_count=3)
        for xc in xref_chunks:
            if xc.get("metadata", {}).get("section_id") == sec:
                chunks.append(xc)
                already.add(sec)
                break

    chunks = chunks[:_RETRIEVE_K]
    system_prompt, user_message = builder.build(query, chunks)
    raw_response = llm.complete(system_prompt, user_message)
    return serializer.serialize(raw_response, chunks)


# ── LLM judge ─────────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def _judge_answer(
    question:      str,
    gold_answer:   str,
    system_answer: str,
    judge_llm:     LLMClient,
) -> Dict[str, Any]:
    """
    Call the LLM judge and return {"correct": bool, "reason": str}.

    Uses LLMClient.complete() so it shares the same underlying OpenAI client
    as the pipeline and benefits from its error handling.
    """
    judge_system = (
        "You are an expert evaluator for NJDOT construction specifications. "
        "Reply with JSON only — no markdown, no extra text."
    )
    judge_user = (
        f"Question: {question}\n\n"
        f"Gold Answer: {gold_answer}\n\n"
        f"System Answer: {system_answer}\n\n"
        "Is the system answer correct? It is correct if it contains the key "
        "facts from the gold answer, even if worded differently. Minor omissions "
        "are acceptable; wrong numbers or missing critical values are not.\n\n"
        'Reply with JSON only: {"correct": true/false, "reason": "brief explanation"}'
    )
    try:
        raw = judge_llm.complete(judge_system, judge_user)
        cleaned = _strip_fences(raw)
        parsed = json.loads(cleaned)
        return {
            "correct": bool(parsed.get("correct", False)),
            "reason":  str(parsed.get("reason", "")),
        }
    except Exception as exc:
        return {
            "correct": False,
            "reason":  f"Judge error [{type(exc).__name__}]: {exc}",
        }


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_answer(
    category:      str,
    system_answer: str,
    question:      str,
    gold_answer:   str,
    judge_llm:     LLMClient,
) -> Dict[str, Any]:
    """
    Score one answer.  Returns {"correct": bool, "reason": str, "method": str}.

    insufficient_evidence → rule-based string match
    all other categories  → LLM judge
    """
    if category == "insufficient_evidence":
        correct = _INSUF_MARKER in system_answer
        return {
            "correct": correct,
            "reason": (
                f"Answer contains '{_INSUF_MARKER}'" if correct
                else f"Expected '{_INSUF_MARKER}' not found in answer"
            ),
            "method": "rule",
        }

    verdict = _judge_answer(question, gold_answer, system_answer, judge_llm)
    return {
        "correct": verdict["correct"],
        "reason":  verdict["reason"],
        "method":  "llm_judge",
    }


# ── Display helpers ───────────────────────────────────────────────────────────

def _truncate(s: str, max_len: int = 55) -> str:
    return s if len(s) <= max_len else s[:max_len - 1] + "…"


def _progress_line(
    q_id:      int,
    correct:   bool,
    question:  str,
    elapsed_ms: int,
) -> str:
    icon   = "✅" if correct else "❌"
    status = "CORRECT  " if correct else "INCORRECT"
    label  = _truncate(question)
    return f"Q{q_id:<3} {icon} {status} - {label}  ({elapsed_ms}ms)"


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(
    results:  List[Dict[str, Any]],
    skipped:  int,
    failures: List[Dict[str, Any]],
) -> None:
    total   = len(results)
    correct = sum(1 for r in results if r["correct"])
    wrong   = total - correct
    acc     = correct / total * 100 if total else 0.0

    cat_correct: Dict[str, int] = defaultdict(int)
    cat_total:   Dict[str, int] = defaultdict(int)
    for r in results:
        cat = r["category"]
        cat_total[cat] += 1
        if r["correct"]:
            cat_correct[cat] += 1

    print()
    print(_SEP)
    print("EVAL RESULTS")
    print(_SEP)
    print(f"Total:              {total} questions ({skipped} skipped)")
    print(f"Correct:            {correct}")
    print(f"Incorrect:          {wrong}")
    print(f"Accuracy:           {acc:.1f}%")
    print()
    print("By category:")
    for cat in _CATEGORY_ORDER:
        if cat not in cat_total:
            continue
        c   = cat_correct[cat]
        t   = cat_total[cat]
        pct = c / t * 100 if t else 0.0
        print(f"  {cat:<28} {c}/{t:<5} ({pct:.1f}%)")

    if failures:
        print()
        print("Failures:")
        for r in failures:
            label = _truncate(r["question"])
            print(f"  Q{r['id']:<3} - {label}")

    print(_SEP)
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NJDOT RAG pipeline evaluation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/run_eval.py\n"
            "  python scripts/run_eval.py --dry-run\n"
            "  python scripts/run_eval.py --category table_lookup\n"
            "  python scripts/run_eval.py --ids 1,5,15,28\n"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only run the first 5 questions (after filters) for quick validation.",
    )
    p.add_argument(
        "--category",
        metavar="CAT",
        choices=_CATEGORY_ORDER,
        help=(
            "Only evaluate questions in this category. "
            "Choices: " + ", ".join(_CATEGORY_ORDER)
        ),
    )
    p.add_argument(
        "--ids",
        metavar="ID_LIST",
        help=(
            "Comma-separated question IDs to run, e.g. --ids 1,5,15,28. "
            "When provided, overrides --category and --dry-run."
        ),
    )
    p.add_argument(
        "--collection",
        metavar="NAME",
        default=None,
        help=(
            "Restrict retrieval to this Supabase collection, e.g. "
            "'specs_2019_v2'.  Defaults to None (search all collections)."
        ),
    )
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    # ── Config check ─────────────────────────────────────────────────────────
    if not config.validate():
        sys.exit(1)

    # ── Load eval set ─────────────────────────────────────────────────────────
    if not _EVAL_JSON.exists():
        print(f"ERROR: Eval file not found: {_EVAL_JSON}", file=sys.stderr)
        sys.exit(1)

    with _EVAL_JSON.open() as f:
        eval_data = json.load(f)

    all_questions: List[Dict[str, Any]] = eval_data["questions"]

    # ── Parse --ids (overrides all other filters when present) ────────────────
    ids_filter: Optional[set] = None
    if args.ids:
        try:
            ids_filter = {int(x.strip()) for x in args.ids.split(",") if x.strip()}
        except ValueError:
            print(
                "ERROR: --ids must be comma-separated integers, e.g. --ids 1,5,15",
                file=sys.stderr,
            )
            sys.exit(1)

    # ── Build the active question list ────────────────────────────────────────
    active: List[Dict[str, Any]] = []

    if ids_filter is not None:
        # --ids overrides --category and --dry-run; preserve eval-file order
        id_to_q = {q["id"]: q for q in all_questions}
        for qid in sorted(ids_filter):
            if qid in id_to_q:
                active.append(id_to_q[qid])
            else:
                print(f"WARNING: ID {qid} not found in eval set — skipping.", file=sys.stderr)
    else:
        for q in all_questions:
            if args.category and q["category"] != args.category:
                continue
            active.append(q)
        if args.dry_run:
            active = active[:5]

    if not active:
        print("No questions match the given filters. Exiting.")
        sys.exit(0)

    total         = len(active)
    skipped_count = 0   # no automatic skips; callers control inclusion via --ids

    # ── Header ────────────────────────────────────────────────────────────────
    # ── Build shared pipeline objects ─────────────────────────────────────────
    db_client    = get_db()
    vector       = VectorSearcher(db_client=db_client)
    keyword      = KeywordSearcher(db_client=db_client)
    hybrid       = HybridRanker(vector_searcher=vector, keyword_searcher=keyword)
    expander     = QueryExpander(llm_client=LLMClient(), hybrid_ranker=hybrid)
    builder      = PromptBuilder()
    pipeline_llm = LLMClient()
    judge_llm    = LLMClient()
    serializer   = CitationSerializer()

    print()
    print(_SEP)
    print("NJDOT RAG Pipeline Evaluation")
    print(_SEP)
    print(f"Eval file        : {_EVAL_JSON.name}")
    print(f"Pipeline model   : {pipeline_llm.model}")
    print(f"Judge model      : {judge_llm.model}")
    print(f"Collection       : {args.collection or '(all)'}")
    print(f"Questions to run : {total}")
    if ids_filter is not None:
        print(f"IDs filter       : {', '.join(str(i) for i in sorted(ids_filter))}")
    else:
        if args.dry_run:
            print("Mode             : DRY RUN (first 5 questions)")
        if args.category:
            print(f"Category filter  : {args.category}")
    print(_SEP)
    print()

    # ── Evaluate ──────────────────────────────────────────────────────────────
    results:  List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for q in active:
        q_id        = q["id"]
        category    = q["category"]
        question    = q["query"]
        gold_answer = q["gold_answer"]

        t_start        = time.time()
        pipeline_error: Optional[str] = None
        system_answer  = ""

        # ── Run pipeline ──────────────────────────────────────────────────
        try:
            result        = run_pipeline(question, expander, builder, pipeline_llm, serializer,
                                         collection=args.collection)
            system_answer = result.get("answer", "")
        except Exception as exc:
            pipeline_error = f"{type(exc).__name__}: {exc}"
            system_answer  = ""

        # ── Score ─────────────────────────────────────────────────────────
        if pipeline_error:
            verdict: Dict[str, Any] = {
                "correct": False,
                "reason":  f"Pipeline error: {pipeline_error}",
                "method":  "error",
            }
        else:
            verdict = score_answer(
                category, system_answer, question, gold_answer, judge_llm
            )

        elapsed_ms = int((time.time() - t_start) * 1000)

        # ── Progress line ─────────────────────────────────────────────────
        print(_progress_line(q_id, verdict["correct"], question, elapsed_ms))

        # ── Accumulate ────────────────────────────────────────────────────
        record: Dict[str, Any] = {
            "id":             q_id,
            "category":       category,
            "difficulty":     q.get("difficulty"),
            "question":       question,
            "gold_answer":    gold_answer,
            "system_answer":  system_answer,
            "correct":        verdict["correct"],
            "reason":         verdict["reason"],
            "score_method":   verdict["method"],
            "elapsed_ms":     elapsed_ms,
            "pipeline_error": pipeline_error,
        }
        results.append(record)
        if not verdict["correct"]:
            failures.append(record)

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(results, skipped_count, failures)

    # ── Save results ──────────────────────────────────────────────────────────
    n_correct = sum(1 for r in results if r["correct"])
    output: Dict[str, Any] = {
        "run_at":          datetime.now(timezone.utc).isoformat(),
        "pipeline_model":  pipeline_llm.model,
        "judge_model":     judge_llm.model,
        "total_run":       len(results),
        "correct":         n_correct,
        "accuracy":        round(n_correct / len(results) * 100, 2) if results else 0.0,
        "dry_run":         args.dry_run,
        "collection":      args.collection,
        "category_filter": args.category,
        "ids_filter":      sorted(ids_filter) if ids_filter is not None else None,
        "questions":       results,
    }

    _RESULTS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with _RESULTS_OUT.open("w") as f:
        json.dump(output, f, indent=2)

    print(f"Results saved → {_RESULTS_OUT}")
    print()


if __name__ == "__main__":
    main()
