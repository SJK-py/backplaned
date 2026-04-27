"""bp_router.ws_hub — WebSocket endpoint and live socket registry.

One socket per agent; supersede semantics: a new Hello with the same
agent_id closes the previous socket. See
`docs/design/router/protocol.md` §3.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from bp_protocol.frames import Frame

if TYPE_CHECKING:
    from bp_router.app import AppState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-socket state
# ---------------------------------------------------------------------------


@dataclass
class SocketEntry:
    agent_id: str
    websocket: WebSocket
    session_token: str
    """Server-issued token; sent in Welcome and required for resume."""
    outbox: asyncio.Queue[Frame] = field(default_factory=lambda: asyncio.Queue(256))
    last_recv: float = 0.0
    last_send: float = 0.0
    inflight_correlations: set[str] = field(default_factory=set)
    """correlation_ids of frames sent to this socket awaiting ack."""

    closed: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SocketRegistry:
    """`agent_id → SocketEntry` with supersede + resume support.

    Not thread-safe; relies on the single asyncio event loop. For multi-
    worker deployments, the load balancer sticky-routes by agent_id so
    one agent's socket lives on one worker.
    """

    def __init__(self) -> None:
        self._live: dict[str, SocketEntry] = {}
        self._resume: dict[str, SocketEntry] = {}
        """agent_id → entry recently disconnected; cleared on resume window expiry."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def attach(self, entry: SocketEntry) -> Optional[SocketEntry]:
        """Register a new live socket. Supersedes any prior; returns the
        previous entry so the caller can close it cleanly."""
        previous = self._live.pop(entry.agent_id, None)
        self._live[entry.agent_id] = entry
        return previous

    async def detach(self, agent_id: str, *, into_resume: bool) -> Optional[SocketEntry]:
        """Remove a socket from the live map. If `into_resume`, park it
        in the resume map until the window expires."""
        entry = self._live.pop(agent_id, None)
        if entry is not None and into_resume:
            self._resume[agent_id] = entry
        return entry

    def consume_resume(self, agent_id: str, token: str) -> Optional[SocketEntry]:
        """Look up a parked entry by agent_id + session_token."""
        entry = self._resume.get(agent_id)
        if entry is None or entry.session_token != token:
            return None
        return self._resume.pop(agent_id)

    def get(self, agent_id: str) -> Optional[SocketEntry]:
        return self._live.get(agent_id)

    def live_agent_ids(self) -> list[str]:
        return list(self._live.keys())

    def __len__(self) -> int:
        return len(self._live)


# ---------------------------------------------------------------------------
# WS endpoint
# ---------------------------------------------------------------------------


def register_ws_endpoint(app: FastAPI) -> None:
    """Register the `/v1/agent` WebSocket endpoint on the FastAPI app."""

    @app.websocket("/v1/agent")
    async def agent_ws(ws: WebSocket) -> None:
        await ws.accept()
        state: "AppState" = ws.app.state.bp

        try:
            entry = await _handshake(ws, state)
        except _HandshakeFailed as exc:
            logger.warning(
                "agent_handshake_failed",
                extra={"event": "agent_handshake_failed", "reason": exc.reason},
            )
            await ws.close(code=exc.close_code, reason=exc.reason)
            return

        try:
            await _run_socket(entry, state)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("agent_socket_loop_failed")
        finally:
            await _on_disconnect(entry, state)


# ---------------------------------------------------------------------------
# Internals (signatures only; impl in a follow-up)
# ---------------------------------------------------------------------------


class _HandshakeFailed(Exception):
    def __init__(self, reason: str, close_code: int = 4001) -> None:
        self.reason = reason
        self.close_code = close_code


async def _handshake(ws: WebSocket, state: "AppState") -> SocketEntry:
    """Read the first frame, validate Hello, register, send Welcome.

    Steps (`docs/design/router/protocol.md` §3.2):
      1. recv → parse_frame → expect HelloFrame.
      2. Validate auth_token (JWT signature, expiry, revocation list).
      3. Look up agent row; verify status.
      4. Optional: resume via resume_token.
      5. Supersede any prior live socket.
      6. Build Welcome with ACL-filtered destinations.
    """
    raise NotImplementedError


async def _run_socket(entry: SocketEntry, state: "AppState") -> None:
    """Run the three coroutines per socket: receive, send, heartbeat."""
    raise NotImplementedError


async def _on_disconnect(entry: SocketEntry, state: "AppState") -> None:
    """Move into resume if within window, else fail in-flight tasks."""
    raise NotImplementedError
