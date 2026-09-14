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

    def __init__(self, pinned=(), vector=(), keyword=(), metadata_rows=None):
        self._pinned, self._vector, self._keyword = list(pinned), list(vector), list(keyword)
        # project_has_section_metadata's probe (select("metadata")) and
        # pin_by_anchors's query (select("*")) both hit db.table(...), but
        # they must be answerable independently -- otherwise a genuine gap
        # (project HAS section metadata, anchor matches nothing) can never
        # be distinguished from a pre-Task-6 project (no metadata at all),
        # which is the exact three-way split check_retrieval exists to make.
        # Default metadata_rows to `pinned` so tests that don't care about
        # the split keep the old behaviour (one fixture answers both).
        self._metadata_rows = list(pinned) if metadata_rows is None else list(metadata_rows)
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
        return _FakeTable(pinned=self._pinned, metadata_rows=self._metadata_rows)


class _FakeTable:
    def __init__(self, pinned, metadata_rows):
        self._pinned = pinned
        self._metadata_rows = metadata_rows
        self._select_cols = None

    def select(self, cols="*", *a, **k):
        # The metadata probe asks for select("metadata"); the pin query
        # asks for select("*"). That's the real distinction between the two
        # callers, so route each to its own fixture instead of one shared
        # `rows` list.
        self._select_cols = cols
        return self

    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def not_(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def or_(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self

    def execute(self):
        rows = self._metadata_rows if self._select_cols == "metadata" else self._pinned
        return type("X", (), {"data": rows, "count": len(rows)})()


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


def test_top_k_of_one_still_leaves_budget_for_one_pin():
    # int(1 * PIN_BUDGET_FRACTION) truncates to 0, which used to starve
    # pin_by_anchors entirely and get misreported as anchor_missing even
    # though a genuine match exists.
    db = _FakeDB(pinned=[_chunk("t", section="105.05", tables=["TABLE 105.05-1"])])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=1)
    assert out.pinned == 1
    assert out.rows[0]["id"] == "t"
    assert out.anchor_missing is False


def test_a_project_with_no_section_metadata_degrades_silently():
    # Ingested before section-aware chunking: pinning cannot work and that is
    # not evidence the clause is absent from the document.
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[_chunk("k1")])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.anchor_missing is False
    assert out.rows


# The three tests below pin down the module's central three-way split --
# anchor_missing must land on the right value for each case. Case 2 is the
# one the original six tests could never reach: _FakeDB's metadata probe and
# pin query used to share a single `rows` fixture, so "pinning found
# nothing" and "the project has no section metadata" were structurally the
# same fact. With metadata_rows answered independently of pinned, they can
# now disagree, which is exactly what the genuine-gap case requires.


def test_case1_no_section_metadata_anywhere_is_not_evidence_of_absence():
    # Pre-Task-6 project: the probe (metadata_rows=[]) sees no section_id
    # metadata at all, independent of what the pin query returns.
    db = _FakeDB(pinned=[], vector=[_chunk("v1")], keyword=[_chunk("k1")], metadata_rows=[])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.pinned == 0
    assert out.anchor_missing is False


def test_case2_section_metadata_present_but_anchor_matches_nothing_is_a_genuine_gap():
    # The project clearly has section metadata (metadata_rows carries a row
    # with a section_id, from some other section), yet the pin query for
    # THIS anchor (pinned=[]) still comes back empty -- pinning had a real
    # chance to match and didn't.
    db = _FakeDB(
        pinned=[],
        vector=[_chunk("v1")],
        keyword=[_chunk("k1")],
        metadata_rows=[_chunk("other", section="900.01")],
    )
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert out.pinned == 0
    assert out.anchor_missing is True


def test_case3_no_anchor_in_the_instruction_is_neither():
    # Nothing was ever named, so this can't be a missing-evidence signal --
    # true even though the project clearly has section metadata here
    # (metadata_rows is non-empty), because anchors.is_empty short-circuits
    # before project_has_section_metadata is ever queried.
    db = _FakeDB(
        pinned=[], vector=[_chunk("v1")], keyword=[],
        metadata_rows=[_chunk("other", section="900.01")],
    )
    out = retrieve_for_check(
        db, lambda q: [0.0] * 3, "p1", "Confirm the narrative addresses community commitments.", top_k=8,
    )
    assert out.anchor_missing is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
