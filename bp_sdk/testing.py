"""bp_sdk.testing — TestRouter harness for unit and integration tests.

Spins up an in-process router with sqlite-memory DB, the local file
store, and ACL set to allow-all. Useful for testing agents without
infrastructure.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from pydantic import BaseModel

if TYPE_CHECKING:
    from bp_protocol.frames import ResultFrame
    from bp_sdk.agent import Agent


class TestRouter:
    """Lightweight router harness for tests.

    Two modes:
      - In-process only: agents register via `register_embedded()`.
      - Bound port: `serve()` exposes a real WebSocket so external
        agents can connect normally.
    """

    def __init__(self) -> None:
        self._embedded_agents: dict[str, Any] = {}
        self._port: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TestRouter":
        await self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _start(self) -> None:
        # Implementation: build a Settings with sqlite-memory DSN +
        # LocalFileStore in tmpdir + permissive ACL config; instantiate
        # the FastAPI app and start it via httpx.ASGITransport (for
        # in-process) or a uvicorn server on a random port (for serve()).
        raise NotImplementedError

    async def _stop(self) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Agent registration
    # ------------------------------------------------------------------

    def register_embedded(self, agent: "Agent") -> None:
        self._embedded_agents[agent.info.agent_id] = agent

    async def serve(self) -> int:
        """Start a real WebSocket on a random local port. Returns the port."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Direct invocation
    # ------------------------------------------------------------------

    async def call(
        self,
        agent_id: str,
        payload: BaseModel,
        *,
        user_id: str = "test-user",
        session_id: Optional[str] = None,
        timeout_s: float = 30.0,
    ) -> "ResultFrame":
        """Drive the full pipeline (frames, ACL, state machine) without
        sockets. Returns the final Result frame.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def captured_logs() -> AsyncIterator[list[dict[str, Any]]]:
    """Capture structured log records emitted while the block runs."""
    raise NotImplementedError


@asynccontextmanager
async def captured_metrics() -> AsyncIterator[dict[str, float]]:
    """Snapshot Prometheus counter deltas during the block."""
    raise NotImplementedError
