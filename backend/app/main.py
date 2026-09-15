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
import sys

# Windows' console defaults to the legacy cp1252 codepage, which can't encode
# the emoji some ingestion modules (e.g. pdf_parser.py) print for progress
# logging — crashes with UnicodeEncodeError the first time such a print runs
# under uvicorn (previously only ever hit via debug scripts that reconfigured
# this locally). Fix it once, globally, at the real server entrypoint.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ───────────────────────────────────────────────────────────────────
# Configured BEFORE the router imports below: app/api/query.py calls get_db()
# at module-import time, and any record it emits while the root logger is
# still an unconfigured WARNING would be dropped instead of printed.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logging.getLogger("azure").setLevel(logging.WARNING)  # App Insights auto-instrumentation floods INFO with per-request HTTP dumps
logger = logging.getLogger(__name__)

from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.pdf import router as pdf_router
from app.api.query import router as query_router
from app.api.review import router as review_router
from app.api.session import router as session_router
from app.config import config
from app.request_logging import log_request_outcome

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="NJDOT Chatbot API",
    description=(
        "RAG-based question-answering API for NJDOT Standard Specifications, "
        "CPM Scheduling requirements, and Material Procedures."
    ),
    version="0.1.0",
)

# ── Request outcome logging ───────────────────────────────────────────────────
# Registered BEFORE CORSMiddleware below, and the order matters: Starlette
# inserts each newly-added middleware at the OUTSIDE of the stack, so
# whatever is added last ends up outermost. Adding this one first leaves
# CORSMiddleware outermost, so a 4xx/5xx response still picks up its CORS
# headers on the way out. Note that an UNHANDLED exception is re-raised past
# CORSMiddleware to Starlette's own ServerErrorMiddleware, which sits outside
# all user middleware — that synthesized 500 carries no CORS headers in any
# ordering, so this registration order is about handled responses, not crashes.
app.middleware("http")(log_request_outcome)

# ── CORS ──────────────────────────────────────────────────────────────────────
_allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://cm-chatbot-coral.vercel.app",
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
