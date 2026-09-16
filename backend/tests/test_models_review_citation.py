"""backend/tests/test_models_review_citation.py

Tests for ReviewCitation and the new citation-carrying fields on
EvaluationSchema/ReviewCheckResult -- see
docs/superpowers/specs/2026-09-10-review-citations-design.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.models import EvaluationSchema, ReviewCheckResult, ReviewCitation  # noqa: E402


def test_evaluation_schema_defaults_cited_chunk_ids_to_empty_list():
    result = EvaluationSchema(evidence="e", source="s")
    assert result.cited_chunk_ids == []


def test_evaluation_schema_defaults_item_lists_to_empty():
    # status is not a field here at all (see EvaluationSchema's docstring) --
    # it's derived from breaching_items in eval_engine.py, never authored
    # directly.
    result = EvaluationSchema(evidence="e", source="s")
    assert result.considered_items == []
    assert result.breaching_items == []
    assert not hasattr(result, "status")


def test_review_check_result_defaults_citations_to_empty_list():
    result = ReviewCheckResult(
        id="c1", category="Cat", name="Name", status="Pass", evidence="e", source="s",
    )
    assert result.citations == []


def test_review_citation_verified_chunk_shape():
    citation = ReviewCitation(
        kind="private", doc_type="special_provision", label="Special Provision",
        page_pdf=5, section_id="sp_2_1", verified=True,
    )
    assert citation.kind == "private"
    assert citation.page_pdf == 5
    assert citation.verified is True


def test_review_citation_unverified_has_no_page():
    citation = ReviewCitation(
        kind="private", doc_type="unknown", label="Unverified citation (sp-9)", verified=False,
    )
    assert citation.page_pdf is None
    assert citation.section_id is None
    assert citation.verified is False


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
