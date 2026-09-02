"""Shared Supabase JWT auth helper.

Extracted from ``app.api.conversations`` so other authenticated endpoints
(e.g. the review re-run endpoint in ``app.api.review``) can reuse it without
importing a private name from a sibling router module.

Supabase projects sign access tokens one of two ways depending on when the
project was created / whether it's migrated to the newer "JWT signing keys"
feature:
  - Legacy: a single shared HS256 secret (``SUPABASE_JWT_SECRET``).
  - Current: an asymmetric key (ES256 on this project, verified locally),
    published at ``{SUPABASE_URL}/auth/v1/.well-known/jwks.json`` and
    rotatable without invalidating the static secret's config entry.
A token signed with the asymmetric key raises ``InvalidAlgorithmError`` if
you naively try to verify it against the HS256 secret ("The specified alg
value is not allowed") — this happened in practice on this project's local
Supabase instance, which issues ES256 tokens. We try JWKS first (the
current, correct path for both local and hosted Supabase) and fall back to
the legacy HS256 secret only if JWKS verification fails, so this keeps
working for older projects that haven't rotated to asymmetric keys.
"""

from __future__ import annotations

import jwt
from fastapi import HTTPException

from app.config import config

_jwks_client: jwt.PyJWKClient | None = None


def _get_jwks_client() -> jwt.PyJWKClient | None:
    """Lazily build (and cache) the JWKS client for this Supabase project.

    Cached at module scope rather than per-call: ``PyJWKClient`` caches the
    fetched key set internally too, so re-verifying many tokens doesn't
    re-fetch the JWKS endpoint every time.
    """
    global _jwks_client
    if _jwks_client is None and config.SUPABASE_URL:
        _jwks_client = jwt.PyJWKClient(
            f"{config.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
        )
    return _jwks_client


def user_id_from_token(authorization: str | None) -> str:
    """Decode the Supabase JWT and return the user's UUID (``sub`` claim)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = authorization[7:]

    jwks_client = _get_jwks_client()
    if jwks_client is not None:
        try:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256"],
                audience="authenticated",
            )
            return payload["sub"]
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except Exception:
            pass  # not a JWKS-signed token (or JWKS unreachable) — fall through

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
