from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

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


# Mount Drift Deploy routers + observability only when DRIFT_PG_URL is
# set, so an operator can run drift-agent for pure observability without
# Postgres.
if settings.drift_pg_url:
    from .deploy.observability import (
        http_request_duration_seconds,
        http_requests_total,
        start_background_refresh,
        stop_background_refresh,
    )
    from .deploy.routes_admin import router as deploy_admin_router
    from .deploy.routes_agent import router as deploy_agent_router

    app.include_router(deploy_admin_router)
    app.include_router(deploy_agent_router)

    @app.on_event("startup")
    async def _on_startup() -> None:
        start_background_refresh()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        await stop_background_refresh()

    @app.middleware("http")
    async def _record_http_metrics(request: Request, call_next) -> Response:
        # Only instrument the deploy surface — observability endpoints
        # themselves are excluded from their own metrics.
        path = request.url.path
        if not path.startswith("/api/deploy") or path.endswith("/metrics"):
            return await call_next(request)
        start = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            elapsed = time.perf_counter() - start
            # Use the route template, not the raw path with ids in it.
            route = request.scope.get("route")
            template = getattr(route, "path", path) if route else path
            http_requests_total.labels(
                method=request.method, path=template, status=status
            ).inc()
            http_request_duration_seconds.labels(
                method=request.method, path=template
            ).observe(elapsed)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    # Pure read from in-process registry — gauges are kept fresh by the
    # background refresh task (see deploy/observability.py).
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "model": settings.model,
        "vm_url": settings.vm_url,
        "deploy_enabled": settings.deploy_enabled,
    }


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
