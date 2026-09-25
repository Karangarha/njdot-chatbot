"""backend/tests/test_ingestion_logging.py

Tests that PDF ingestion logs when it silently degrades. Every one of these
paths currently discards the exception and carries on with less data, which
is the right runtime behaviour and the wrong logging behaviour -- a garbled
table becomes a missing compliance citation with nothing in the log to
explain it.

Uses fake pdfplumber page objects; no real PDF is opened.

Runnable two ways:
    python tests/test_ingestion_logging.py
    python -m pytest tests/test_ingestion_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ingestion.table_extractor import TableExtractor  # noqa: E402
from tests.logcapture import capture_logs  # noqa: E402

_TABLE_LOGGER = "app.ingestion.table_extractor"
_CHUNKER_LOGGER = "app.ingestion.session_chunker"


class _ExplodingFindTablesPage:
    """A page whose find_tables() raises under both strict and relaxed
    settings -- the shape a malformed page graph produces."""

    width = 612
    height = 792

    def find_tables(self, table_settings=None):
        raise RuntimeError("bad page graph")


class _ExplodingTableObject:
    bbox = (0.0, 100.0, 500.0, 300.0)

    def extract(self):
        raise RuntimeError("cell extraction failed")


class _OneBadTablePage:
    """find_tables() succeeds but the single returned table blows up when
    extracted -- one bad table must not abort the page, but must be logged."""

    width = 612
    height = 792

    def find_tables(self, table_settings=None):
        return [_ExplodingTableObject()]


def test_find_tables_failure_logs_a_warning():
    with capture_logs(_TABLE_LOGGER) as records:
        result = TableExtractor().extract_tables(_ExplodingFindTablesPage(), page_pdf=441)

    assert result == []
    assert len(records) >= 1
    assert all(r.levelno == logging.WARNING for r in records)
    joined = " ".join(r.getMessage() for r in records)
    assert "441" in joined
    assert "bad page graph" in joined


def test_one_bad_table_logs_a_warning_and_does_not_abort_the_page():
    with capture_logs(_TABLE_LOGGER) as records:
        result = TableExtractor().extract_tables(_OneBadTablePage(), page_pdf=442)

    assert result == []  # the bad table is skipped, not raised
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1
    joined = " ".join(r.getMessage() for r in warnings)
    assert "442" in joined
    assert "cell extraction failed" in joined


class _UncroppablePage:
    """find_tables() and extract() both succeed, but every crop() raises --
    the shape a page with a malformed content stream produces. Reaches the
    caption-scan and footnote-scan guards, which the bad-table fakes above
    never get to (extract() raises first, before _process_table calls them)."""

    width = 612
    height = 792

    def find_tables(self, table_settings=None):
        return [_ExtractablePage()]

    def crop(self, bbox):
        raise RuntimeError("content stream unreadable")


class _ExtractablePage:
    """A table object whose extract() returns real rows."""

    bbox = (0.0, 100.0, 500.0, 300.0)

    def extract(self):
        return [["Sieve Size", "% Passing"], ["19.0 mm", "100"]]


def test_caption_and_footnote_scan_failures_log_warnings():
    with capture_logs(_TABLE_LOGGER) as records:
        result = TableExtractor().extract_tables(_UncroppablePage(), page_pdf=443)

    # The table itself still survives -- only its caption and footnotes are lost.
    assert len(result) == 1
    assert result[0]["table_id"] == "p443_t1"      # positional fallback id
    assert result[0]["footnotes"] == []

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    joined = " ".join(r.getMessage() for r in warnings)
    assert "Caption scan failed" in joined
    assert "Footnote scan failed" in joined
    assert "content stream unreadable" in joined
    # Both lines must be attributable to a specific table on a specific page.
    assert joined.count("443") == 2


def test_table_to_nl_fallback_logs_a_warning_when_pdfplumber_cannot_open():
    from app.ingestion.session_chunker import _extract_page_with_tables

    pages = [{"page_num": 1, "text": "original text", "char_count": 13}]
    with capture_logs(_CHUNKER_LOGGER) as records:
        result = _extract_page_with_tables("/definitely/not/a/real/file.pdf", pages)

    assert result == pages  # falls back to the original pages
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "/definitely/not/a/real/file.pdf" in records[0].getMessage()


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
