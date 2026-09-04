"""Conversation history endpoints for NJDOT Chatbot.

GET /api/conversations                           → list conversations for the authenticated user
GET /api/conversations/{conversation_id}/messages → messages for a specific conversation

Auth: Supabase JWT must be passed as ``Authorization: Bearer <token>``.
The ``sub`` claim in the decoded token is used as the user ID to scope queries.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.auth import user_id_from_token as _user_id_from_token
from app.database import get_db

router = APIRouter(tags=["conversations"])


# ── Response models ───────────────────────────────────────────────────────────

class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: str


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    citations:  list = []
    bdc_alerts: list = []
    created_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/conversations", response_model=List[ConversationOut])
async def list_conversations(
    authorization: str | None = Header(default=None),
) -> list:
    """Return the 40 most-recent conversations for the authenticated user."""
    user_id = _user_id_from_token(authorization)
    db = get_db()
    result = (
        db.table("conversations")
        .select("id, title, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(40)
        .execute()
    )
    return result.data or []


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=List[MessageOut],
)
async def get_conversation_messages(
    conversation_id: str,
    authorization: str | None = Header(default=None),
) -> list:
    """Return all messages for a conversation, verifying it belongs to the user."""
    user_id = _user_id_from_token(authorization)
    db = get_db()

    # Confirm the conversation belongs to this user
    conv = (
        db.table("conversations")
        .select("id")
        .eq("id", conversation_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not conv.data:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = (
        db.table("messages")
        .select("id, role, content, citations, bdc_alerts, created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    )
    return msgs.data or []
