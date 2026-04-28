"""bp_sdk.llm — Agent-side LLM service client.

Routes calls to the router-side LlmService over the same WebSocket frame
channel that carries every other agent traffic. Streaming generates
yield `LlmDelta` chunks; the iterator ends when the terminal
`LlmResult` arrives.

See `docs/sdk/services.md` §1.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, Optional, Union

from bp_protocol.frames import (
    CancelFrame,
    LlmDeltaFrame,
    LlmRequestFrame,
    LlmResultFrame,
)
from bp_sdk.errors import CancellationError

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider-neutral types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]]]
    name: Optional[str] = None
    tool_call_id: Optional[str] = None

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            out["name"] = self.name
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        return out


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


ToolChoice = Union[Literal["auto", "none", "required"], dict[str, Any]]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LlmResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LlmDelta:
    text: Optional[str] = None
    tool_call: Optional[ToolCall] = None
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None


class LlmCallError(RuntimeError):
    """Raised when the router returns an `LlmResultFrame` with `error` set."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


# Sentinel pushed to a streaming queue to signal the terminal LlmResult.
_END = object()

# Sentinel pushed to a streaming queue when cancel_token trips so the
# awaiting `queue.get()` unblocks with a real value (avoids racing two
# coroutines and leaving the unscheduled one warning at GC).
_CANCEL_SENTINEL = object()


