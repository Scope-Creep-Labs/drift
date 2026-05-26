from __future__ import annotations

import time

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from .agent import run_agent
from .config import settings
from .schemas import PromptRequest
from .tools.metrics import make_vm_client


app = FastAPI(title="drift-agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    # User auth uses a cookie, which means credentialed requests. Required
    # only for browsers that hit /api/* across origins (rare with same-
    # origin SPA-via-nginx setup, but still correct).
    allow_credentials=True,
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
    from .admin.routes import router as admin_router
    from .admin.updates import start_updates_poller, stop_updates_poller
    from .deploy.routes_admin import router as deploy_admin_router
    from .deploy.routes_agent import router as deploy_agent_router
    from .deploy.seed import seed_default_apps
    from .deploy.terminal import router as terminal_router
    from .users.bootstrap import ensure_bootstrap_admin
    from .users.routes import router as auth_router

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(deploy_admin_router)
    app.include_router(deploy_agent_router)
    app.include_router(terminal_router)

    @app.on_event("startup")
    async def _on_startup() -> None:
        await ensure_bootstrap_admin()
        # Default-apps (e.g. reporter) — only if deploy subsystem is
        # configured; pre-deploy installs have no DRIFT_PG_URL and
        # there's nothing to seed against.
        from .config import settings
        if settings.deploy_enabled:
            await seed_default_apps()
        start_background_refresh()
        start_updates_poller()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        await stop_background_refresh()
        await stop_updates_poller()

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


# Investigate endpoint: when the deploy subsystem (and thus the user
# system) is enabled, require an authenticated user. In pure-observability
# deploys, the endpoint is open and the surrounding layer (e.g. Caddy
# basic_auth) is expected to gate access.
if settings.drift_pg_url:
    from .users.deps import UserContext, get_current_user

    @app.post("/investigate")
    async def investigate(
        req: PromptRequest,
        user: UserContext = Depends(get_current_user),
    ) -> StreamingResponse:
        return StreamingResponse(
            run_agent(req, user=user),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # disable nginx response buffering
                "Connection": "keep-alive",
            },
        )
else:
    @app.post("/investigate")
    async def investigate(req: PromptRequest) -> StreamingResponse:
        return StreamingResponse(
            run_agent(req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )


# /api/query — thin authed PromQL passthrough used by live charts that
# poll on a frontend timer. Reuses the same VM credentials configured for
# the agent's query_range tool, so an operator's access to the SPA is
# the only gate. Returns the raw VictoriaMetrics response (matrix shape);
# the frontend converts to Plotly traces. Auth mirrors /investigate.
from pydantic import BaseModel, Field  # noqa: E402


class QueryRangeRequest(BaseModel):
    promql: str = Field(min_length=1, max_length=8192)
    start: float
    end: float
    step: int = Field(ge=1, le=3600, default=15)


async def _run_query_range(body: QueryRangeRequest) -> dict:
    vm = make_vm_client()
    try:
        return await vm.query_range(body.promql, body.start, body.end, f"{body.step}s")
    finally:
        await vm.aclose()


if settings.drift_pg_url:
    @app.post("/api/query")
    async def query_range_endpoint(
        body: QueryRangeRequest,
        _user: "UserContext" = Depends(get_current_user),
    ) -> dict:
        return await _run_query_range(body)
else:
    @app.post("/api/query")
    async def query_range_endpoint(body: QueryRangeRequest) -> dict:
        return await _run_query_range(body)
