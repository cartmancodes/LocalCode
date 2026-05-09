from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
# Single-user dev tool: a small pool is plenty. SQLAlchemy's default
# pool_size=5/max_overflow=10 keeps up to 15 idle connections around per
# process; trimming both halves the steady-state memory footprint of the
# asyncpg pool without affecting throughput at this concurrency level.
engine = create_async_engine(
    _settings.database_url,
    future=True,
    pool_pre_ping=True,
    pool_size=3,
    max_overflow=2,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that provides an `AsyncSession` with managed
    transaction lifecycle: commits on a clean exit, rolls back on exception.

    Routes should NOT call `await db.commit()` themselves — this dependency
    will commit when the handler returns. Routes that raise
    ``HTTPException`` after writes will still see the rollback (HTTPException
    propagates through `__aexit__`)."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