class LlmServiceClient:
    """Per-task LLM facade. Routes calls over the agent's WebSocket.

    Lifetime is the task; constructed by the dispatcher. The dispatcher
    routes incoming `LlmDelta` and `LlmResult` frames to the right
    pending future / streaming queue keyed on `correlation_id`.
    """

    def __init__(self, ctx: "TaskContext", dispatcher: "Dispatcher") -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher

    @property
    def _agent_id(self) -> str:
        return self._dispatcher.agent.info.agent_id

    @property
    def _trace_id(self) -> str:
        return self._ctx.trace_id

    @property
    def _span_id(self) -> str:
        return self._ctx.span_id

    # ------------------------------------------------------------------
    # Cancel-aware await helpers
    # ------------------------------------------------------------------

    async def _send_abort(self, request: LlmRequestFrame) -> None:
        """Tell the router to cancel a specific LLM call."""
        cancel = CancelFrame(
            agent_id=self._agent_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
            task_id=None,
            ref_correlation_id=request.correlation_id,
            reason=self._ctx.cancel_token.reason or "cancelled",
        )
        try:
            await self._dispatcher.transport.send(cancel)
        except Exception:  # noqa: BLE001
            # Cancellation is best-effort — handler is about to bail anyway.
            pass

    async def _await_with_cancel_future(
        self,
        fut: "asyncio.Future",
        request: LlmRequestFrame,
    ) -> Any:
        """Await a Future from PendingMap while watching cancel_token.

        On cancel, reject the future so the await unblocks, send abort,
        raise CancellationError. The PendingMap exception path means the
        late-arriving result (if any) is discarded.
        """
        if self._ctx.cancel_token.cancelled:
            await self._send_abort(request)
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")

        async def _watch() -> None:
            await self._ctx.cancel_token.wait()
            if not fut.done():
                fut.set_exception(CancellationError(
                    self._ctx.cancel_token.reason or "cancelled"
                ))

        watcher = asyncio.create_task(_watch())
        try:
            return await fut
        except CancellationError:
            await self._send_abort(request)
            raise
        finally:
            watcher.cancel()
            try:
                await watcher
            except BaseException:  # noqa: BLE001
                pass

    async def _queue_get_or_cancel(
        self,
        queue: "asyncio.Queue",
        request: LlmRequestFrame,
    ) -> Any:
        """Pull the next item from `queue` while watching cancel_token.

        On cancel, push a sentinel onto the queue so the get() unblocks
        cleanly with a real value (rather than racing two coroutines
        and leaving one unstarted), send abort, raise CancellationError.
        """
        if self._ctx.cancel_token.cancelled:
            await self._send_abort(request)
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")

        sentinel = _CANCEL_SENTINEL

        async def _watch() -> None:
            await self._ctx.cancel_token.wait()
            queue.put_nowait(sentinel)

        watcher = asyncio.create_task(_watch())
        try:
            item = await queue.get()
            if item is sentinel:
                await self._send_abort(request)
                raise CancellationError(
                    self._ctx.cancel_token.reason or "cancelled"
                )
            return item
        finally:
            watcher.cancel()
            try:
                await watcher
            except BaseException:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # generate
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: Union[str, list[Message]],
        *,
        model: str = "default",
        tools: Optional[list[ToolSpec]] = None,
        tool_choice: Optional[ToolChoice] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        provider_options: Optional[dict[str, Any]] = None,
    ) -> Union[LlmResponse, AsyncIterator[LlmDelta]]:
        if self._ctx.cancel_token.cancelled:
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")
        if isinstance(prompt, str):
            messages = [Message(role="user", content=prompt)]
        else:
            messages = prompt

        request = LlmRequestFrame(
            agent_id=self._agent_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
            kind="generate",
            model=model,
            messages=[m.model_dump() for m in messages],
            tools=[t.model_dump() for t in tools] if tools else [],
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            provider_options=provider_options,
            user_id=self._ctx.user_id,
            task_id=(
                self._ctx.task_id if self._ctx.task_id != "<spawn>" else None
            ),
        )

        if stream:
            return self._stream(request)

        # Non-streaming: register a single-shot pending result on
        # correlation_id and await one LlmResult.
        fut = self._dispatcher.pending_results.register(request.correlation_id)
        await self._dispatcher.transport.send(request)
        try:
            result = await self._await_with_cancel_future(fut, request)
        except CancellationError:
            self._dispatcher.pending_results.reject(
                request.correlation_id, CancellationError("cancelled")
            )
            raise
        except TimeoutError as exc:
            raise LlmCallError("LLM request timed out") from exc
        return _result_to_response(result)

    async def _stream(self, request: LlmRequestFrame) -> AsyncIterator[LlmDelta]:
        # Streaming: register a queue keyed on correlation_id BEFORE sending,
        # so deltas arriving back-to-back with the request are not dropped.
        queue: asyncio.Queue = asyncio.Queue()
        self._dispatcher._llm_streams[request.correlation_id] = queue
        try:
            await self._dispatcher.transport.send(request)
            while True:
                # Race the next item against cancellation so a Cancel
                # frame mid-stream aborts cleanly. The watcher pushes a
                # sentinel on cancel rather than racing two awaits.
                item = await self._queue_get_or_cancel(queue, request)
                if item is _END:
                    return
                if isinstance(item, LlmResultFrame):
                    if item.error:
                        raise LlmCallError(
                            f"{item.error.get('code', 'error')}: "
                            f"{item.error.get('message', '')}"
                        )
                    return
                yield item  # already typed as LlmDelta
        finally:
            self._dispatcher._llm_streams.pop(request.correlation_id, None)

    # ------------------------------------------------------------------
    # embed
    # ------------------------------------------------------------------

    async def embed(
        self,
        text: Union[str, list[str]],
        *,
        model: str = "default",
    ) -> list[list[float]]:
        if self._ctx.cancel_token.cancelled:
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")
        if isinstance(text, str):
            text_list = [text]
        else:
            text_list = list(text)

        request = LlmRequestFrame(
            agent_id=self._agent_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
            kind="embed",
            model=model,
            text=text_list,
            user_id=self._ctx.user_id,
        )
        fut = self._dispatcher.pending_results.register(request.correlation_id)
        await self._dispatcher.transport.send(request)
        try:
            result: LlmResultFrame = await self._await_with_cancel_future(fut, request)
        except CancellationError:
            self._dispatcher.pending_results.reject(
                request.correlation_id, CancellationError("cancelled")
            )
            raise
        if result.error:
            raise LlmCallError(
                f"{result.error.get('code', 'error')}: "
                f"{result.error.get('message', '')}"
            )
        return result.vectors

    # ------------------------------------------------------------------
    # count_tokens
    # ------------------------------------------------------------------

    async def count_tokens(
        self,
        prompt: Union[str, list[Message]],
        *,
        model: str = "default",
    ) -> int:
        if self._ctx.cancel_token.cancelled:
            raise CancellationError(self._ctx.cancel_token.reason or "cancelled")
        if isinstance(prompt, str):
            messages = [Message(role="user", content=prompt).model_dump()]
        else:
            messages = [m.model_dump() for m in prompt]

        request = LlmRequestFrame(
            agent_id=self._agent_id,
            trace_id=self._trace_id,
            span_id=self._span_id,
            kind="count_tokens",
            model=model,
            messages=messages,
        )
        fut = self._dispatcher.pending_results.register(request.correlation_id)
        await self._dispatcher.transport.send(request)
        try:
            result: LlmResultFrame = await self._await_with_cancel_future(fut, request)
        except CancellationError:
            self._dispatcher.pending_results.reject(
                request.correlation_id, CancellationError("cancelled")
            )
            raise
        if result.error:
            raise LlmCallError(
                f"{result.error.get('code', 'error')}: "
                f"{result.error.get('message', '')}"
            )
        return result.total_tokens

    async def aclose(self) -> None:
        # Frame channel doesn't own a connection; nothing to close.
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_to_response(result: LlmResultFrame) -> LlmResponse:
    if result.error:
        raise LlmCallError(
            f"{result.error.get('code', 'error')}: "
            f"{result.error.get('message', '')}"
        )
    return LlmResponse(
        text=result.text,
        tool_calls=[
            ToolCall(id=tc["id"], name=tc["name"], args=tc.get("args", {}))
            for tc in result.tool_calls
        ],
        finish_reason=result.finish_reason,
        usage=TokenUsage(
            input_tokens=result.usage.get("input_tokens", 0),
            output_tokens=result.usage.get("output_tokens", 0),
        ),
        raw=result.raw,
    )


def _frame_delta_to_delta(frame: LlmDeltaFrame) -> LlmDelta:
    tool_call = None
    if frame.tool_call:
        tc = frame.tool_call
        tool_call = ToolCall(id=tc["id"], name=tc["name"], args=tc.get("args", {}))
    usage = None
    if frame.usage:
        usage = TokenUsage(
            input_tokens=frame.usage.get("input_tokens", 0),
            output_tokens=frame.usage.get("output_tokens", 0),
        )
    return LlmDelta(
        text=frame.text,
        tool_call=tool_call,
        finish_reason=frame.finish_reason,
        usage=usage,
    )
