from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .orchestrator.registry import shutdown_all, warm_up
from .routes import (
    fleet as fleet_route,
    models as models_route,
    sessions,
    system as system_route,
)
from .storage.sessions import store as session_store


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build provider singletons up-front so first-call concurrency can't race
    # the registry init and so any provider misconfiguration surfaces at boot
    # rather than mid-WS-turn.
    await warm_up()

    # Sweep stale sessions on startup. The store self-bounds via a 24-hour
    # cooldown sentinel, so this is a no-op on a backend that just bounced
    # — only fires when the backend has been down for a while or the last
    # sweep was over a day ago.
    s = get_settings()
    try:
        result = await session_store.cleanup_expired(
            retention_days=s.session_retention_days
        )
        if any(result.get(k, 0) for k in ("deleted", "compacted")):
            logger.info("session cleanup at startup: %s", result)
    except Exception:  # noqa: BLE001
        logger.exception("session cleanup at startup failed")

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
