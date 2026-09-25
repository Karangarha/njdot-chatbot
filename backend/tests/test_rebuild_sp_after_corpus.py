"""backend/tests/test_rebuild_sp_after_corpus.py

FINDING 4: backend/scripts/rebuild_sp_after_corpus.py writes 500+ rows
through get_db(). If local.env.local is missing, load_dotenv silently does
nothing and the insert lands on whatever database .env names -- possibly
the hosted database. _assert_local_host must refuse anything else.

    python -m pytest backend/tests/test_rebuild_sp_after_corpus.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_SCRIPTS = _BACKEND / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from rebuild_sp_after_corpus import _assert_local_host  # noqa: E402


def test_refuses_a_hosted_url():
    with pytest.raises(SystemExit):
        _assert_local_host("https://abcdefgh.supabase.co")


def test_refuses_an_empty_url():
    # An empty SUPABASE_URL means local.env.local (or .env) never loaded --
    # exactly the silent-misconfiguration case this guard exists to catch.
    with pytest.raises(SystemExit):
        _assert_local_host("")


def test_allows_localhost():
    _assert_local_host("http://localhost:54321")  # must not raise


def test_allows_127_0_0_1():
    _assert_local_host("http://127.0.0.1:54321")  # must not raise


def test_allows_the_docker_kong_hostname():
    _assert_local_host("http://kong:8000")  # must not raise


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("All tests passed!")
