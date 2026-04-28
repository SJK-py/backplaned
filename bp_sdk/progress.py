"""bp_sdk.progress — ProgressEmitter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bp_protocol.frames import ProgressFrame

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher


class ProgressEmitter:
    """Best-effort emitter — never blocks the handler.

    Backpressure: when the per-socket outbox is full, `chunk` events are
    coalesced (concatenated) and oldest non-chunk events are dropped.
    """

    def __init__(self, ctx: "TaskContext", dispatcher: "Dispatcher") -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher

    async def emit(
        self,
        event: str,
        content: str = "",
        **metadata: Any,
    ) -> None:
        frame = ProgressFrame(
            agent_id=self._dispatcher.agent.info.agent_id,
            trace_id=self._ctx.trace_id,
            span_id=self._ctx.span_id,
            task_id=self._ctx.task_id,
            event=event,
            content=content,
            metadata=metadata,
        )
        try:
            self._dispatcher.transport  # type: ignore[truthy-function]
            # Use a non-blocking put so a slow consumer cannot stall the
            # handler. The dispatcher's outbox loop applies the actual
            # backpressure policy.
            await self._dispatcher.transport.send(frame)
        except Exception:  # noqa: BLE001
            pass

    # Convenience wrappers (sync, fire-and-forget)
    def chunk(self, text: str) -> None:
        import asyncio  # noqa: PLC0415

        asyncio.create_task(self.emit("chunk", text))

    def status(self, status: str) -> None:
        import asyncio  # noqa: PLC0415

        asyncio.create_task(self.emit("status", status))

    def tool_call(self, name: str, args: dict[str, Any]) -> None:
        import asyncio  # noqa: PLC0415

        asyncio.create_task(self.emit("tool_call", name, args=args))

    def tool_result(self, name: str, result: Any) -> None:
        import asyncio  # noqa: PLC0415

        asyncio.create_task(self.emit("tool_result", name, result=result))
