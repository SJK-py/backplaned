"""bp_router.ws_hub — WebSocket endpoint and live socket registry.

One socket per agent; supersede semantics: a new Hello with the same
agent_id closes the previous socket. See
`docs/router/protocol.md` §3.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from bp_protocol import PROTOCOL_VERSION
from bp_protocol.frames import (
    ErrorCode,
    ErrorFrame,
    Frame,
    HelloFrame,
    PingFrame,
    PongFrame,
    WelcomeFrame,
    parse_frame,
    serialize_frame,
)
from bp_router.db import queries
from bp_router.security.jwt import TokenError, verify_agent_token
from bp_router.visibility import available_destinations, caller_from_agent

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
    outbox: asyncio.Queue[Frame] = field(default_factory=lambda: asyncio.Queue(256))
    last_recv: float = 0.0
    last_send: float = 0.0
    inflight_correlations: set[str] = field(default_factory=set)
    llm_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    """correlation_id → router-side asyncio.Task running an LLM call.

    Cancelled by `_on_disconnect` so a dropped client doesn't keep
    consuming provider tokens.
    """
    closed: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SocketRegistry:
    """`agent_id → SocketEntry` with supersede + resume support."""

    def __init__(self) -> None:
        self._live: dict[str, SocketEntry] = {}
        self._resume: dict[str, SocketEntry] = {}

    async def attach(self, entry: SocketEntry) -> Optional[SocketEntry]:
        previous = self._live.pop(entry.agent_id, None)
        self._live[entry.agent_id] = entry
        return previous

    async def detach(
        self, agent_id: str, *, into_resume: bool
    ) -> Optional[SocketEntry]:
        entry = self._live.pop(agent_id, None)
        if entry is not None and into_resume:
            self._resume[agent_id] = entry
        return entry

    def consume_resume(
        self, agent_id: str, token: str
    ) -> Optional[SocketEntry]:
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
            try:
                await ws.send_text(
                    serialize_frame(
                        ErrorFrame(
                            agent_id="router",
                            trace_id="0" * 32,
                            span_id="0" * 16,
                            code=exc.code,
                            message=exc.reason,
                        )
                    )
                )
            except Exception:  # noqa: BLE001
                pass
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
# Handshake
# ---------------------------------------------------------------------------


class _HandshakeFailed(Exception):
    def __init__(
        self, reason: str, *, code: str = "auth_failed", close_code: int = 4001
    ) -> None:
        self.reason = reason
        self.code = code
        self.close_code = close_code


async def _handshake(ws: WebSocket, state: "AppState") -> SocketEntry:
    """Read Hello, validate auth, register, send Welcome."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
    except asyncio.TimeoutError as exc:
        raise _HandshakeFailed("hello_timeout") from exc

    try:
        frame = parse_frame(raw)
    except ValidationError as exc:
        raise _HandshakeFailed(
            f"frame_invalid: {exc.errors()[0]['msg']}",
            code=ErrorCode.FRAME_INVALID,
            close_code=1002,
        ) from exc

    if not isinstance(frame, HelloFrame):
        raise _HandshakeFailed(
            f"expected Hello, got {frame.type}",
            code=ErrorCode.FRAME_INVALID,
            close_code=1002,
        )

    if frame.protocol_version != PROTOCOL_VERSION:
        raise _HandshakeFailed(
            f"protocol_version mismatch: {frame.protocol_version}",
            code=ErrorCode.PROTOCOL_VERSION,
            close_code=1002,
        )

    settings = state.settings  # type: ignore[attr-defined]
    try:
        principal = verify_agent_token(
            frame.auth_token,
            secret=settings.jwt_secret.get_secret_value(),
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError as exc:
        raise _HandshakeFailed(f"auth_failed: {exc}", code=ErrorCode.AUTH_FAILED) from exc

    if principal.agent_id != frame.agent_id:
        raise _HandshakeFailed(
            "auth_failed: token sub does not match Hello.agent_id",
            code=ErrorCode.AUTH_FAILED,
        )

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        agent_row = await queries.get_agent(conn, principal.agent_id)
    if agent_row is None:
        raise _HandshakeFailed(
            f"unknown agent: {principal.agent_id}",
            code=ErrorCode.AUTH_FAILED,
        )
    if agent_row.status != "active":
        raise _HandshakeFailed(
            f"agent suspended: {principal.agent_id}",
            code=ErrorCode.AGENT_SUSPENDED,
        )

    # Resume?
    resumed = None
    if frame.resume_token:
        resumed = state.socket_registry.consume_resume(  # type: ignore[attr-defined]
            principal.agent_id, frame.resume_token
        )

    if resumed is not None:
        entry = SocketEntry(
            agent_id=principal.agent_id,
            websocket=ws,
            session_token=resumed.session_token,
            outbox=resumed.outbox,
            inflight_correlations=resumed.inflight_correlations,
        )
    else:
        entry = SocketEntry(
            agent_id=principal.agent_id,
            websocket=ws,
            session_token=secrets.token_urlsafe(24),
        )

    previous = await state.socket_registry.attach(entry)  # type: ignore[attr-defined]
    if previous is not None:
        try:
            await previous.websocket.close(code=4001, reason="superseded")
        except Exception:  # noqa: BLE001
            pass
        previous.closed.set()

    try:
        from bp_router.observability.metrics import ws_connected_agents  # noqa: PLC0415

        ws_connected_agents.set(len(state.socket_registry))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    # Build Welcome.
    async with pool.acquire() as conn:
        all_agents = await queries.list_agents(conn)
        await queries.update_agent_last_seen(conn, principal.agent_id)

    caller = caller_from_agent(agent_row)
    catalog = available_destinations(caller, all_agents, state.acl)  # type: ignore[attr-defined]

    welcome = WelcomeFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        session_id=entry.session_token,
        available_destinations=catalog,
        capabilities=agent_row.capabilities,
        heartbeat_interval_ms=settings.heartbeat_interval_ms,
        max_payload_bytes=settings.max_payload_bytes,
    )
    await ws.send_text(serialize_frame(welcome))

    logger.info(
        "agent_connected",
        extra={
            "event": "agent_connected",
            "bp.agent_id": principal.agent_id,
            "resumed": resumed is not None,
        },
    )
    return entry


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


