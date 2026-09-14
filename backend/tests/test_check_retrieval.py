"""backend/tests/test_check_retrieval.py

Composition of the three retrieval layers, with fakes for the database so the
logic is testable without a container.

    python -m pytest backend/tests/test_check_retrieval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.compliance.check_retrieval import (  # noqa: E402
    PIN_BUDGET_FRACTION, retrieve_for_check,
)

_INSTRUCTION = (
    "Special Provisions 105.05 WORKING DRAWINGS: \"TABLE 105.05-1 IS CHANGED TO\".\n\n"
    "Classify each submittal by the governing table."
)


def _chunk(cid, section=None, tables=(), body="body"):
    return {
        "id": cid, "content": body, "doc_type": "special_provision",
        "metadata": {"section_id": section, "tables": list(tables)}, "similarity": 0.5,
    }


class _FakeDB:
    """Enough of the PostgREST surface for pinning plus the two searches."""

    def __init__(self, pinned=(), vector=(), keyword=()):
        self._pinned, self._vector, self._keyword = list(pinned), list(vector), list(keyword)
        self.keyword_query = None

    def rpc(self, name, params):
        outer = self
        if name == "keyword_search_session_chunks":
            outer.keyword_query = params["search_query"]
            data = outer._keyword
        else:
            data = outer._vector

        class _R:
            def execute(self_inner):
                return type("X", (), {"data": data})()

        return _R()

    def table(self, _name):
        return _FakeTable(self._pinned)


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def not_(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def or_(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def execute(self):
        return type("X", (), {"data": self.rows, "count": len(self.rows)})()


def test_an_anchored_chunk_is_pinned_first():
    db = _FakeDB(
        pinned=[_chunk("t", section="105.05", tables=["TABLE 105.05-1"])],
        vector=[_chunk("v1"), _chunk("v2")],
        keyword=[_chunk("k1")],
    )
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.rows[0]["id"] == "t"
    assert out.pinned == 1
    assert out.anchor_missing is False


def test_the_bm25_query_is_anchors_only_never_the_rule_body():
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[])
    retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert "105.05" in db.keyword_query
    assert "Classify each submittal" not in db.keyword_query
    assert len(db.keyword_query.split()) <= 12


def test_pinned_results_are_capped_so_they_cannot_fill_the_budget():
    many = [_chunk(f"t{i}", section="105.05") for i in range(20)]
    db = _FakeDB(pinned=many, vector=[_chunk(f"v{i}") for i in range(20)], keyword=[])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.pinned <= int(8 * PIN_BUDGET_FRACTION)
    assert len(out.rows) == 8


def test_a_pinned_chunk_is_not_repeated_by_the_fused_half():
    shared = _chunk("t", section="105.05")
    db = _FakeDB(pinned=[shared], vector=[shared, _chunk("v1")], keyword=[shared])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert [r["id"] for r in out.rows].count("t") == 1


def test_no_anchor_means_dense_only_and_no_keyword_call():
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[])
    out = retrieve_for_check(
        db, lambda q: [0.0] * 3, "p1", "Confirm the narrative addresses community commitments.", top_k=8,
    )
    assert db.keyword_query is None
    assert out.pinned == 0
    assert out.anchor_missing is False


def test_a_project_with_no_section_metadata_degrades_silently():
    # Ingested before section-aware chunking: pinning cannot work and that is
    # not evidence the clause is absent from the document.
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[_chunk("k1")])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.anchor_missing is False
    assert out.rows


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
