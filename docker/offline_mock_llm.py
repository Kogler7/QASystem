"""Minimal OpenAI-compatible streaming LLM used by the sealed runtime test."""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


app = FastAPI()
RESPONSE = json.dumps({"entities": [], "relations": []}, ensure_ascii=False)


class ChatCompletionRequest(BaseModel):
    stream: bool = False


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": "offline-mock", "object": "model"}]}


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest):
    if not request.stream:
        return {
            "id": "offline-mock",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": RESPONSE}}],
        }

    def events() -> Iterator[str]:
        payload = {"id": "offline-mock", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": RESPONSE}}]}
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9000)