async def _run_socket(entry: SocketEntry, state: "AppState") -> None:
    """Run recv, send, and heartbeat coroutines until any one ends."""
    settings = state.settings  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    entry.last_recv = loop.time()
    entry.last_send = loop.time()

    recv_task = asyncio.create_task(_recv_loop(entry, state))
    send_task = asyncio.create_task(_send_loop(entry))
    hb_task = asyncio.create_task(
        _heartbeat_loop(entry, state, interval_s=settings.heartbeat_interval_ms / 1000)
    )

    tasks = [recv_task, send_task, hb_task]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )
        # Surface any exception so the caller's `except` blocks can log it.
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.exception(
                    "ws_loop_exception",
                    extra={"event": "ws_loop_exception", "bp.agent_id": entry.agent_id},
                    exc_info=exc,
                )
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _recv_loop(entry: SocketEntry, state: "AppState") -> None:
    from bp_router.dispatch import dispatch_frame  # noqa: PLC0415

    settings = state.settings  # type: ignore[attr-defined]
    loop = asyncio.get_running_loop()
    while not entry.closed.is_set():
        raw = await entry.websocket.receive_text()
        entry.last_recv = loop.time()

        if len(raw.encode("utf-8")) > settings.max_payload_bytes:
            await entry.websocket.close(code=1009, reason="payload_too_large")
            return

        try:
            frame = parse_frame(raw)
        except ValidationError as exc:
            err = ErrorFrame(
                agent_id="router",
                trace_id="0" * 32,
                span_id="0" * 16,
                code=ErrorCode.FRAME_INVALID,
                message=str(exc.errors()[0]["msg"]),
            )
            await entry.outbox.put(err)
            continue

        try:
            await dispatch_frame(state, entry, frame)
        except Exception:  # noqa: BLE001
            logger.exception(
                "dispatch_failed",
                extra={
                    "event": "dispatch_failed",
                    "bp.agent_id": entry.agent_id,
                    "bp.frame.type": frame.type,
                },
            )


