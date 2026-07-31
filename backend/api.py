"""FastAPI entrypoint for the search-and-summarize workflow.

Run from the project root:
  .venv/bin/uvicorn backend.api:app --reload --port 8000

Requests may take several minutes (Ollama + Drive/Gmail MCP, and first-time OAuth).
"""

from __future__ import annotations

import logging
import os
import sys

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)

if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

from search_and_summarize import run_workflow  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Document Assistant",
    description=(
        "Search a topic, summarize with Ollama, upload to Google Drive via MCP, "
        "and email the shareable link. Long-running requests are expected."
    ),
    version="1.0.0",
)

_cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="Topic to search and summarize")


class SearchResponse(BaseModel):
    success: bool
    topic: str
    summary: str | None = None
    google_drive_link: str | None = None
    upload_status: str
    email_status: str
    processing_time_seconds: float
    error: str | None = None


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/search-and-summarize", response_model=SearchResponse)
async def search_and_summarize(body: SearchRequest) -> SearchResponse:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="A topic is required.")

    logger.info("API request for topic: %s", topic)
    try:
        result = await run_workflow(topic)
    except Exception as exc:
        logger.exception("Unhandled workflow error: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Workflow failed unexpectedly: {exc}",
        ) from exc

    response = SearchResponse(**result)
    if result.get("error") and not result.get("summary"):
        # Validation / early failure still returns structured body with HTTP 200
        # when we have a built result; unexpected crashes are 500 above.
        pass
    return response
