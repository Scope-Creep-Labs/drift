"""Prometheus metrics for the Drift Deploy control plane.

Exposed at `/metrics` on drift-agent. Two flavors of metric:

  - **Counters/histograms** incremented inline from the route handlers
    (revision uploads, check-ins, apply transitions, HTTP requests).
  - **Gauges** refreshed periodically by a background task that snapshots
    the database (devices_total{status}, deployment_targets_total{status},
    device_last_seen_seconds{device}, etc.).

The refresh-task model avoids two pitfalls of the simpler "custom
collector reads DB on each scrape" pattern:
  - Async-loop reentrancy (the collector runs in the scrape thread but
    SQLAlchemy's async engine is bound to the app's main loop).
  - Scrape-time latency tied to DB roundtrip.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Optional

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import func, select

from .db import session
from .models import App, AppRevision, Device, DeploymentTarget


log = logging.getLogger("drift_deploy.observability")


# ---------- Inline counters/histograms ----------


http_requests_total = Counter(
    "drift_deploy_http_requests_total",
    "Total HTTP requests handled by the deploy routes, by method/path/status.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "drift_deploy_http_request_duration_seconds",
    "HTTP request latency seconds, by method/path.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

revision_uploads_total = Counter(
    "drift_deploy_revision_uploads_total",
    "Revisions created (bundle packed + uploaded to object storage), by app.",
    ["app"],
)

apply_transitions_total = Counter(
    "drift_deploy_apply_transitions_total",
    "Deployment-target state transitions reported via check-in.",
    ["from_status", "to_status"],
)

check_ins_total = Counter(
    "drift_deploy_check_ins_total",
    "Edge-agent check-ins received.",
    ["result"],  # ok | unauthorized
)


# ---------- Background-refreshed gauges ----------


devices_total = Gauge(
    "drift_deploy_devices_total",
    "Devices known to the control plane, by status.",
    ["status"],
)
apps_total = Gauge(
    "drift_deploy_apps_total",
    "Apps known to the control plane.",
)
revisions_total = Gauge(
    "drift_deploy_revisions_total",
    "App revisions stored, by app.",
    ["app"],
)
deployment_targets_total = Gauge(
    "drift_deploy_deployment_targets_total",
    "Deployment targets, by status.",
    ["status"],
)
device_last_seen_seconds = Gauge(
    "drift_deploy_device_last_seen_seconds",
    "Unix epoch seconds of the device's last check-in; 0 if never.",
    ["device"],
)


# Agent-runtime metrics. These are emitted on /metrics alongside the
# deploy-state gauges + counters, scraped by reporter-cp's
# drift-deploy-cp job, and queryable from the chat just like any other
# metric series. Operators can ask "how many tokens did I burn this
# month?" instead of relying on a server-side aggregate table.
agent_tokens_total = Counter(
    "drift_agent_tokens_total",
    "Anthropic API token usage by Drift's investigate endpoint, by user/model/kind.",
    ["user", "model", "kind"],
)
agent_turns_total = Counter(
    "drift_agent_turns_total",
    "Completed conversation turns (one increment per successful run_agent loop).",
    ["user", "model"],
)


REFRESH_SECONDS = 30
_refresh_task: Optional[asyncio.Task] = None


async def _refresh_once() -> None:
    async with session() as s:
        # Staleness reaper: any device that claims to be "online" but
        # hasn't checked in within DRIFT_DEVICE_STALE_AFTER_SECONDS
        # gets flipped to "offline". The check-in handler resets to
        # "online" on the next successful tick, so once the device is
        # back, status reflects reality without any operator action.
        from ..config import settings as _settings  # avoid circular at module load
        stale_threshold = datetime.now(timezone.utc) - timedelta(
            seconds=_settings.drift_device_stale_after_seconds
        )
        stale_rows = await s.execute(
            select(Device).where(
                Device.status == "online",
                Device.last_seen < stale_threshold,
            )
        )
        for device in stale_rows.scalars().all():
            log.info(
                "device %s marked offline (last_seen=%s, threshold=%ss)",
                device.name,
                device.last_seen,
                _settings.drift_device_stale_after_seconds,
            )
            device.status = "offline"
        await s.commit()

        # Devices by status.
        devices_total.clear()
        rows = await s.execute(select(Device.status, func.count()).group_by(Device.status))
        for status, n in rows.all():
            devices_total.labels(status=status or "unknown").set(int(n))

        # Device last_seen (one series per device).
        device_last_seen_seconds.clear()
        rows = await s.execute(select(Device.name, Device.last_seen))
        for name, last_seen in rows.all():
            device_last_seen_seconds.labels(device=name).set(
                last_seen.timestamp() if last_seen else 0.0
            )

        # Apps total.
        n = (await s.execute(select(func.count(App.id)))).scalar_one() or 0
        apps_total.set(int(n))

        # Revisions per app.
        revisions_total.clear()
        rows = await s.execute(
            select(App.name, func.count(AppRevision.id))
            .join(AppRevision, AppRevision.app_id == App.id, isouter=True)
            .group_by(App.name)
        )
        for app_name, n in rows.all():
            revisions_total.labels(app=app_name).set(int(n))

        # Deployment targets by status.
        deployment_targets_total.clear()
        rows = await s.execute(
            select(DeploymentTarget.status, func.count()).group_by(DeploymentTarget.status)
        )
        for status, n in rows.all():
            deployment_targets_total.labels(status=status or "unknown").set(int(n))


async def _refresh_loop() -> None:
    while True:
        try:
            await _refresh_once()
        except Exception as e:  # noqa: BLE001 — never let the loop die
            log.warning("observability refresh failed: %s", e)
        await asyncio.sleep(REFRESH_SECONDS)


def start_background_refresh() -> None:
    """Kick off the periodic snapshotter. Safe to call multiple times."""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(_refresh_loop(), name="drift-deploy-obs-refresh")


async def stop_background_refresh() -> None:
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await _refresh_task
