"""bp_sdk.peers — How a handler invokes other agents."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel

from bp_protocol.frames import (
    AckFrame,
    NewTaskFrame,
    ResultFrame,
)
from bp_protocol.types import AgentInfo, TaskPriority

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher

logger = logging.getLogger(__name__)


class PeerCallError(Exception):
    """Raised when spawn/delegate fails before the destination accepts."""


class PeerClient:
    """Per-task helper bound to one TaskContext."""

    def __init__(self, ctx: "TaskContext", dispatcher: "Dispatcher") -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------
    # Spawn / delegate
    # ------------------------------------------------------------------

    async def spawn(
        self,
        destination_agent_id: str,
        payload: BaseModel,
        *,
        wait: bool = True,
        timeout_s: Optional[float] = None,
        idempotency_key: Optional[str] = None,
    ) -> ResultFrame | str:
        """Create a child task. Returns the Result with wait=True, else
        the assigned task_id (the parent transitions to WAITING_CHILDREN
        until the result is delivered).
        """
        frame = NewTaskFrame(
            agent_id=self._dispatcher.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=None,  # spawn
            parent_task_id=self._ctx.task_id,
            destination_agent_id=destination_agent_id,
            user_id=self._ctx.user_id,
            session_id=self._ctx.session_id,
            priority=TaskPriority.NORMAL,
            idempotency_key=idempotency_key,
            payload=payload.model_dump(),
        )

        # Register a pending ack so we can pick up the assigned task_id.
        ack_fut = self._dispatcher.pending_acks.register(frame.correlation_id)
        await self._dispatcher.transport.send(frame)
        try:
            ack = await ack_fut
        except TimeoutError as exc:
            raise PeerCallError("spawn ack timed out") from exc
        if not isinstance(ack, AckFrame) or not ack.accepted:
            reason = ack.reason if isinstance(ack, AckFrame) else "unknown"
            raise PeerCallError(f"router rejected spawn: {reason}")
        if ack.task_id is None:
            raise PeerCallError("router accepted spawn but did not assign task_id")

        if not wait:
            return ack.task_id

        # Register a pending Result keyed on the assigned task_id.
        result_fut = self._dispatcher.pending_results.register(
            ack.task_id, timeout_s=timeout_s
        )
        try:
            result = await result_fut
        except TimeoutError as exc:
            raise PeerCallError(
                f"spawn result timeout for task {ack.task_id}"
            ) from exc
        if not isinstance(result, ResultFrame):
            raise PeerCallError("unexpected response while awaiting Result")
        return result

    async def delegate(
        self,
        destination_agent_id: str,
        payload: BaseModel,
        *,
        handoff_note: Optional[str] = None,
    ) -> None:
        """Hand off the current task. The current handler should return
        immediately afterwards — the delegated agent will terminate the
        task with the parent's task_id.
        """
        frame = NewTaskFrame(
            agent_id=self._dispatcher.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=self._ctx.task_id,  # delegate preserves task_id
            parent_task_id=self._ctx.parent_task_id,
            destination_agent_id=destination_agent_id,
            user_id=self._ctx.user_id,
            session_id=self._ctx.session_id,
            priority=TaskPriority.NORMAL,
            payload={
                **payload.model_dump(),
                **({"handoff_note": handoff_note} if handoff_note else {}),
            },
        )
        ack_fut = self._dispatcher.pending_acks.register(frame.correlation_id)
        await self._dispatcher.transport.send(frame)
        try:
            ack = await ack_fut
        except TimeoutError as exc:
            raise PeerCallError("delegate ack timed out") from exc
        if not isinstance(ack, AckFrame) or not ack.accepted:
            raise PeerCallError(
                f"router rejected delegate: {ack.reason if isinstance(ack, AckFrame) else 'unknown'}"
            )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def visible(self) -> dict[str, dict[str, Any]]:
        """Return the catalog from the last Welcome frame."""
        welcome = getattr(self._dispatcher.transport, "welcome", None)
        if welcome is None:
            return {}
        return welcome.available_destinations

    async def find(self, capability: str) -> list[AgentInfo]:
        """Visible agents that provide `capability`."""
        out: list[AgentInfo] = []
        for agent_id, entry in self.visible().items():
            if capability in entry.get("capabilities", []):
                out.append(
                    AgentInfo(
                        agent_id=agent_id,
                        description=entry.get("description", ""),
                        capabilities=entry.get("capabilities", []),
                        tags=entry.get("tags", []),
                        accepts_schema=entry.get("accepts_schema"),
                        documentation_url=entry.get("documentation_url"),
                        hidden=entry.get("hidden", False),
                    )
                )
        return out

    async def describe(self, agent_id: str) -> AgentInfo:
        """Full AgentInfo for a destination from the cached catalog."""
        entry = self.visible().get(agent_id)
        if entry is None:
            raise PeerCallError(f"agent {agent_id!r} not visible")
        return AgentInfo(
            agent_id=agent_id,
            description=entry.get("description", ""),
            capabilities=entry.get("capabilities", []),
            tags=entry.get("tags", []),
            accepts_schema=entry.get("accepts_schema"),
            documentation_url=entry.get("documentation_url"),
            hidden=entry.get("hidden", False),
        )
