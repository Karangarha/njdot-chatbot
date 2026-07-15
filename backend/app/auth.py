"""Shared Supabase JWT auth helper.

Extracted from ``app.api.conversations`` so other authenticated endpoints
(e.g. the review re-run endpoint in ``app.api.review``) can reuse it without
importing a private name from a sibling router module.
"""

from __future__ import annotations

import jwt
from fastapi import HTTPException

from app.config import config


def user_id_from_token(authorization: str | None) -> str:
    """Decode the Supabase JWT and return the user's UUID (``sub`` claim)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = authorization[7:]

    if not config.SUPABASE_JWT_SECRET:
        # If the JWT secret isn't configured, extract sub without verification.
        # Suitable for local dev only — never do this in production.
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
            return payload["sub"]
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Cannot decode token: {exc}") from exc

    try:
        payload = jwt.decode(
            token,
            config.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
        return payload["sub"]
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc


def user_id_from_token_optional(authorization: str | None) -> str | None:
    """Best-effort variant of ``user_id_from_token`` for endpoints that must
    keep working for anonymous callers (e.g. the initial review upload,
    which is usable signed-out — it just doesn't get saved/stored). Returns
    ``None`` instead of raising on any failure: missing header, malformed,
    expired, or wrong-secret. Never use this where an unauthenticated
    request should actually be rejected — use ``user_id_from_token`` there.
    """
    try:
        return user_id_from_token(authorization)
    except HTTPException:
        return None
