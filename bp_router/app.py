"""bp_router.app — FastAPI application factory and lifespan management.

The lifespan boots the database pool, Redis client, file store, ACL
evaluator, observability exporters, and starts background tasks
(timeout sweep, file GC, ACL hot-reload watcher).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from bp_router.settings import Settings, load_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application state — attached to FastAPI's `app.state` for DI in handlers
# ---------------------------------------------------------------------------


class AppState:
    """Shared, long-lived runtime state.

    Attached to `app.state.bp` in `create_app()`. Endpoint handlers and
    WebSocket dispatch reach into this for the DB pool, file store, etc.
    """

    settings: Settings
    db_pool: object  # asyncpg.Pool — typed import deferred to avoid hard dep at import time
    redis: object    # redis.asyncio.Redis | None
    file_store: object  # FileStore (see bp_router.storage)
    socket_registry: object  # SocketRegistry (see bp_router.ws_hub)
    acl: object  # AclEvaluator (see bp_router.acl)
    llm_service: object  # LlmService (see bp_router.llm)
    correlation: object  # PendingAcks (see bp_router.correlation)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot subsystems in dependency order; tear down in reverse."""
    settings = load_settings()
    state = AppState()
    state.settings = settings
    app.state.bp = state

    # 1. Observability (must be first so subsequent init is traced)
    from bp_router.observability import (  # noqa: PLC0415
        configure_logging,
        configure_metrics,
        configure_tracing,
    )

    configure_logging(settings)
    configure_tracing(settings)
    configure_metrics()

    # 2. Database pool
    from bp_router.db.connection import open_pool  # noqa: PLC0415

    state.db_pool = await open_pool(settings)

    # 3. Redis (optional)
    if settings.redis_url:
        from bp_router.db.connection import open_redis  # noqa: PLC0415

        state.redis = await open_redis(settings)
    else:
        state.redis = None

    # 4. File store
    from bp_router.storage import build_file_store  # noqa: PLC0415

    state.file_store = build_file_store(settings)

    # 5. ACL
    from bp_router.acl import AclEvaluator, load_acl_config  # noqa: PLC0415

    state.acl = AclEvaluator(load_acl_config(settings))

    # 6. LLM service
    from bp_router.llm import LlmService  # noqa: PLC0415

    state.llm_service = LlmService(settings)

    # 7. Correlation + socket registry
    from bp_router.correlation import PendingAcks  # noqa: PLC0415
    from bp_router.ws_hub import SocketRegistry  # noqa: PLC0415

    state.correlation = PendingAcks()
    state.socket_registry = SocketRegistry()

    # 8. Background tasks
    from bp_router.tasks import start_background_loops  # noqa: PLC0415

    bg_tasks = await start_background_loops(state)

    logger.info("router_started", extra={"event": "router_started"})

    try:
        yield
    finally:
        for t in bg_tasks:
            t.cancel()
        await state.db_pool.close()  # type: ignore[attr-defined]
        if state.redis is not None:
            await state.redis.aclose()  # type: ignore[attr-defined]
        logger.info("router_stopped", extra={"event": "router_stopped"})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build the FastAPI app. Routers and middleware are registered here."""
    app = FastAPI(
        title="bp_router",
        version="0.1.0",
        lifespan=lifespan,
    )

    # HTTP routers
    from bp_router.api import (  # noqa: PLC0415
        admin,
        auth,
        files,
        health,
        llm,
        onboard,
        sessions,
        tasks,
    )

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/v1/auth", tags=["auth"])
    app.include_router(sessions.router, prefix="/v1/sessions", tags=["sessions"])
    app.include_router(tasks.router, prefix="/v1/tasks", tags=["tasks"])
    app.include_router(files.router, prefix="/v1/files", tags=["files"])
    app.include_router(onboard.router, prefix="/v1", tags=["onboard"])
    app.include_router(admin.router, prefix="/v1/admin", tags=["admin"])
    app.include_router(llm.router, prefix="/v1/llm", tags=["llm"])

    # WebSocket endpoint
    from bp_router.ws_hub import register_ws_endpoint  # noqa: PLC0415

    register_ws_endpoint(app)

    return app
