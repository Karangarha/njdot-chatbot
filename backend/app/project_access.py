"""Sign-in + ownership rule for everything keyed by a review project id
(/api/session/*).

Every caller must be signed in. A project with an owner is usable only by
that owner. The owner is looked up in the in-process review store first
(a review that just finished isn't in review_projects yet -- the frontend
inserts that row after /api/session/upload), then in review_projects.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from app.auth import user_id_from_token
from app.database import get_db

logger = logging.getLogger(__name__)


def _project_owner(project_id: str) -> Optional[str]:
    from app.api.review import _review_progress  # lazy: avoid import cycle

    owner = (_review_progress.get(project_id) or {}).get("user_id")
    if owner:
        return owner
    try:
        rows = (
            get_db().table("review_projects").select("user_id")
            .eq("id", project_id).limit(1).execute().data
        ) or []
    except Exception as exc:
        # Fail closed: an unreachable DB must not turn into open access.
        logger.error("Ownership lookup for project_id=%s failed: %s", project_id, exc, exc_info=True)
        raise HTTPException(status_code=503, detail="Could not verify project access.") from exc
    return rows[0].get("user_id") if rows else None


def require_project_access(project_id: str, authorization: Optional[str]) -> str:
    """Return the caller's user id, or raise 401 (not signed in),
    403 (someone else's project) or 503 (ownership lookup failed)."""
    caller = user_id_from_token(authorization)
    # Empty id = standalone upload (no project yet). Skip the lookup:
    # querying review_projects.id = '' errors on the uuid column -> 503.
    owner = _project_owner(project_id) if project_id else None
    if owner and owner != caller:
        raise HTTPException(status_code=403, detail="This project does not belong to you")
    return caller
