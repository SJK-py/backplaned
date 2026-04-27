"""bp_sdk.peers — How a handler invokes other agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel

from bp_protocol.frames import NewTaskFrame, ResultFrame
from bp_protocol.types import AgentInfo

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher


class PeerClient:
    """Per-task helper bound to one TaskContext.

    Holds back-references so calls can correlate replies into the
    parent's pending_results map.
    """

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
        """Create a child task. With wait=True, returns the Result frame.

        With wait=False, returns the assigned task_id and the parent
        transitions to WAITING_CHILDREN until the result is delivered.
        """
        raise NotImplementedError

    async def delegate(
        self,
        destination_agent_id: str,
        payload: BaseModel,
        *,
        handoff_note: Optional[str] = None,
    ) -> None:
        """Hand off the current task to another agent. Caller should return
        immediately afterwards — the delegated agent terminates the task.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def find(self, capability: str) -> list[AgentInfo]:
        """Ranked list of visible agents that provide `capability`."""
        raise NotImplementedError

    async def describe(self, agent_id: str) -> AgentInfo:
        """Fetch full AgentInfo for a destination."""
        raise NotImplementedError

    def visible(self) -> dict[str, dict[str, Any]]:
        """Local view of available_destinations from the last Welcome.

        Updated when the Welcome frame is refreshed (reconnect, ACL
        rule changes pushed by the router).
        """
        raise NotImplementedError
