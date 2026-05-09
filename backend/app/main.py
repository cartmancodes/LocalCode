from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Base, engine
from . import models  # noqa: F401  register tables on Base.metadata
from .orchestrator.registry import shutdown_all, warm_up
from .routes import (
    fleet as fleet_route,
    models as models_route,
    sessions,
    system as system_route,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-create tables in dev. Replace with Alembic for prod.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Build provider singletons up-front so first-call concurrency can't race
    # the registry init and so any provider misconfiguration surfaces at boot
    # rather than mid-WS-turn.
    await warm_up()
    yield
    await shutdown_all()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title=s.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(sessions.router)
    app.include_router(models_route.router)
    app.include_router(fleet_route.router)
    app.include_router(system_route.router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": s.env}

    return app


app = create_app()
