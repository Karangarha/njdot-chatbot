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


def _chunk(cid, section=None, tables=(), body="body", chunk_index=0):
    return {
        "id": cid, "content": body, "doc_type": "special_provision",
        "metadata": {"section_id": section, "tables": list(tables), "chunk_index": chunk_index},
        "similarity": 0.5,
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
        self.pin_filter = None

    def rpc(self, name, params):
        outer = self
        if name == "keyword_search_session_chunks":
            outer.keyword_query = params["search_query"]
            # Real Postgres honors match_count as a LIMIT; unsliced fixture
            # data would hide any regression in how many rows the caller
            # actually asks the RPC for.
            data = outer._keyword[: params.get("match_count", len(outer._keyword))]
        else:
            data = outer._vector

        class _R:
            def execute(self_inner):
                return type("X", (), {"data": data})()

        return _R()

    def table(self, _name):
        return _FakeTable(pinned=self._pinned, metadata_rows=self._metadata_rows, outer=self)


class _FakeTable:
    def __init__(self, pinned, metadata_rows, outer=None):
        self._pinned = pinned
        self._metadata_rows = metadata_rows
        self._select_cols = None
        self._outer = outer

    def select(self, cols="*", *a, **k):
        # The metadata probe asks for select("metadata"); the pin query
        # asks for select("*"). That's the real distinction between the two
        # callers, so route each to its own fixture instead of one shared
        # `rows` list.
        self._select_cols = cols
        return self

    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self

    @property
    def not_(self):
        # Real postgrest-py's .not_ is a property (see
        # postgrest.base_request_builder.BaseFilterRequestBuilder.not_), not
        # a method -- callers chain `.not_.is_(...)`, no parens.
        return self

    def is_(self, *a, **k): return self

    def or_(self, filter_str, *a, **k):
        if self._outer is not None:
            self._outer.pin_filter = filter_str
        return self

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


def test_pin_filter_adds_a_dot_bounded_prefix_clause_for_section_anchors():
    """FINDING 2: pinning must not require an exact section_id match -- a
    check naming the parent "105.07" has to also pin a document whose actual
    heading is the child "105.07.02". The PostgREST filter this builds uses
    "like" with a literal ".*" suffix (PostgREST's documented stand-in for
    SQL LIKE's "%"), so the boundary is a dot, not a bare string prefix --
    proven end-to-end against the real filter in
    backend/tests/integration/test_local_retrieval.py."""
    db = _FakeDB(pinned=[_chunk("t", section="105.05", tables=["TABLE 105.05-1"])])
    retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert 'metadata->>section_id.like.105.05.*' in db.pin_filter


def test_pin_filter_excludes_bare_single_letter_table_anchors():
    """FINDING 3: a bare "TABLE A" must never enter the PIN filter -- it's
    ambiguous across documents (three checks name the Construction
    Scheduling Manual's "Table A"; an unrelated Special Provision can carry
    its own unrelated "TABLE A"), and pinned rows bypass ranking entirely,
    so a wrong pin is worse than no pin. A numbered caption stays
    pinnable."""
    from app.compliance.anchors import Anchors
    from app.compliance.check_retrieval import pin_by_anchors

    db = _FakeDB(pinned=[_chunk("t", tables=["TABLE 105.05-1"])])
    pin_by_anchors(db, "p1", Anchors(tables=("TABLE 105.05-1", "TABLE A")), limit=8)

    assert 'metadata->tables.cs.["TABLE 105.05-1"]' in db.pin_filter
    assert "TABLE A" not in db.pin_filter


def test_pin_by_anchors_returns_empty_when_only_an_ambiguous_table_is_named():
    """No section anchor, and the only table anchor is the ambiguous "TABLE
    A" -- nothing is left to pin. Must return [] rather than send a
    malformed empty .or_() filter to PostgREST."""
    from app.compliance.anchors import Anchors
    from app.compliance.check_retrieval import pin_by_anchors

    db = _FakeDB(pinned=[_chunk("wrong-doc-table-a", tables=["TABLE A"])])
    rows = pin_by_anchors(db, "p1", Anchors(tables=("TABLE A",)), limit=8)

    assert rows == []
    assert db.pin_filter is None  # .or_() was never reached


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


def test_top_k_zero_is_budget_starvation_not_a_missing_anchor():
    # FINDING 5: at top_k=0, pin_limit is forced to 0 and pin_by_anchors
    # never even runs -- an empty pinned_rows there is budget starvation,
    # not proof the anchor is absent. metadata_rows proves the project DOES
    # have section metadata (so the old code would have called this a
    # genuine gap); anchor_missing must still come back False.
    db = _FakeDB(pinned=[], vector=[], keyword=[], metadata_rows=[_chunk("other", section="900.01")])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=0)
    assert out.pinned == 0
    assert out.anchor_missing is False


