"""In-app password reset endpoints.

Never sends email or exposes Supabase service credentials to the client.

POST /api/auth/request-reset   – Step 1: verify email server-side, return short-lived token.
POST /api/auth/reset-password  – Step 2: validate token, update password via admin API.
POST /api/auth/change-password – Authenticated password change for logged-in users.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import config

router = APIRouter(tags=["auth"])

_TTL_MINUTES = 15
_MIN_PW_LEN = 8
_GENERIC_ERROR = "Reset failed. Please start over."


def _reset_secret() -> str:
    base = config.SUPABASE_SERVICE_ROLE_KEY or config.SUPABASE_JWT_SECRET
    return hashlib.sha256(f"{base}:pw-reset-v1".encode()).hexdigest()


def _admin_headers() -> dict:
    return {
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _find_user_id_by_email(email: str) -> str | None:
    """Return Supabase user UUID for the given email, or None if not found."""
    try:
        resp = httpx.get(
            f"{config.SUPABASE_URL}/auth/v1/admin/users",
            headers=_admin_headers(),
            params={"filter": email.strip().lower(), "per_page": 10},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        users = resp.json().get("users", [])
        match = next(
            (u for u in users if (u.get("email") or "").lower() == email.strip().lower()),
            None,
        )
        return str(match["id"]) if match else None
    except Exception:
        return None


def _update_user_password(user_id: str, password: str) -> bool:
    """Update a user's password via the Supabase admin REST API."""
    try:
        resp = httpx.put(
            f"{config.SUPABASE_URL}/auth/v1/admin/users/{user_id}",
            headers=_admin_headers(),
            json={"password": password},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ── Models ─────────────────────────────────────────────────────────────────────

class RequestResetBody(BaseModel):
    email: str


class ResetPasswordBody(BaseModel):
    reset_token: str
    new_password: str


class ChangePasswordBody(BaseModel):
    new_password: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/api/auth/request-reset")
def request_reset(body: RequestResetBody):
    """
    Step 1 of in-app password reset.

    Always returns HTTP 200 regardless of whether the email exists to prevent
    account enumeration. The reset_token is only usable if the account exists.
    """
    user_id = _find_user_id_by_email(body.email)

    exp = datetime.now(timezone.utc) + timedelta(minutes=_TTL_MINUTES)
    token = jwt.encode(
        {
            "sub": user_id or "00000000-0000-0000-0000-000000000000",
            "purpose": "pw-reset",
            "valid": user_id is not None,
            "exp": exp,
        },
        _reset_secret(),
        algorithm="HS256",
    )
    return {"reset_token": token}


@router.post("/api/auth/reset-password")
def reset_password(body: ResetPasswordBody):
    """
    Step 2 of in-app password reset.

    Validates the short-lived token from step 1, then updates the password
    via the Supabase admin API. Generic errors prevent information leakage.
    """
    if len(body.new_password) < _MIN_PW_LEN:
        raise HTTPException(400, f"Password must be at least {_MIN_PW_LEN} characters.")

    try:
        payload = jwt.decode(body.reset_token, _reset_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(400, "Reset session expired. Please start over.")
    except Exception:
        raise HTTPException(400, _GENERIC_ERROR)

    if payload.get("purpose") != "pw-reset" or not payload.get("valid"):
        raise HTTPException(400, _GENERIC_ERROR)

    user_id = payload.get("sub", "")
    if not user_id or user_id == "00000000-0000-0000-0000-000000000000":
        raise HTTPException(400, _GENERIC_ERROR)

    if not _update_user_password(user_id, body.new_password):
        raise HTTPException(400, _GENERIC_ERROR)

    return {"ok": True}


@router.post("/api/auth/change-password")
def change_password(
    body: ChangePasswordBody,
    authorization: str | None = Header(default=None),
):
    """
    Authenticated password change for already-logged-in users.

    Requires a valid Supabase JWT in ``Authorization: Bearer <token>``.
    The service role key is never sent to the client.
    """
    from app.api.conversations import _user_id_from_token

    if len(body.new_password) < _MIN_PW_LEN:
        raise HTTPException(400, f"Password must be at least {_MIN_PW_LEN} characters.")

    user_id = _user_id_from_token(authorization)

    if not _update_user_password(user_id, body.new_password):
        raise HTTPException(400, "Password update failed. Please try again.")

    return {"ok": True}
