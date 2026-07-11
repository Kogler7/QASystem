from contextlib import asynccontextmanager

from fastapi import FastAPI

from .routers.knowledge_precipitation import router as knowledge_protocol_router
from mcp_services.server import mcp


mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="QASystem Knowledge Services", lifespan=lifespan)
app.include_router(knowledge_protocol_router)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/", mcp_app)