def test_negative_top_k_is_also_budget_starvation_not_a_missing_anchor():
    # A malformed custom check's sp_top_k could reach here as negative
    # (see test_parse_checks.py's own guard against that at the API layer)
    # -- retrieve_for_check must not treat that as a genuine gap either.
    db = _FakeDB(pinned=[], vector=[], keyword=[], metadata_rows=[_chunk("other", section="900.01")])
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=-5)
    assert out.pinned == 0
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


def test_pinned_rows_come_back_in_reading_order_not_supply_order():
    # _chunk() used to never set chunk_index, so every pinned row sorted
    # equal and a broken sort key in pin_by_anchors wouldn't be caught.
    # Supply the rows out of order here and require the fix (chunk_index
    # ascending) to put them back.
    db = _FakeDB(
        pinned=[
            _chunk("c2", section="105.05", chunk_index=2),
            _chunk("c0", section="105.05", chunk_index=0),
            _chunk("c1", section="105.05", chunk_index=1),
        ],
    )
    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)
    assert [r["id"] for r in out.rows] == ["c0", "c1", "c2"]


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


def test_project_has_section_metadata_finds_a_later_row_not_just_the_first():
    """Old bug: reading one arbitrary row and inspecting only ITS section_id
    could land on a row that happens to lack it even when a sibling row in
    the same project has one, silently losing the genuine-gap signal. The
    shared _FakeDB/_FakeTable above don't apply real filters (they just hand
    back a fixture), so this needs a small fake that actually honors the
    not.is.null filter server-side, to prove the fix asks "does ANY row have
    section_id", not "does the first row returned"."""
    from app.compliance.check_retrieval import project_has_section_metadata

    class _FilteringTable:
        def __init__(self, rows):
            self._rows, self._negate = rows, False

        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self

        @property
        def not_(self):
            self._negate = True
            return self

        def is_(self, column, value):
            assert value in (None, "null")
            field = column.rsplit("->>", 1)[-1]
            has_field = lambda r: (r.get("metadata") or {}).get(field) is not None
            self._rows = [r for r in self._rows if has_field(r) == self._negate]
            self._negate = False
            return self

        def limit(self, n):
            self._rows = self._rows[:n]
            return self

        def execute(self):
            return type("X", (), {"data": self._rows})()

    class _FilteringDB:
        def __init__(self, rows):
            self._rows = rows

        def table(self, _name):
            return _FilteringTable(self._rows)

    # The row an unordered `.limit(1)` might return first has no section_id;
    # a later row does.
    rows = [{"metadata": {"section_id": None}}, {"metadata": {"section_id": "105.05"}}]
    assert project_has_section_metadata(_FilteringDB(rows), "p1") is True
    assert project_has_section_metadata(_FilteringDB([{"metadata": {}}]), "p1") is False


def test_pinning_still_fills_the_budget_when_the_two_legs_overlap():
    """Regression for the fetch-size bug: each leg used to be asked for only
    `remaining` rows. When the dense and keyword legs return the SAME rows
    (realistic -- an exact-match anchor ranks highly in both) and those rows
    are also the ones already pinned, there is nothing left after the
    pinned-id dedupe to fill the budget with.

    Both legs here return the identical 8-row pool, and its first 4 rows are
    exactly the 4 pinned rows. A `remaining`-sized fetch (4/leg) returns only
    those 4 pinned rows per leg, so after fusion (no-op, both legs agree)
    and the pinned dedupe, ZERO unpinned survivors are left -- the check
    comes back with 4 rows instead of 8. Over-fetching each leg to `top_k`
    (8) pulls the 4 genuinely-unpinned rows (f0..f3) into range, which is
    exactly `remaining`.
    """
    pinned = [_chunk(f"p{i}", section="105.05", chunk_index=i) for i in range(4)]
    pool = pinned + [_chunk(f"f{i}") for i in range(4)]
    db = _FakeDB(pinned=pinned, vector=pool, keyword=pool)

    out = retrieve_for_check(db, lambda q: [0.0] * 3, "p1", _INSTRUCTION, top_k=8)

    ids = [r["id"] for r in out.rows]
    assert len(ids) == len(set(ids)) == 8


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
