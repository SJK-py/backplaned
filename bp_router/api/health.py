"""bp_router.api.health — Liveness, readiness, and metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Always returns 200 if the process is up. Suitable for k8s liveness."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readiness(request: Request) -> Response:
    """Returns 200 only if the router can serve requests.

    Checks DB pool reachability and (when configured) Redis.
    """
    state = request.app.state.bp
    try:
        async with state.db_pool.acquire() as conn:
            await conn.execute("SELECT 1")
        if state.redis is not None:
            await state.redis.ping()
    except Exception:  # noqa: BLE001
        return Response(content="not ready", status_code=503)
    return Response(content="ok", status_code=200)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition. Wired up in `bp_router.observability.metrics`."""
    from bp_router.observability.metrics import render_exposition  # noqa: PLC0415

    body = render_exposition()
    return Response(content=body, media_type="text/plain; version=0.0.4")
