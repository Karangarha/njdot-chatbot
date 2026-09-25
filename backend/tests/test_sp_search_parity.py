"""backend/tests/test_sp_search_parity.py

Two bugs this file pins down.

1. The RPC returns the top match_count rows of ANY doc_type for the session,
   and the caller then filtered to special_provision in Python. On a project
   with a narrative and a key map, asking for 8 SP passages routinely yielded
   3. Over-fetch so the post-filter still leaves match_count.

2. The fresh-review closure applied no similarity floor and the rerun closure
   applied 0.2, so the same check retrieved differently depending on which
   code path ran. The floor is now the caller's choice, defaulting to none.

    python -m pytest backend/tests/test_sp_search_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.retrieval_langchain.sp_retriever import retrieve_sp_chunks  # noqa: E402


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.last_params = None

    def rpc(self, name, params):
        self.last_params = params
        outer = self

        class _R:
            def execute(self_inner):
                return type("X", (), {"data": outer.rows})()

        return _R()


def _mixed_rows(n_sp, n_other):
    sp = [{"content": f"sp {i}", "doc_type": "special_provision", "similarity": 0.9} for i in range(n_sp)]
    other = [{"content": f"nar {i}", "doc_type": "narrative", "similarity": 0.8} for i in range(n_other)]
    return other + sp   # interleaving order does not matter; the filter does


def test_over_fetches_so_the_doc_type_filter_still_yields_match_count():
    db = _FakeDB(_mixed_rows(n_sp=8, n_other=24))
    rows = retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "night work", match_count=8)
    assert db.last_params["match_count"] > 8, "must over-fetch to survive the doc_type filter"
    assert len(rows) == 8
    assert all(r["doc_type"] == "special_provision" for r in rows)


def test_defaults_to_no_similarity_floor():
    db = _FakeDB([])
    retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "q")
    assert db.last_params["match_threshold"] == 0.0


def test_honours_an_explicit_floor():
    db = _FakeDB([])
    retrieve_sp_chunks(db, lambda q: [0.0] * 3, "proj-1", "q", match_threshold=0.35)
    assert db.last_params["match_threshold"] == 0.35


if __name__ == "__main__":
    test_over_fetches_so_the_doc_type_filter_still_yields_match_count()
    test_defaults_to_no_similarity_floor()
    test_honours_an_explicit_floor()
    print("All tests passed!")
