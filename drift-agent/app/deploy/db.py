"""Lazy async SQLAlchemy engine + session factory.

Engine creation is deferred to first use so that drift-agent boots cleanly
even when Postgres isn't reachable yet (or when the deploy subsystem is
intentionally disabled by leaving DRIFT_PG_URL unset).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    if not settings.drift_pg_url:
        raise RuntimeError(
            "DRIFT_PG_URL is not configured — Drift Deploy is disabled."
        )
    engine = create_async_engine(settings.drift_pg_url, pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, sm


def engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine, _sessionmaker = _build()
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine, _sessionmaker = _build()
    return _sessionmaker


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Use as `async with session() as s:`."""
    async with sessionmaker()() as s:
        yield s
