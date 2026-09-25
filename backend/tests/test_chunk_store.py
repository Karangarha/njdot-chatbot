"""backend/tests/test_chunk_store.py

No LLM, no network, no Supabase (db is a fake recording every insert() call).
Runnable two ways:
    python tests/test_chunk_store.py
    python -m pytest tests/test_chunk_store.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ingestion.chunk_store import insert_session_chunks   # noqa: E402
from tests.logcapture import capture_logs                     # noqa: E402


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


class _FailingTable:
    """Mimics a Supabase table whose insert blows up at .execute()."""

    def __init__(self, exc):
        self._exc = exc

    def insert(self, rows):
        return self

    def execute(self):
        raise self._exc


class FailingDB:
    def __init__(self, exc):
        self._exc = exc

    def table(self, name):
        return _FailingTable(self._exc)


def test_insert_failure_logs_an_error_and_reraises():
    """This is the one RUNTIME Supabase call site the logging work
    instruments. Both /api/review and /api/session ingestion funnel their
    chunk writes through here, and before this the failure surfaced only as
    a generic 500 or a generic "review failed" progress message with nothing
    naming Supabase. The exception must still propagate -- a half-written
    chunk set has to fail the ingest, not be swallowed."""
    db = FailingDB(RuntimeError("connection reset by peer"))

    with capture_logs("app.ingestion.chunk_store") as records:
        try:
            insert_session_chunks(db, "proj-9", [_chunk("a", "special_provision")])
            assert False, "insert_session_chunks must re-raise"
        except RuntimeError as exc:
            assert "connection reset by peer" in str(exc)

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    text = records[0].getMessage()
    assert "proj-9" in text                    # which session
    assert "special_provision" in text         # which document type
    assert "connection reset by peer" in text  # what actually failed
    assert records[0].exc_info is not None


def test_insert_failure_reports_how_far_it_got():
    """A partial write is the dangerous case -- the log must say which batch
    failed, not just that something did."""
    db = FailingDB(RuntimeError("boom"))
    chunks = [_chunk(f"c{i}", "key_map") for i in range(120)]

    with capture_logs("app.ingestion.chunk_store") as records:
        try:
            insert_session_chunks(db, "proj-10", chunks)
            assert False, "insert_session_chunks must re-raise"
        except RuntimeError:
            pass

    # Fails on the very first batch, so exactly one record -- and it names
    # the batch bounds and the total.
    assert len(records) == 1
    text = records[0].getMessage()
    assert "120" in text
    assert "0-50" in text


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
