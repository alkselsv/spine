from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from spine.config import settings
from spine.ingest.loaders import documents_to_remember_payloads, load_path
from spine.memory.cognee_memory import ask, format_answers, remember_texts

app = FastAPI(
    title="Spine",
    version="0.1.0",
    description="Phase 1: company Q&A on Cognee. Trace-like orchestration comes later.",
)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    dataset: str | None = None
    session_id: str | None = None


class AskResponse(BaseModel):
    question: str
    answer: str
    raw_count: int


class IngestRequest(BaseModel):
    path: str = Field(..., description="Absolute or project-relative path to a file or directory")
    dataset: str | None = None
    node_set: list[str] | None = None


class IngestResponse(BaseModel):
    path: str
    documents: int
    status: str


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "spine", "phase": "qa"}


@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(body: AskRequest) -> AskResponse:
    try:
        results = await ask(
            body.question,
            dataset=body.dataset,
            session_id=body.session_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"recall failed: {exc}") from exc
    items = list(results) if results is not None else []
    return AskResponse(
        question=body.question,
        answer=format_answers(items),
        raw_count=len(items),
    )


@app.post("/ingest", response_model=IngestResponse)
async def ingest_endpoint(body: IngestRequest) -> IngestResponse:
    path = Path(body.path)
    if not path.is_absolute():
        path = settings.project_root / path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"path not found: {path}")

    docs = load_path(path)
    if not docs:
        raise HTTPException(status_code=400, detail="no supported documents found")

    payloads = documents_to_remember_payloads(docs)
    try:
        await remember_texts(
            payloads,
            dataset=body.dataset,
            node_set=body.node_set,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"remember failed: {exc}") from exc

    return IngestResponse(path=str(path), documents=len(docs), status="ok")
