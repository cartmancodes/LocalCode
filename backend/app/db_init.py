"""One-shot table creation. Run with `python -m backend.app.db_init`."""
from __future__ import annotations

import asyncio

from .db import Base, engine
from . import models  # noqa: F401  ensure models register on Base.metadata


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables ensured.")


if __name__ == "__main__":
    asyncio.run(main())
