from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .orchestrator import AgenticGraphRAGOrchestrator

app = FastAPI(title="Agentic GraphRAG", version="0.1.0")
orchestrator = AgenticGraphRAGOrchestrator()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "agentic-graphrag"}


@app.post("/api/agentic-graphrag/ask")
def ask(body: dict) -> dict:
    question = str(body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    session_id = body.get("session_id")
    return orchestrator.answer(question=question, session_id=session_id).to_dict()
