"""bp_router.db.connection — Postgres pool and Redis client lifecycle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bp_router.settings import Settings

if TYPE_CHECKING:
    import asyncpg
    import redis.asyncio as aredis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


async def open_pool(settings: Settings) -> "asyncpg.Pool":
    """Create the asyncpg connection pool.

    Sets a per-connection statement timeout from `settings.db_statement_timeout_ms`
    so a runaway query cannot block the worker indefinitely. Application
    code should also set per-query deadlines via `asyncio.wait_for`.
    """
    import asyncpg  # noqa: PLC0415

    async def _init(conn: "asyncpg.Connection") -> None:
        await conn.execute(
            f"SET statement_timeout = {settings.db_statement_timeout_ms}"
        )
        # Register JSON codec so dict ↔ jsonb is automatic.
        import json  # noqa: PLC0415

        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        await conn.set_type_codec(
            "json",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    pool = await asyncpg.create_pool(
        dsn=settings.db_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        init=_init,
    )
    logger.info(
        "db_pool_opened",
        extra={
            "event": "db_pool_opened",
            "min_size": settings.db_pool_min_size,
            "max_size": settings.db_pool_max_size,
        },
    )
    return pool


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


async def open_redis(settings: Settings) -> "aredis.Redis":
    """Open a Redis async client. Required for multi-worker deployments."""
    import redis.asyncio as aredis  # noqa: PLC0415

    if settings.redis_url is None:
        raise RuntimeError("open_redis called but settings.redis_url is None")
    client = aredis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    await client.ping()
    logger.info("redis_opened", extra={"event": "redis_opened"})
    return client
