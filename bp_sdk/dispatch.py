"""bp_sdk.dispatch — Receive loop, send queue drain, heartbeat.

Runs the receive coroutine that classifies inbound frames:
  - NewTask → TaskContext build + handler invocation + Result emission
  - Result → resolve correlated peer-call Future
  - Cancel → trip cancel token on the matching task
  - Progress → forward to subscriber
  - Ack → resolve send-side Future
  - Ping → respond Pong
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ValidationError

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    ErrorFrame,
    Frame,
    LlmDeltaFrame,
    LlmResultFrame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
)
from bp_protocol.types import AgentOutput, TaskStatus
from bp_sdk.context import CancelToken, TaskContext
from bp_sdk.correlation import PendingMap
from bp_sdk.errors import CancellationError, HandlerError

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
    cancel_token: CancelToken
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
        # correlation_id → asyncio.Queue used by streaming LLM calls.
        # Keyed on the LlmRequest.correlation_id; LlmDelta frames are
        # pushed in arrival order and the terminal LlmResult is pushed
        # last, ending the SDK-side iterator.
        self._llm_streams: dict[str, asyncio.Queue] = {}

    # ------------------------------------------------------------------
    # Run / shutdown
    # ------------------------------------------------------------------

    async def run_until(self, stop_event: asyncio.Event) -> None:
        self.pending_acks.start_reaper()
        self.pending_results.start_reaper()

        recv_loop = asyncio.create_task(self._recv_loop())
        self._loops = [recv_loop]

        # Wait for either external stop or the recv loop dying.
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            [recv_loop, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        await self._drain_in_flight(grace_s=30.0)
        for t in self._loops:
            t.cancel()

    async def _drain_in_flight(self, *, grace_s: float) -> None:
        deadline = asyncio.get_running_loop().time() + grace_s
        while self._active:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
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
                logger.exception(
                    "recv_failed", extra={"event": "recv_failed"}
                )
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
            # Peer-call results are correlated by task_id (assigned during
            # the spawn ack). PendingMap accepts arbitrary keys.
            self.pending_results.resolve(frame.task_id, frame)
        elif isinstance(frame, ProgressFrame):
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
        elif isinstance(frame, LlmDeltaFrame):
            await self._handle_llm_delta(frame)
        elif isinstance(frame, LlmResultFrame):
            await self._handle_llm_result(frame)
        elif isinstance(frame, ErrorFrame):
            logger.warning(
                "router_error_frame",
                extra={
                    "event": "router_error_frame",
                    "code": frame.code,
                    "message": frame.message,
                },
            )
        else:
            logger.warning(
                "unexpected_frame",
                extra={"event": "unexpected_frame", "type": frame.type},
            )

    # ------------------------------------------------------------------
    # NewTask → handler invocation
    # ------------------------------------------------------------------

    async def _handle_new_task(self, frame: NewTaskFrame) -> None:
        # Acknowledge admission immediately; the handler runs in the
        # background and emits a Result frame on completion.
        ack = AckFrame(
            agent_id=self.agent.info.agent_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=True,
            task_id=frame.task_id,
        )

        # Find a handler. If we can't, reject before acking.
        handler = self._resolve_handler_for(frame)
        if handler is None:
            ack = AckFrame(
                agent_id=self.agent.info.agent_id,
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=frame.correlation_id,
                accepted=False,
                reason="no_handler",
            )
            await self.transport.send(ack)
            return

        # Validate input.
        try:
            payload = handler.input_model.model_validate(frame.payload)
        except ValidationError as exc:
            await self.transport.send(
                AckFrame(
                    agent_id=self.agent.info.agent_id,
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=frame.correlation_id,
                    accepted=False,
                    reason=f"validation_error: {exc.errors()[0]['msg']}",
                )
            )
            return

        await self.transport.send(ack)

        # Build TaskContext + run handler.
        cancel_token = CancelToken()
        ctx = self._build_context(frame, cancel_token)

        handler_task = asyncio.create_task(
            self._run_handler(handler, ctx, payload, frame)
        )
        # NewTaskFrame.task_id is None only on agent → router spawn frames
        # (router assigns the id and acks). Frames the router DELIVERS to
        # an agent always carry the assigned task_id; if we somehow see
        # one without, log and skip the active-tasks bookkeeping rather
        # than relying on `assert` (which `python -O` would strip).
        if frame.task_id is None:
            logger.warning(
                "newtask_without_task_id",
                extra={"event": "newtask_without_task_id"},
            )
            return
        self._active[frame.task_id] = _ActiveTask(
            task_id=frame.task_id,
            cancel_token=cancel_token,
            handler_task=handler_task,
        )

    def _resolve_handler_for(self, frame: NewTaskFrame):  # type: ignore[no-untyped-def]
        """Pick the right handler for the payload.

        Strategy: if exactly one handler is registered, use it. Otherwise,
        try to match by validating the payload against each registered
        input model — the first that passes wins. (A future enhancement
        would have NewTask carry an explicit input-model name.)
        """
        handlers = self.agent.registered_handlers
        if not handlers:
            return None
        if len(handlers) == 1:
            return next(iter(handlers.values()))
        for h in handlers.values():
            try:
                h.input_model.model_validate(frame.payload)
                return h
            except ValidationError:
                continue
        return None

    def _build_context(
        self, frame: NewTaskFrame, cancel_token: CancelToken
    ) -> TaskContext:
        from bp_sdk.files import ProxyFileManager  # noqa: PLC0415
        from bp_sdk.llm import LlmServiceClient  # noqa: PLC0415
        from bp_sdk.peers import PeerClient  # noqa: PLC0415
        from bp_sdk.progress import ProgressEmitter  # noqa: PLC0415

        bound_log = logger.getChild(self.agent.info.agent_id)
        # Construct service handles. None of them block on construction;
        # actual network/IO happens on first use.
        ctx = TaskContext(
            task_id=frame.task_id or "<spawn>",
            parent_task_id=frame.parent_task_id,
            user_id=frame.user_id,
            session_id=frame.session_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            deadline=frame.deadline,
            cancel_token=cancel_token,
            log=bound_log,
            progress=None,  # filled below
            files=None,     # filled below
            llm=None,       # filled below
            peers=None,     # filled below
        )
        ctx.progress = ProgressEmitter(ctx, self)
        ctx.peers = PeerClient(ctx, self)
        ctx.llm = LlmServiceClient(ctx, self)
        ctx.files = ProxyFileManager(
            ctx,
            inbox_dir=Path(self.agent.config.state_dir) / "inbox" / (frame.task_id or "spawn"),
            router_url=self._http_router_url(),
            embedded=self.agent.config.embedded,
            auth_token=self.agent.config.auth_token,
        )
        return ctx

    def _http_router_url(self) -> str:
        # Derive http(s) base from ws(s) router_url for ProxyFile fetch fallbacks.
        url = self.agent.config.router_url
        if url.startswith("wss://"):
            return "https://" + url[len("wss://") :].split("/v1/")[0]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://") :].split("/v1/")[0]
        return url

    async def _run_handler(
        self,
        handler,  # type: ignore[no-untyped-def]
        ctx: TaskContext,
        payload: BaseModel,
        frame: NewTaskFrame,
    ) -> None:
        status = TaskStatus.SUCCEEDED
        status_code = 200
        output: Optional[AgentOutput] = None
        error: Optional[dict[str, Any]] = None

        try:
            result = await handler.fn(ctx, payload)
            if isinstance(result, AgentOutput):
                output = result
            elif isinstance(result, BaseModel):
                # Coerce arbitrary BaseModel returns into AgentOutput.metadata
                output = AgentOutput(metadata=result.model_dump())
            elif result is None:
                output = AgentOutput()
            else:
                output = AgentOutput(content=str(result))
        except CancellationError as exc:
            status = TaskStatus.CANCELLED
            status_code = exc.status_code
            error = {"code": "cancelled", "message": str(exc)}
        except HandlerError as exc:
            status = TaskStatus.FAILED
            status_code = exc.status_code
            error = {"code": type(exc).__name__, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "handler_unhandled_exception",
                extra={
                    "event": "handler_unhandled_exception",
                    "bp.task_id": frame.task_id,
                },
            )
            status = TaskStatus.FAILED
            status_code = 500
            error = {"code": "InternalError", "message": str(exc)}
        finally:
            if frame.task_id is not None:
                self._active.pop(frame.task_id, None)

        result_frame = ResultFrame(
            agent_id=self.agent.info.agent_id,
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            task_id=frame.task_id or "",
            parent_task_id=frame.parent_task_id,
            status=status,
            status_code=status_code,
            output=output,
            error=error,
        )
        await self.transport.send(result_frame)

    async def _handle_progress(self, frame: ProgressFrame) -> None:
        # Forward to peer-call subscribers (registered by ctx.peers.spawn
        # with stream=True). Safe to ignore if no subscriber exists.
        # Subscribers register a queue keyed by task_id in pending_results.
        # Hook left as a stub for now.
        return

    async def _handle_cancel(self, frame: CancelFrame) -> None:
        active = self._active.get(frame.task_id)
        if active is not None:
            active.cancel_token.trip(frame.reason)

    # ------------------------------------------------------------------
    # LLM responses
    # ------------------------------------------------------------------

    async def _handle_llm_delta(self, frame: LlmDeltaFrame) -> None:
        from bp_sdk.llm import _frame_delta_to_delta  # noqa: PLC0415

        queue = self._llm_streams.get(frame.ref_correlation_id)
        if queue is None:
            return  # late delta after the iterator was abandoned
        await queue.put(_frame_delta_to_delta(frame))

    async def _handle_llm_result(self, frame: LlmResultFrame) -> None:
        # Streaming case: terminate the iterator queue.
        queue = self._llm_streams.get(frame.ref_correlation_id)
        if queue is not None:
            await queue.put(frame)
            return
        # Non-streaming case: resolve the pending future.
        self.pending_results.resolve(frame.ref_correlation_id, frame)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_dispatcher(agent: "Agent", transport: "Transport") -> Dispatcher:
    return Dispatcher(agent, transport)
