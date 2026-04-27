"""bp_sdk.dispatch — Receive loop, send queue drain, heartbeat.

Runs three coroutines per agent: receive, send-drain, heartbeat. The
receive loop classifies incoming frames and dispatches:
  - NewTask → handler invocation
  - Result for our peer calls → resolve correlated Future
  - Cancel → trip cancel token on the matching task
  - Progress for our peer calls → forward to subscriber
  - Ack → resolve send-side Future
  - Ping → respond Pong
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    Frame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
)
from bp_sdk.correlation import PendingMap

if TYPE_CHECKING:
    from bp_sdk.agent import Agent
    from bp_sdk.transport.base import Transport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclass
class _ActiveTask:
    task_id: str
    cancel_token: Any  # CancelToken — typed loosely to avoid circular import
    handler_task: asyncio.Task


class Dispatcher:
    """The runtime that ties Agent + Transport together."""

    def __init__(self, agent: "Agent", transport: "Transport") -> None:
        self.agent = agent
        self.transport = transport
        self.pending_acks = PendingMap(
            default_timeout_s=agent.config.pending_acks_timeout_s,
        )
        self.pending_results = PendingMap(
            default_timeout_s=agent.config.pending_results_timeout_s,
        )
        self._active: dict[str, _ActiveTask] = {}
        self._loops: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Run / shutdown
    # ------------------------------------------------------------------

    async def run_until(self, stop_event: asyncio.Event) -> None:
        self.pending_acks.start_reaper()
        self.pending_results.start_reaper()

        self._loops = [
            asyncio.create_task(self._recv_loop()),
            # Heartbeat loop is owned by the WebSocket transport itself.
        ]

        await stop_event.wait()

        # Graceful shutdown: stop accepting new tasks (handled by recv loop
        # noticing closed transport), drain in-flight, then return.
        await self._drain_in_flight(grace_s=30.0)
        for t in self._loops:
            t.cancel()

    async def _drain_in_flight(self, *, grace_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + grace_s
        while self._active:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                # Trip cancel tokens on remaining tasks
                for entry in self._active.values():
                    entry.cancel_token.trip("shutdown")
                break
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        while True:
            try:
                frame = await self.transport.recv()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                logger.exception("recv_failed", extra={"event": "recv_failed"})
                continue

            try:
                await self._dispatch(frame)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "dispatch_failed",
                    extra={"event": "dispatch_failed", "type": frame.type},
                )

    async def _dispatch(self, frame: Frame) -> None:
        if isinstance(frame, NewTaskFrame):
            await self._handle_new_task(frame)
        elif isinstance(frame, ResultFrame):
            self.pending_results.resolve(frame.correlation_id, frame)
        elif isinstance(frame, ProgressFrame):
            # Forward to any peer-call subscriber listening for this task.
            await self._handle_progress(frame)
        elif isinstance(frame, CancelFrame):
            await self._handle_cancel(frame)
        elif isinstance(frame, AckFrame):
            self.pending_acks.resolve(frame.ref_correlation_id, frame)
        elif isinstance(frame, PingFrame):
            pong = PongFrame(
                agent_id=self.agent.info.agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
            )
            await self.transport.send(pong)
        elif isinstance(frame, PongFrame):
            self.pending_acks.resolve(frame.ref_correlation_id, frame)
        else:
            logger.warning(
                "unexpected_frame",
                extra={"event": "unexpected_frame", "type": frame.type},
            )

    # ------------------------------------------------------------------
    # NewTask → handler invocation
    # ------------------------------------------------------------------

    async def _handle_new_task(self, frame: NewTaskFrame) -> None:
        # Implementation: build TaskContext, look up the handler by
        # payload model, validate frame.payload, run the handler under
        # asyncio.create_task, and on completion send the Result.
        raise NotImplementedError

    async def _handle_progress(self, frame: ProgressFrame) -> None:
        # Look up subscriber registered by ctx.peers.spawn(stream=True).
        raise NotImplementedError

    async def _handle_cancel(self, frame: CancelFrame) -> None:
        active = self._active.get(frame.task_id)
        if active is not None:
            active.cancel_token.trip(frame.reason)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_dispatcher(agent: "Agent", transport: "Transport") -> Dispatcher:
    return Dispatcher(agent, transport)