async def _send_loop(entry: SocketEntry) -> None:
    loop = asyncio.get_running_loop()
    while not entry.closed.is_set():
        frame = await entry.outbox.get()
        await entry.websocket.send_text(serialize_frame(frame))
        entry.last_send = loop.time()
        try:
            from bp_router.observability.metrics import frames_total  # noqa: PLC0415

            frames_total.labels(
                direction="send", type=frame.type, agent_id=entry.agent_id
            ).inc()
        except Exception:  # noqa: BLE001
            pass


async def _heartbeat_loop(
    entry: SocketEntry, state: "AppState", *, interval_s: float
) -> None:
    loop = asyncio.get_running_loop()
    misses = 0
    max_misses = 2
    while not entry.closed.is_set():
        await asyncio.sleep(interval_s)
        idle = loop.time() - entry.last_recv
        if idle < interval_s:
            misses = 0
            continue
        # Send Ping; expect Pong via ack-correlation.
        ping = PingFrame(
            agent_id="router",
            trace_id="0" * 32,
            span_id="0" * 16,
        )
        fut = state.correlation.register(  # type: ignore[attr-defined]
            ping.correlation_id, timeout_s=interval_s
        )
        await entry.outbox.put(ping)
        try:
            await fut
            misses = 0
        except (TimeoutError, asyncio.CancelledError):
            misses += 1
            if misses >= max_misses:
                logger.info(
                    "heartbeat_timeout",
                    extra={
                        "event": "heartbeat_timeout",
                        "bp.agent_id": entry.agent_id,
                    },
                )
                try:
                    await entry.websocket.close(
                        code=4002, reason="heartbeat_timeout"
                    )
                except Exception:  # noqa: BLE001
                    pass
                entry.closed.set()
                return


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


async def _on_disconnect(entry: SocketEntry, state: "AppState") -> None:
    """Move into resume window if applicable, else fail in-flight tasks."""
    entry.closed.set()
    settings = state.settings  # type: ignore[attr-defined]

    # Cancel any in-flight LLM router-side tasks for this agent so we
    # stop burning provider tokens on a dead client.
    for cid, task in list(entry.llm_tasks.items()):
        task.cancel()
    entry.llm_tasks.clear()

    try:
        from bp_router.observability.metrics import (  # noqa: PLC0415
            ws_connected_agents,
            ws_disconnects_total,
        )

        ws_connected_agents.set(len(state.socket_registry))  # type: ignore[attr-defined]
        ws_disconnects_total.labels(reason="closed").inc()
    except Exception:  # noqa: BLE001
        pass

    # Reject any frame-level pending acks tied to this socket's correlations.
    if entry.inflight_correlations:
        rejected = state.correlation.reject_all_for(  # type: ignore[attr-defined]
            lambda cid: cid in entry.inflight_correlations
        )
        logger.info(
            "rejected_pending_acks_on_disconnect",
            extra={
                "event": "rejected_pending_acks_on_disconnect",
                "bp.agent_id": entry.agent_id,
                "count": rejected,
            },
        )

    # Park into resume window so a fast reconnect can re-attach.
    parked = await state.socket_registry.detach(  # type: ignore[attr-defined]
        entry.agent_id, into_resume=True
    )
    if parked is None:
        # Already superseded by a newer socket; nothing to do.
        return

    # Schedule the resume window: if no Hello+resume_token arrives in time,
    # fail in-flight tasks for this agent.
    asyncio.create_task(
        _resume_window_expiry(entry, state, ttl_s=settings.resume_window_s)
    )


async def _resume_window_expiry(
    entry: SocketEntry, state: "AppState", *, ttl_s: int
) -> None:
    from bp_router.tasks import fail_inflight_for_agent  # noqa: PLC0415

    await asyncio.sleep(ttl_s)
    # If a new socket attached in the meantime, the resume entry has been
    # consumed and we have nothing to do.
    if state.socket_registry._resume.get(entry.agent_id) is not entry:  # type: ignore[attr-defined]
        return
    state.socket_registry._resume.pop(entry.agent_id, None)  # type: ignore[attr-defined]

    failed = await fail_inflight_for_agent(state, entry.agent_id)
    logger.info(
        "agent_disconnect_finalised",
        extra={
            "event": "agent_disconnect_finalised",
            "bp.agent_id": entry.agent_id,
            "failed_tasks": failed,
        },
    )
