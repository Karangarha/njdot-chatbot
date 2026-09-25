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


# _build_sp_search_fn's anchor_missing split (review.py, ~lines 390-421) has
# five distinguishable cases. Each test below queries with the same anchored
# instruction (mirroring test_check_retrieval.py's _INSTRUCTION) except the
# "no anchor named" case, so the only thing that varies is what the chunks'
# metadata carries.

_ANCHORED_INSTRUCTION = (
    "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\".\n\n"
    "Classify each submittal by the governing table."
)


def _sp_embeddings():
    embeddings = MagicMock()
    embeddings.embed_query.return_value = [0.0, 1.0]
    return embeddings


def test_build_sp_search_fn_anchor_matched_is_not_missing():
    """Case 1: the named anchor pins a real chunk -- anchor_missing False."""
    sp_chunks = [
        {"content": "Governing table text.",
         "metadata": {"section_id": "105.05", "tables": ["TABLE 105.05-1"], "chunk_index": 0}},
    ]
    search_fn = _build_sp_search_fn(sp_chunks, [[0.0, 1.0]], _sp_embeddings())

    _text, _candidates, anchor_missing = search_fn(_ANCHORED_INSTRUCTION, top_k=8)

    assert anchor_missing is False


def test_build_sp_search_fn_anchor_absent_with_section_metadata_present_is_missing():
    """Case 2: the project's chunks DO carry section_id metadata (just not
    for the anchor this check names) -- a genuine, provable gap."""
    sp_chunks = [
        {"content": "Some other clause entirely.",
         "metadata": {"section_id": "200.01", "tables": [], "chunk_index": 0}},
    ]
    search_fn = _build_sp_search_fn(sp_chunks, [[0.0, 1.0]], _sp_embeddings())

    _text, _candidates, anchor_missing = search_fn(_ANCHORED_INSTRUCTION, top_k=8)

    assert anchor_missing is True


def test_build_sp_search_fn_no_section_id_key_degrades_silently():
    """Case 3: pre-section-aware chunks with no "section_id" key at all --
    pinning could never have worked, so this must NOT be reported as a gap."""
    sp_chunks = [
        {"content": "Pre-section-aware chunk.", "metadata": {"tables": [], "chunk_index": 0}},
    ]
    search_fn = _build_sp_search_fn(sp_chunks, [[0.0, 1.0]], _sp_embeddings())

    _text, _candidates, anchor_missing = search_fn(_ANCHORED_INSTRUCTION, top_k=8)

    assert anchor_missing is False


def test_build_sp_search_fn_section_id_present_but_none_degrades_silently():
    """Case 4: "section_id" key exists but is None -- same silent-degrade
    behavior as the key being absent entirely."""
    sp_chunks = [
        {"content": "Chunk with an explicit null section_id.",
         "metadata": {"section_id": None, "tables": [], "chunk_index": 0}},
    ]
    search_fn = _build_sp_search_fn(sp_chunks, [[0.0, 1.0]], _sp_embeddings())

    _text, _candidates, anchor_missing = search_fn(_ANCHORED_INSTRUCTION, top_k=8)

    assert anchor_missing is False


def test_build_sp_search_fn_no_anchor_named_is_not_missing():
    """Case 5: the instruction names no section/table anchor at all -- never
    a missing-evidence signal, regardless of what the chunks carry."""
    sp_chunks = [
        {"content": "Some clause with section metadata.",
         "metadata": {"section_id": "200.01", "tables": [], "chunk_index": 0}},
    ]
    search_fn = _build_sp_search_fn(sp_chunks, [[0.0, 1.0]], _sp_embeddings())

    _text, _candidates, anchor_missing = search_fn(
        "Confirm the narrative addresses community commitments.", top_k=8,
    )

    assert anchor_missing is False


def test_build_sp_search_fn_does_not_pin_an_unrelated_bare_table_a():
    """FINDING 3, in-process closure: ``_sp_chunk_matches_anchors`` claims to
    be "the same pin test" as ``check_retrieval.pin_by_anchors``, which
    excludes bare single-letter table anchors (see
    Anchors.pinnable_tables) -- a chunk carrying an unrelated "TABLE A"
    (e.g. the Special Provision's own unrelated table, coincidentally
    sharing a caption with the Construction Scheduling Manual's "Table A"
    a check actually means) must not be pinned into position 0."""
    sp_chunks = [
        {"content": "Unrelated SP table, coincidentally also called Table A.",
         "metadata": {"section_id": None, "tables": ["TABLE A"], "chunk_index": 0}},
        {"content": "The real best dense match for this query.",
         "metadata": {"section_id": None, "tables": [], "chunk_index": 1}},
    ]
    sp_vectors = [[0.0, 1.0], [1.0, 0.0]]
    embeddings = MagicMock()
    embeddings.embed_query.return_value = [1.0, 0.0]  # matches the second chunk

    search_fn = _build_sp_search_fn(sp_chunks, sp_vectors, embeddings)
    text, _candidates, _anchor_missing = search_fn(
        "Construction Scheduling Manual Table A governs submittal review.", top_k=1,
    )

    assert "The real best dense match" in text
    assert "coincidentally also called Table A" not in text


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
