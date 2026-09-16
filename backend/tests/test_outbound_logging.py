"""backend/tests/test_outbound_logging.py

Tests that non-LLM outbound calls -- Supabase, Neo4j, the Supabase admin
REST API over httpx, and Supabase Storage -- log when they fail, instead of
returning None/False silently.

All outbound calls are mocked; nothing here touches the network.

Runnable two ways:
    python tests/test_outbound_logging.py
    python -m pytest tests/test_outbound_logging.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException  # noqa: E402

from tests.logcapture import capture_logs  # noqa: E402


def test_supabase_connection_failure_logs_an_error():
    from app.database import Database

    with patch("app.database.Database.get_client", side_effect=RuntimeError("dns failure")):
        with capture_logs("app.database") as records:
            assert Database.test_connection() is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "dns failure" in records[0].getMessage()


def test_neo4j_connection_failure_logs_an_error():
    from app.neo4j_client import Neo4jClient

    with patch("app.neo4j_client.Neo4jClient.get_graph", side_effect=RuntimeError("bolt refused")):
        with capture_logs("app.neo4j_client") as records:
            assert Neo4jClient.test_connection() is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "bolt refused" in records[0].getMessage()


class _FakeJWKSClient:
    @staticmethod
    def get_signing_key_from_jwt(token):
        raise RuntimeError("jwks endpoint unreachable")


def test_jwks_failure_with_a_secret_configured_logs_a_warning():
    """Falling through from JWKS to the legacy HS256 secret is a normal,
    supported path (older Supabase projects) -- but it used to be completely
    silent, so a genuinely unreachable JWKS endpoint looked identical to an
    old-format token. The signature still gets verified, so: WARNING."""
    from app import auth as auth_module

    with patch.object(auth_module, "_get_jwks_client", return_value=_FakeJWKSClient()), \
         patch.object(auth_module.config, "SUPABASE_JWT_SECRET", "legacy-hs256-secret"), \
         patch("app.auth.jwt.decode", return_value={"sub": "user-1"}):
        with capture_logs("app.auth") as records:
            assert auth_module.user_id_from_token("Bearer sometoken") == "user-1"

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "jwks endpoint unreachable" in records[0].getMessage()


def test_jwks_failure_without_a_secret_logs_an_error_naming_the_unverified_fallback():
    """SUPABASE_JWT_SECRET is optional (config.py defaults it to ""). With it
    unset, a JWKS outage drops straight through to verify_signature=False and
    every attacker-supplied JWT with a `sub` claim is accepted. Authentication
    silently degrading is not a WARNING -- and the line has to say so, since
    it is the only indication the unverified branch engaged."""
    from app import auth as auth_module

    with patch.object(auth_module, "_get_jwks_client", return_value=_FakeJWKSClient()), \
         patch.object(auth_module.config, "SUPABASE_JWT_SECRET", ""), \
         patch("app.auth.jwt.decode", return_value={"sub": "user-1"}):
        with capture_logs("app.auth") as records:
            assert auth_module.user_id_from_token("Bearer sometoken") == "user-1"

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    text = records[0].getMessage()
    assert "jwks endpoint unreachable" in text
    assert "without signature verification" in text


def test_admin_user_lookup_transport_failure_logs_a_warning():
    from app.api import auth as api_auth

    with patch("app.api.auth.httpx.get", side_effect=RuntimeError("connection reset")):
        with capture_logs("app.api.auth") as records:
            assert api_auth._find_user_id_by_email("someone@example.com") is None

    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "connection reset" in records[0].getMessage()
    # The looked-up email must never reach the log.
    assert "someone@example.com" not in records[0].getMessage()


def test_admin_user_lookup_non_200_logs_a_warning():
    from app.api import auth as api_auth

    class _Resp:
        status_code = 503

    with patch("app.api.auth.httpx.get", return_value=_Resp()):
        with capture_logs("app.api.auth") as records:
            assert api_auth._find_user_id_by_email("someone@example.com") is None

    assert len(records) == 1
    assert "503" in records[0].getMessage()


def test_password_update_failure_logs_an_error():
    from app.api import auth as api_auth

    with patch("app.api.auth.httpx.put", side_effect=RuntimeError("timeout")):
        with capture_logs("app.api.auth") as records:
            assert api_auth._update_user_password("user-1", "hunter2hunter2") is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    text = records[0].getMessage()
    assert "timeout" in text
    assert "hunter2hunter2" not in text  # never log the password


def test_password_update_non_200_logs_an_error():
    from app.api import auth as api_auth

    class _Resp:
        status_code = 422

    with patch("app.api.auth.httpx.put", return_value=_Resp()):
        with capture_logs("app.api.auth") as records:
            assert api_auth._update_user_password("user-1", "hunter2hunter2") is False

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "422" in records[0].getMessage()


def test_pdf_storage_transport_failure_logs_an_error():
    """Storage being unreachable (DNS, reset, timeout) is a genuine outbound
    API failure and the one the plan originally missed -- it fails before any
    status code exists to classify."""
    import asyncio

    import httpx

    from app.api import pdf as pdf_module

    class _FailingClient:
        closed = False

        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            raise httpx.ConnectError("name resolution failed")

        async def aclose(self):
            type(self).closed = True

    with patch("app.api.pdf.httpx.AsyncClient", _FailingClient):
        with capture_logs("app.api.pdf") as records:
            try:
                asyncio.run(pdf_module.serve_pdf("Spec2019"))
                assert False, "Should have raised HTTPException"
            except HTTPException as exc:
                assert exc.status_code == 502

    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert "name resolution failed" in records[0].getMessage()
    # Under a sustained Storage outage every request takes this path, so a
    # client left open here leaks a connection pool per request.
    assert _FailingClient.closed is True, "the AsyncClient was leaked on the error path"


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
