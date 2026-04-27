"""bp_sdk.context — TaskContext, the argument every handler receives."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from bp_sdk.errors import CancellationError

if TYPE_CHECKING:
    from bp_sdk.files import ProxyFileManager
    from bp_sdk.llm import LlmServiceClient
    from bp_sdk.peers import PeerClient
    from bp_sdk.progress import ProgressEmitter


# ---------------------------------------------------------------------------
# Cancel token
# ---------------------------------------------------------------------------


class CancelToken:
    """Cooperative cancellation signal scoped to one task.

    Tripped when the router sends a Cancel frame for the task or when
    the SDK enters graceful shutdown.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: Optional[str] = None

    def trip(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancellationError(self._reason or "cancelled")

    async def wait(self) -> None:
        await self._event.wait()


# ---------------------------------------------------------------------------
# TaskContext
# ---------------------------------------------------------------------------


@dataclass
class TaskContext:
    """Argument passed to every handler. Lifetime: one task invocation.

    The fields below are stable; new fields are additive across SDK
    versions. Handlers must not mutate identity fields.
    """

    task_id: str
    parent_task_id: Optional[str]
    user_id: str
    session_id: str
    trace_id: str
    span_id: str

    deadline: Optional[datetime]

    cancel_token: CancelToken
    log: logging.Logger
    """Pre-bound with trace_id/task_id/agent_id."""
    progress: "ProgressEmitter"
    files: "ProxyFileManager"
    llm: "LlmServiceClient"
    peers: "PeerClient"

    extras: dict[str, Any] = field(default_factory=dict)
    """Free-form per-context state — SDK plugins may stash data here."""

    # ------------------------------------------------------------------

    def child_span(self, name: str) -> "_ChildSpan":
        """Convenience: open an OTel child span keyed off this context.

        Returns a context-manager. SDK plugins that don't want to depend
        on OTel directly can use this without importing it.
        """
        return _ChildSpan(self, name)

    def metric(self, name: str, value: float, **labels: str) -> None:
        """Increment / observe a Prometheus-style metric.

        SDK side records into a per-process registry; the router-side
        equivalent is `bp_router.observability.metrics`.
        """
        from bp_sdk._metrics import record  # noqa: PLC0415

        record(name, value, labels)


# ---------------------------------------------------------------------------
# Tiny helpers (kept here to avoid circular imports)
# ---------------------------------------------------------------------------


class _ChildSpan:
    def __init__(self, ctx: "TaskContext", name: str) -> None:
        self._ctx = ctx
        self._name = name
        self._span: Any = None

    def __enter__(self) -> "_ChildSpan":
        try:
            from opentelemetry import trace  # noqa: PLC0415

            tracer = trace.get_tracer("bp_sdk")
            self._span = tracer.start_span(self._name)
            self._span.set_attribute("bp.task_id", self._ctx.task_id)
            self._span.set_attribute("bp.user_id", self._ctx.user_id)
        except Exception:  # noqa: BLE001
            self._span = None
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._span is not None:
            try:
                if exc is not None:
                    self._span.record_exception(exc)
                self._span.end()
            except Exception:  # noqa: BLE001
                pass
