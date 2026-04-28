"""bp_router.dispatch — Frame-type → action dispatch.

The receive loop in `ws_hub` decodes one frame at a time and calls
`dispatch_frame`. Side effects are pushed to the right subsystem
(tasks, correlation, observability).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from bp_protocol.frames import (
    AckFrame,
    CancelFrame,
    ErrorCode,
    ErrorFrame,
    Frame,
    LlmDeltaFrame,
    LlmRequestFrame,
    LlmResultFrame,
    NewTaskFrame,
    PingFrame,
    PongFrame,
    ProgressFrame,
    ResultFrame,
)
from bp_router.delivery import AgentNotConnected, deliver_frame, fanout_frame

if TYPE_CHECKING:
    from bp_router.app import AppState
    from bp_router.ws_hub import SocketEntry

logger = logging.getLogger(__name__)


async def dispatch_frame(
    state: "AppState",
    entry: "SocketEntry",
    frame: Frame,
) -> None:
    """Route an inbound frame to the right handler."""
    try:
        from bp_router.observability.metrics import frames_total  # noqa: PLC0415

        frames_total.labels(direction="recv", type=frame.type, agent_id=entry.agent_id).inc()
    except Exception:  # noqa: BLE001
        pass

    if isinstance(frame, NewTaskFrame):
        await _handle_new_task(state, entry, frame)
    elif isinstance(frame, ResultFrame):
        await _handle_result(state, entry, frame)
    elif isinstance(frame, ProgressFrame):
        await _handle_progress(state, entry, frame)
    elif isinstance(frame, CancelFrame):
        await _handle_cancel(state, entry, frame)
    elif isinstance(frame, AckFrame):
        await _handle_ack(state, entry, frame)
    elif isinstance(frame, PingFrame):
        await _handle_ping(state, entry, frame)
    elif isinstance(frame, PongFrame):
        await _handle_pong(state, entry, frame)
    elif isinstance(frame, LlmRequestFrame):
        await _handle_llm_request(state, entry, frame)
    else:
        logger.warning(
            "unexpected_frame_in_dispatch",
            extra={"event": "unexpected_frame_in_dispatch", "type": frame.type},
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_new_task(
    state: "AppState", entry: "SocketEntry", frame: NewTaskFrame
) -> None:
    """Validate, ACL-check, admit, dispatch."""
    from bp_router.tasks import AdmitError, admit_task  # noqa: PLC0415

    try:
        task_id = await admit_task(state, frame, caller_agent_id=entry.agent_id)
    except AdmitError as exc:
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason=exc.message,
        )
        await entry.outbox.put(ack)
        return
    except Exception:  # noqa: BLE001
        logger.exception(
            "admit_task_failed",
            extra={"event": "admit_task_failed"},
        )
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason="internal_error",
        )
        await entry.outbox.put(ack)
        return

    # Acknowledge the spawn with the assigned task_id.
    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=True,
        task_id=task_id,
    )
    await entry.outbox.put(ack)


async def _handle_result(
    state: "AppState", entry: "SocketEntry", frame: ResultFrame
) -> None:
    """Persist + propagate to parent. Send Ack to the reporting agent."""
    from bp_router.tasks import complete_task  # noqa: PLC0415

    try:
        await complete_task(state, frame, reporting_agent_id=entry.agent_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "complete_task_failed",
            extra={"event": "complete_task_failed", "bp.task_id": frame.task_id},
        )
        ack = AckFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=frame.correlation_id,
            accepted=False,
            reason="internal_error",
        )
        await entry.outbox.put(ack)
        return

    ack = AckFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
        accepted=True,
    )
    await entry.outbox.put(ack)


async def _handle_progress(
    state: "AppState", entry: "SocketEntry", frame: ProgressFrame
) -> None:
    """Fan-out to the parent agent's socket. No persistence; best-effort."""
    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT t.parent_task_id, parent.agent_id AS parent_agent_id
            FROM tasks t
            LEFT JOIN tasks parent ON parent.task_id = t.parent_task_id
            WHERE t.task_id = $1
            """,
            frame.task_id,
        )
    if row is None or row["parent_agent_id"] is None:
        return
    fanout_frame(state, [row["parent_agent_id"]], frame)


async def _handle_cancel(
    state: "AppState", entry: "SocketEntry", frame: CancelFrame
) -> None:
    """Cancel an in-flight LLM call or recursively cancel a task."""
    # LLM-call abort: cancel just the matching router-side asyncio.Task.
    if frame.ref_correlation_id is not None:
        task = entry.llm_tasks.pop(frame.ref_correlation_id, None)
        if task is not None and not task.done():
            task.cancel()
        return

    if frame.task_id is None:
        return  # malformed; nothing to do

    from bp_router.tasks import cancel_task  # noqa: PLC0415

    pool = state.db_pool  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM tasks WHERE task_id = $1",
            frame.task_id,
        )
    if row is None:
        return

    await cancel_task(
        state,
        frame.task_id,
        user_id=row["user_id"],
        reason=frame.reason,
        initiator=entry.agent_id,
    )


async def _handle_ack(
    state: "AppState", entry: "SocketEntry", frame: AckFrame
) -> None:
    """Resolve any pending ack for `frame.ref_correlation_id`."""
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]


async def _handle_ping(
    state: "AppState", entry: "SocketEntry", frame: PingFrame
) -> None:
    pong = PongFrame(
        agent_id="router",
        trace_id=frame.trace_id,
        span_id=frame.span_id,
        ref_correlation_id=frame.correlation_id,
    )
    await entry.outbox.put(pong)


async def _handle_pong(
    state: "AppState", entry: "SocketEntry", frame: PongFrame
) -> None:
    state.correlation.resolve(frame.ref_correlation_id, frame)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LLM request handler
# ---------------------------------------------------------------------------


async def _handle_llm_request(
    state: "AppState", entry: "SocketEntry", frame: LlmRequestFrame
) -> None:
    """Run an LLM call against `state.llm_service` and stream/return the result.

    The router-side asyncio.Task is tracked on the SocketEntry so the
    disconnect handler can cancel in-flight LLM work and stop wasting
    provider tokens on a dead client.
    """
    import asyncio  # noqa: PLC0415

    task = asyncio.create_task(_run_llm_call(state, entry, frame))
    entry.llm_tasks[frame.correlation_id] = task

    def _cleanup(_t: "asyncio.Task") -> None:
        entry.llm_tasks.pop(frame.correlation_id, None)

    task.add_done_callback(_cleanup)


async def _run_llm_call(
    state: "AppState", entry: "SocketEntry", frame: LlmRequestFrame
) -> None:
    from bp_router.llm.service import Message, ToolSpec  # noqa: PLC0415

    correlation = frame.correlation_id

    def _send(out_frame: Frame) -> "asyncio.Future":  # type: ignore[no-untyped-def]
        return entry.outbox.put(out_frame)

    def _err_result(message: str, *, code: str = "internal_error") -> LlmResultFrame:
        return LlmResultFrame(
            agent_id="router",
            trace_id=frame.trace_id,
            span_id=frame.span_id,
            ref_correlation_id=correlation,
            error={"code": code, "message": message},
        )

    try:
        if frame.kind == "embed":
            text = frame.text or []
            vectors = await state.llm_service.embed(  # type: ignore[attr-defined]
                text, model=frame.model, user_id=frame.user_id
            )
            await _send(
                LlmResultFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    vectors=vectors,
                )
            )
            return

        if frame.kind == "count_tokens":
            messages = [
                Message(
                    role=m["role"],
                    content=m["content"],
                    name=m.get("name"),
                    tool_call_id=m.get("tool_call_id"),
                )
                for m in frame.messages
            ]
            total = await state.llm_service.count_tokens(  # type: ignore[attr-defined]
                messages, model=frame.model
            )
            await _send(
                LlmResultFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    total_tokens=total,
                )
            )
            return

        # Default: generate
        messages = [
            Message(
                role=m["role"],
                content=m["content"],
                name=m.get("name"),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in frame.messages
        ]
        tools = (
            [
                ToolSpec(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=t.get("parameters") or t.get("input_schema") or {},
                )
                for t in frame.tools
            ]
            if frame.tools
            else None
        )

        if not frame.stream:
            resp = await state.llm_service.generate(  # type: ignore[attr-defined]
                messages,
                model=frame.model,
                tools=tools,
                tool_choice=frame.tool_choice,
                temperature=frame.temperature,
                max_tokens=frame.max_tokens,
                stream=False,
                provider_options=frame.provider_options,
                user_id=frame.user_id,
                task_id=frame.task_id,
            )
            await _send(
                LlmResultFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    text=resp.text,
                    tool_calls=[
                        {"id": tc.id, "name": tc.name, "args": tc.args}
                        for tc in resp.tool_calls
                    ],
                    finish_reason=resp.finish_reason,
                    usage={
                        "input_tokens": resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                    },
                )
            )
            return

        # Streaming
        iterator = await state.llm_service.generate(  # type: ignore[attr-defined]
            messages,
            model=frame.model,
            tools=tools,
            tool_choice=frame.tool_choice,
            temperature=frame.temperature,
            max_tokens=frame.max_tokens,
            stream=True,
            provider_options=frame.provider_options,
            user_id=frame.user_id,
            task_id=frame.task_id,
        )

        final_finish = "stop"
        agg_in = agg_out = 0
        async for delta in iterator:  # type: ignore[union-attr]
            await _send(
                LlmDeltaFrame(
                    agent_id="router",
                    trace_id=frame.trace_id,
                    span_id=frame.span_id,
                    ref_correlation_id=correlation,
                    text=delta.text,
                    tool_call=(
                        {
                            "id": delta.tool_call.id,
                            "name": delta.tool_call.name,
                            "args": delta.tool_call.args,
                        }
                        if delta.tool_call
                        else None
                    ),
                    finish_reason=delta.finish_reason,
                    usage=(
                        {
                            "input_tokens": delta.usage.input_tokens,
                            "output_tokens": delta.usage.output_tokens,
                        }
                        if delta.usage
                        else None
                    ),
                )
            )
            if delta.finish_reason:
                final_finish = delta.finish_reason
            if delta.usage:
                agg_in = max(agg_in, delta.usage.input_tokens)
                agg_out = max(agg_out, delta.usage.output_tokens)

        await _send(
            LlmResultFrame(
                agent_id="router",
                trace_id=frame.trace_id,
                span_id=frame.span_id,
                ref_correlation_id=correlation,
                finish_reason=final_finish,
                usage={"input_tokens": agg_in, "output_tokens": agg_out},
            )
        )
    except asyncio.CancelledError:
        # Disconnect or supersede; don't send a result frame, the socket is gone.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "llm_call_failed",
            extra={
                "event": "llm_call_failed",
                "bp.agent_id": entry.agent_id,
            },
        )
        try:
            await entry.outbox.put(_err_result(str(exc)))
        except Exception:  # noqa: BLE001
            pass
