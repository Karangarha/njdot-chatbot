"""Integration fixtures. These talk to the LOCAL Supabase container only.

backend/local.env.local supplies SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for
the local container but carries no OPENAI_API_KEY, so .env is loaded first and
local.env.local overlaid on top: local database, real embedding key.

Every test here is skipped when the container is not reachable, so the default
suite stays hermetic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

load_dotenv(_BACKEND / ".env")
load_dotenv(_BACKEND / "local.env.local", override=True)


@pytest.fixture(scope="session")
def local_db():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        pytest.skip("local Supabase env not configured")
    if "localhost" not in url and "127.0.0.1" not in url and "kong" not in url:
        pytest.skip("refusing to run integration tests against a non-local host")
    from supabase import create_client
    client = create_client(url, key)
    try:
        client.table("session_chunks").select("id", count="exact", head=True).limit(1).execute()
    except Exception as exc:
        pytest.skip(f"local Supabase not reachable: {type(exc).__name__}")
    return client
