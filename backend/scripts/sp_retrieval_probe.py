"""Per-check probe for Special Provision retrieval quality.

For every built-in compliance check with ``"sp"`` in ``source_files``
(``app.compliance.catalog.BUILTIN_CHECKS``), reports:

  check_key               the check's id
  anchor_sections/tables   what ``app.compliance.anchors.extract_anchors``
                           pulls from the check's instruction
  rows_returned            len(retrieve_for_check(...).rows)
  pinned_count             retrieve_for_check(...).pinned
  top_similarity           pure dense-search similarity of the check's own
                           instruction against this project's SP chunks
                           (retrieve_sp_chunks, match_count=1) -- computed
                           independently of pinning/fusion so it is a fair
                           before/after comparison point even when the top
                           row of the full pipeline came from a pin, which
                           carries no similarity score of its own
  anchor_in_top_passage    whether rows[0] actually carries the named
                           section_id/table (metadata first, falling back to
                           a literal content search for a project with no
                           section metadata at all)

``--repeat N`` reruns ``retrieve_for_check`` N times per check and reports
whether the top row's id is identical on every run -- the stability signal
the design's second success criterion asks for. Pinning is an exact SQL
metadata lookup, so it should be exactly reproducible; the fused (non-
pinned) remainder is ranked, so it need not be.

Every run is written to ``backend/data/eval/sp_retrieval_probe.<label>.json``.
``--compare A B`` loads two labelled runs and prints a per-check diff.

No LLM calls -- retrieve_for_check embeds and reads the database only.

Usage
-----
    cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe \\
        scripts/sp_retrieval_probe.py --project-id <id> --label before-hybrid --repeat 5

    cd backend && PYTHONIOENCODING=utf-8 ../../../../.venv/Scripts/python.exe \\
        scripts/sp_retrieval_probe.py --compare before-hybrid after-hybrid
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Ensure backend/ package root is on sys.path ──────────────────────────────
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Local-container env: backend/.env first, then backend/local.env.local with
# override=True -- same convention as tests/integration/conftest.py -- done
# BEFORE importing anything from app.* so app.config's own (weaker) default
# .env/.env.local loading sees the already-overridden values.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_BACKEND / ".env")
load_dotenv(_BACKEND / "local.env.local", override=True)

from app.compliance.anchors import Anchors, extract_anchors  # noqa: E402
from app.compliance.catalog import BUILTIN_CHECKS, CheckDef  # noqa: E402
from app.compliance.check_retrieval import retrieve_for_check  # noqa: E402
from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks  # noqa: E402

_EVAL_DIR = _BACKEND / "data" / "eval"
_DOC_TYPE = "special_provision"


def _sp_checks() -> List[CheckDef]:
    return [c for c in BUILTIN_CHECKS if "sp" in c.source_files]


def _anchor_in_row(anchors: Anchors, row: Dict[str, Any]) -> bool:
    """Whether *row* actually carries the anchor -- checked against its own
    section_id/tables metadata first (the same signal pin_by_anchors matches
    on), falling back to a literal substring search in content for a
    project with no section metadata at all (the pre-hybrid baseline),
    where metadata can never answer this."""
    if anchors.is_empty:
        return False
    meta = row.get("metadata") or {}
    if meta.get("section_id") in anchors.sections:
        return True
    if any(t in (meta.get("tables") or []) for t in anchors.tables):
        return True
    content = row.get("content", "")
    return any(a in content for a in (*anchors.sections, *anchors.tables))


def probe_check(db: Any, embed_fn, project_id: str, check: CheckDef, repeat: int) -> Dict[str, Any]:
    anchors = extract_anchors(check.instruction)

    runs = [
        retrieve_for_check(db, embed_fn, project_id, check.instruction, top_k=check.sp_top_k)
        for _ in range(max(1, repeat))
    ]
    first = runs[0]
    top_row = first.rows[0] if first.rows else None
    top_ids = [r.rows[0].get("id") if r.rows else None for r in runs]

    # Pure dense-search top similarity for the check's own instruction --
    # independent of pinning/fusion, so it is comparable before vs after
    # even when the pipeline's own top row is a pin (which carries no
    # similarity score at all).
    dense_top = retrieve_sp_chunks(db, embed_fn, project_id, check.instruction, match_count=1)
    top_similarity = dense_top[0]["similarity"] if dense_top else None

    return {
        "check_key": check.check_key,
        "sp_top_k": check.sp_top_k,
        "anchor_sections": list(anchors.sections),
        "anchor_tables": list(anchors.tables),
        "has_anchor": not anchors.is_empty,
        "rows_returned": len(first.rows),
        "pinned_count": first.pinned,
        "anchor_missing": first.anchor_missing,
        "top_similarity": top_similarity,
        "anchor_in_top_passage": _anchor_in_row(anchors, top_row) if top_row else False,
        "top_passage_section_id": (top_row.get("metadata") or {}).get("section_id") if top_row else None,
        "repeat_runs": len(runs),
        "stable_top_id": len({str(i) for i in top_ids}) <= 1,
        "top_ids_seen": sorted({str(i) for i in top_ids}),
    }


def run_probe(project_id: str, label: str, repeat: int) -> Dict[str, Any]:
    from app.config import config
    from app.database import get_db
    from langchain_openai import OpenAIEmbeddings

    db = get_db()
    emb = OpenAIEmbeddings(model=config.EMBEDDING_MODEL, api_key=config.OPENAI_API_KEY)

    checks = _sp_checks()
    results = [probe_check(db, emb.embed_query, project_id, c, repeat) for c in checks]

    out = {
        "label": label,
        "project_id": project_id,
        "doc_type": _DOC_TYPE,
        "repeat": repeat,
        "num_sp_checks": len(results),
        "num_with_anchor": sum(1 for r in results if r["has_anchor"]),
        "checks": results,
    }
    _EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _EVAL_DIR / f"sp_retrieval_probe.{label}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"{label}: {len(results)} sp checks, {out['num_with_anchor']} with an anchor -> {out_path}")
    for r in results:
        print(
            f"  {r['check_key']:32s} anchor={r['has_anchor']!s:5} "
            f"rows={r['rows_returned']:2d} pinned={r['pinned_count']:2d} "
            f"top_sim={r['top_similarity']!s:8} anchor_in_top={r['anchor_in_top_passage']!s:5} "
            f"stable={r['stable_top_id']}"
        )
    return out


def _load(label: str) -> Dict[str, Any]:
    path = _EVAL_DIR / f"sp_retrieval_probe.{label}.json"
    if not path.exists():
        raise SystemExit(f"no saved run for label {label!r} at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def compare(label_a: str, label_b: str) -> None:
    a, b = _load(label_a), _load(label_b)
    by_key_b = {c["check_key"]: c for c in b["checks"]}

    header = (
        f"{'check':32s} {'anchor':>6s} {'pin a->b':>9s} {'rows a->b':>10s} "
        f"{'sim a->b':>18s} {'anchor_in_top a->b':>20s}"
    )
    print(f"\n{label_a}  vs  {label_b}")
    print(header)

    diffs: List[Dict[str, Any]] = []
    for ca in a["checks"]:
        cb = by_key_b.get(ca["check_key"])
        if cb is None:
            continue
        sim_a = ca["top_similarity"]
        sim_b = cb["top_similarity"]
        sim_str = f"{sim_a!s:>7} -> {sim_b!s:<7}"
        print(
            f"{ca['check_key']:32s} {str(ca['has_anchor']):>6s} "
            f"{ca['pinned_count']:>3d} -> {cb['pinned_count']:<3d} "
            f"{ca['rows_returned']:>3d} -> {cb['rows_returned']:<4d} "
            f"{sim_str:>18s} "
            f"{str(ca['anchor_in_top_passage']):>5s} -> {str(cb['anchor_in_top_passage']):<5s}"
        )
        diffs.append({
            "check_key": ca["check_key"],
            "has_anchor": ca["has_anchor"],
            "pinned_count": [ca["pinned_count"], cb["pinned_count"]],
            "rows_returned": [ca["rows_returned"], cb["rows_returned"]],
            "top_similarity": [sim_a, sim_b],
            "anchor_in_top_passage": [ca["anchor_in_top_passage"], cb["anchor_in_top_passage"]],
            "stable_top_id": [ca["stable_top_id"], cb["stable_top_id"]],
        })

    improved = sum(1 for d in diffs if d["has_anchor"] and d["anchor_in_top_passage"] == [False, True])
    regressed = sum(1 for d in diffs if d["has_anchor"] and d["anchor_in_top_passage"] == [True, False])
    sim_up = sum(
        1 for d in diffs
        if d["top_similarity"][0] is not None and d["top_similarity"][1] is not None
        and d["top_similarity"][1] > d["top_similarity"][0]
    )
    print(
        f"\nanchor_in_top_passage: {improved} improved (False->True), "
        f"{regressed} regressed (True->False), of {sum(1 for d in diffs if d['has_anchor'])} anchored checks"
    )
    print(f"top_similarity increased for {sim_up}/{len(diffs)} checks")

    out_path = _EVAL_DIR / f"sp_retrieval_probe.compare.{label_a}.{label_b}.json"
    out_path.write_text(
        json.dumps({"a": label_a, "b": label_b, "diffs": diffs}, indent=2), encoding="utf-8",
    )
    print(f"wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id", help="session_id to probe")
    parser.add_argument("--label", help="label to save this run under")
    parser.add_argument("--repeat", type=int, default=1, help="times to rerun retrieve_for_check per check (stability)")
    parser.add_argument("--compare", nargs=2, metavar=("LABEL_A", "LABEL_B"), help="compare two saved runs")
    args = parser.parse_args()

    if args.compare:
        compare(*args.compare)
        return

    if not args.project_id or not args.label:
        parser.error("--project-id and --label are required unless --compare is given")

    run_probe(args.project_id, args.label, args.repeat)


if __name__ == "__main__":
    main()
