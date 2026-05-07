from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .agent import run_agent
from .config import settings
from .schemas import PromptRequest


app = FastAPI(title="drift-agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": settings.model, "vm_url": settings.vm_url}


@app.post("/investigate")
async def investigate(req: PromptRequest) -> StreamingResponse:
    return StreamingResponse(
        run_agent(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
            "Connection": "keep-alive",
        },
    )
