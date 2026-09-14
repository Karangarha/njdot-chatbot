"""backend/tests/test_hybrid_fuse.py

Reciprocal Rank Fusion arithmetic, isolated from any database so it can be
reasoned about directly. RRF score for a document present in both lists:

    v_weight/(60 + rank_v) + k_weight/(60 + rank_k)

with 1-based ranks and a missing list contributing nothing.

    python -m pytest backend/tests/test_hybrid_fuse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.retrieval.hybrid_ranker import fuse  # noqa: E402


def _row(rid):
    return {"id": rid, "content": f"body {rid}", "metadata": {}}


def test_a_document_in_both_lists_outranks_one_in_only_the_first():
    both, vector_only = _row("a"), _row("b")
    out = fuse([vector_only, both], [both], v_weight=0.5, k_weight=0.5, match_count=2)
    assert [r["id"] for r in out] == ["a", "b"]


def test_keyword_weight_can_lift_a_keyword_only_hit_above_a_vector_hit():
    # The anchored case: 0.3/0.7 is what classify_query returns for a query
    # carrying a section number, and it must actually change the order.
    v_first, k_first = _row("v"), _row("k")
    out = fuse([v_first], [k_first], v_weight=0.3, k_weight=0.7, match_count=2)
    assert out[0]["id"] == "k"


def test_similarity_carries_the_rrf_score_and_match_count_truncates():
    out = fuse([_row("a"), _row("b"), _row("c")], [], v_weight=1.0, k_weight=0.0, match_count=2)
    assert len(out) == 2
    assert out[0]["similarity"] > out[1]["similarity"]
    assert abs(out[0]["similarity"] - 1.0 / 61) < 1e-9


def test_empty_inputs_are_not_an_error():
    assert fuse([], [], v_weight=0.5, k_weight=0.5, match_count=5) == []


if __name__ == "__main__":
    test_a_document_in_both_lists_outranks_one_in_only_the_first()
    test_keyword_weight_can_lift_a_keyword_only_hit_above_a_vector_hit()
    test_similarity_carries_the_rrf_score_and_match_count_truncates()
    test_empty_inputs_are_not_an_error()
    print("All tests passed!")
