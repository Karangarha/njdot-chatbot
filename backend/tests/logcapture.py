"""backend/tests/logcapture.py

Stdlib log capture for the backend test suite.

Deliberately not pytest's ``caplog`` fixture: every test file in this
directory is runnable both under pytest AND as a plain script (see each
file's ``if __name__ == "__main__":`` runner), and a fixture parameter
breaks the plain-script path.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, List


class _ListHandler(logging.Handler):
    """Collects every emitted record instead of writing it anywhere."""

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_logs(logger_name: str, level: int = logging.WARNING) -> Iterator[List[logging.LogRecord]]:
    """Capture records emitted by ``logger_name`` at ``level`` or above.

    Attaches to the named logger directly (not the root logger), so a test
    asserting on one module's output is unaffected by anything else that
    happens to log during the same test.
    """
    handler = _ListHandler()
    target = logging.getLogger(logger_name)
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(level)
    try:
        yield handler.records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
