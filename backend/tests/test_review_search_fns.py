"""backend/tests/test_review_search_fns.py

Tests for the review-citations feature's evidence-tagging search functions
in app.api.review: _build_sp_search_fn, _build_sp_search_fn_from_supabase,
and _build_static_doc_search_fn now return (tagged_text, {tag: candidate})
instead of a plain string, so _evaluate_one_check can verify which passage
(if any) the LLM's cited_chunk_ids actually refers to. See
docs/superpowers/specs/2026-09-10-review-citations-design.md.

The two SP closures return a third element, anchor_missing (see
app.compliance.check_retrieval.RetrievalResult) -- _build_static_doc_search_fn
has no anchor concept and keeps the two-tuple.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.api.review import (  # noqa: E402
    _build_sp_search_fn,
    _build_sp_search_fn_from_supabase,
    _build_static_doc_search_fn,
)
from app.compliance.eval_engine import EvidenceCandidate  # noqa: E402


def test_build_sp_search_fn_tags_top_result_with_page_pdf():
    sp_chunks = [
        {"content": "Gas work prohibited in July.", "metadata": {"page_pdf": 7}},
        {"content": "Unrelated clause about bonds.", "metadata": {"page_pdf": 2}},
    ]
    sp_vectors = [[1.0, 0.0], [0.0, 1.0]]
    embeddings = MagicMock()
    embeddings.embed_query.return_value = [1.0, 0.0]  # matches first chunk exactly

    search_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)
    text, candidates, anchor_missing = search_fn("gas work restriction", top_k=1)

    assert "[cite:sp-0]" in text
    assert "Gas work prohibited in July." in text
    assert candidates["sp-0"] == EvidenceCandidate(
        kind="private", doc_type="special_provision", label="Special Provision", page_pdf=7,
    )
    assert anchor_missing is False


def test_build_sp_search_fn_returns_none_for_no_chunks():
    assert _build_sp_search_fn([], [], MagicMock()) is None


def test_build_sp_search_fn_from_supabase_tags_rows_with_page_pdf():
    # "funding" has no section/table anchor, so retrieve_for_check's pinning
    # and anchor_missing probe both short-circuit before ever calling
    # db.table() -- only the "does this project have SP chunks" existence
    # check (below) and the dense leg's db.rpc("match_session_chunks", ...)
    # need fixtures.
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.count = 1
    db.rpc.return_value.execute.return_value.data = [
        {"id": "c1", "doc_type": "special_provision",
         "content": "Multi-year funding clause text.", "metadata": {"page_pdf": 12}},
    ]

    search_fn = _build_sp_search_fn_from_supabase(db, MagicMock(), "proj1")
    text, candidates, anchor_missing = search_fn("funding")

    assert "[cite:sp-0]" in text
    assert candidates["sp-0"].page_pdf == 12
    assert candidates["sp-0"].kind == "private"
    assert anchor_missing is False


def test_build_static_doc_search_fn_tags_results_with_doc_and_page():
    fake_searcher = MagicMock()
    fake_searcher.search.return_value = [
        {"content": "Proposal bond must be 10% of bid.", "metadata": {
            "doc": "Spec2019", "section_id": "102.03", "section_title": "Proposal Guaranty", "page_pdf": 45,
        }},
    ]
    with patch("app.api.review.VectorSearcher", return_value=fake_searcher):
        search_fn = _build_static_doc_search_fn("specs_2019")
        text, candidates = search_fn("proposal bond requirements")

    tag = "specs_2019-0"
    assert f"[cite:{tag}]" in text
    assert candidates[tag] == EvidenceCandidate(
        kind="public", doc_type="Spec2019", label="Proposal Guaranty",
        page_pdf=45, section_id="102.03",
    )


def test_build_static_doc_search_fn_skips_candidate_when_doc_missing():
    fake_searcher = MagicMock()
    fake_searcher.search.return_value = [
        {"content": "Untagged passage with no doc metadata.", "metadata": {"doc": None, "page_pdf": 12}},
    ]
    with patch("app.api.review.VectorSearcher", return_value=fake_searcher):
        search_fn = _build_static_doc_search_fn("specs_2019")
        text, candidates = search_fn("some query")

    tag = "specs_2019-0"
    assert f"[cite:{tag}]" in text
    assert "Untagged passage with no doc metadata." in text
    assert tag not in candidates


def test_build_static_doc_search_fn_returns_none_when_searcher_init_fails():
    with patch("app.api.review.VectorSearcher", side_effect=RuntimeError("no key")):
        assert _build_static_doc_search_fn("specs_2019") is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    total = sum(1 for n in globals() if n.startswith("test_"))
    print(f"\n{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)
