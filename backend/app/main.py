"""FastAPI application entry point for the NJDOT Chatbot API.

Start the server
----------------
From the ``backend/`` directory::

    uvicorn app.main:app --reload           # development
    uvicorn app.main:app --host 0.0.0.0     # production

Interactive docs
----------------
* Swagger UI : http://localhost:8000/docs
* ReDoc      : http://localhost:8000/redoc

Endpoints
---------
GET  /health      → {"status": "ok"}
POST /api/query   → QueryResponse  (see app.api.query)
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.pdf import router as pdf_router
from app.api.query import router as query_router
from app.api.review import router as review_router
from app.api.session import router as session_router
from app.config import config

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="NJDOT Chatbot API",
    description=(
        "RAG-based question-answering API for NJDOT Standard Specifications, "
        "CPM Scheduling requirements, and Material Procedures."
    ),
    version="0.1.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
_allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://cm-chatbot-corral.vercel.app",
    "https://cm-smart-assistant.vercel.app",
]

if config.FRONTEND_URL and config.FRONTEND_URL not in _allowed_origins:
    _allowed_origins.append(config.FRONTEND_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(query_router)
app.include_router(pdf_router)
app.include_router(conversations_router)
app.include_router(review_router)
app.include_router(session_router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Liveness probe — returns ``{"status": "ok"}`` with HTTP 200."""
    return {"status": "ok"}


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
