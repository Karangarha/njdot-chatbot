"""PDF serving endpoint for NJDOT Chatbot.

GET /api/pdf/{doc_name}?page={page_pdf}

Fetches the PDF from Supabase Storage and streams it back to the client
with inline Content-Disposition so browsers render it directly.  The
optional ``page`` query parameter is accepted for logging; the actual
page scroll is driven by the ``#page=N`` URL fragment on the client.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pdf"])

# Maps ingestion ``doc`` keys to their PDF filenames in Supabase Storage.
# MP* and BDC* doc names match their filenames directly (e.g. "MP3-25" →
# "MP3-25.pdf"), so only the two non-obvious mappings are listed here.
_DOC_TO_FILENAME: dict[str, str] = {
    "Spec2019": "StandSpecRoadBridge.pdf",
    "SchedulingManual": "constructionschedulingmanual.pdf",
}


def _storage_url(filename: str) -> str:
    return f"{config.SUPABASE_URL}/storage/v1/object/public/pdfs/{filename}"


@router.get("/api/pdf/{doc_name}")
async def serve_pdf(doc_name: str, page: int | None = None) -> StreamingResponse:
    """Proxy a PDF from Supabase Storage for inline browser viewing."""
    filename = _DOC_TO_FILENAME.get(doc_name, f"{doc_name}.pdf")
    url = _storage_url(filename)

    try:
        client = httpx.AsyncClient(timeout=30.0)
        request = client.build_request("GET", url)
        upstream = await client.send(request, stream=True)
    except httpx.RequestError as exc:
        logger.error(
            "Storage unreachable for doc=%r file=%r: %s", doc_name, filename, exc, exc_info=True,
        )
        raise HTTPException(status_code=502, detail=f"PDF storage unreachable: {exc}") from exc

    if upstream.status_code != 200:
        # Supabase Storage returns 400 with {"error":"not_found"} for missing objects.
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        try:
            is_not_found = upstream.status_code in (400, 404) and b"not_found" in body
        except Exception as exc:
            logger.warning(
                "Could not classify Storage response %d for doc=%r: %s",
                upstream.status_code, doc_name, exc,
            )
            is_not_found = False
        if is_not_found:
            logger.warning("PDF not found in Storage: doc=%r file=%r", doc_name, filename)
            raise HTTPException(status_code=404, detail=f"PDF not found: {doc_name!r}")
        logger.error(
            "Storage returned %d for doc=%r file=%r", upstream.status_code, doc_name, filename,
        )
        raise HTTPException(
            status_code=502,
            detail=f"PDF storage returned {upstream.status_code}",
        )

    async def _stream():
        try:
            async for chunk in upstream.aiter_bytes(65_536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        content=_stream(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
