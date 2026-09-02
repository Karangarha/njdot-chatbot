"""backend/tests/test_chunk_store.py

No LLM, no network, no Supabase (db is a fake recording every insert() call).
Runnable two ways:
    python tests/test_chunk_store.py
    python -m pytest tests/test_chunk_store.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ingestion.chunk_store import insert_session_chunks   # noqa: E402


class _FakeExecuted:
    def __init__(self, rows):
        self.data = rows


class _FakeTable:
    def __init__(self, recorder, name):
        self.recorder = recorder
        self.name = name
        self._pending = None

    def insert(self, rows):
        self._pending = rows
        return self

    def execute(self):
        self.recorder.append((self.name, self._pending))
        return _FakeExecuted(self._pending)


class FakeDB:
    def __init__(self):
        self.calls: list = []

    def table(self, name):
        return _FakeTable(self.calls, name)


def _chunk(content, doc_type, embedding=(0.1, 0.2)):
    return {"content": content, "embedding": list(embedding), "metadata": {"doc_type": doc_type}}


def test_inserts_one_batch_for_small_input():
    db = FakeDB()
    insert_session_chunks(db, "proj-1", [_chunk("a", "special_provision"), _chunk("b", "special_provision")])
    assert len(db.calls) == 1
    table_name, rows = db.calls[0]
    assert table_name == "session_chunks"
    assert len(rows) == 2
    assert rows[0]["session_id"] == "proj-1"
    assert rows[0]["doc_type"] == "special_provision"
    assert rows[0]["content"] == "a"
    assert rows[0]["embedding"] == [0.1, 0.2]


def test_batches_at_fifty_rows():
    db = FakeDB()
    chunks = [_chunk(f"c{i}", "key_map") for i in range(120)]
    insert_session_chunks(db, "proj-2", chunks)
    assert len(db.calls) == 3          # 50 + 50 + 20
    assert [len(rows) for _, rows in db.calls] == [50, 50, 20]


def test_empty_input_makes_no_calls():
    db = FakeDB()
    insert_session_chunks(db, "proj-3", [])
    assert db.calls == []


def test_doc_type_read_from_metadata():
    db = FakeDB()
    insert_session_chunks(db, "proj-4", [_chunk("x", "estimate")])
    _, rows = db.calls[0]
    assert rows[0]["doc_type"] == "estimate"
    assert rows[0]["metadata"]["doc_type"] == "estimate"


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
