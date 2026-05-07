from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Base, engine
from . import models  # noqa: F401  register tables on Base.metadata
from .orchestrator.registry import shutdown_all
from .routes import budget, models as models_route, sessions


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables in dev. Replace with Alembic for prod.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await shutdown_all()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(sessions.router)
    app.include_router(models_route.router)
    app.include_router(budget.router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": s.env}

    return app


app = create_app()
