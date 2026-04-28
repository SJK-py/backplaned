"""bp_protocol.frames — Discriminated-union frame models for the WebSocket
protocol between router and agents.

See `docs/router/protocol.md` §2 for the full specification.

Every frame is a Pydantic model; validation happens at the router edge
before any business logic runs. Callers should use `parse_frame()` to
decode JSON into the correct typed instance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter

from bp_protocol.types import (
    AgentInfo,
    AgentOutput,
    ProxyFile,
    TaskPriority,
    TaskStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_correlation_id() -> str:
    """UUIDv4 used for correlation. UUIDv7 once stdlib supports it cleanly."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Common header
# ---------------------------------------------------------------------------


class _FrameBase(BaseModel):
    """Fields present on every frame (`docs/router/protocol.md` §2.1)."""

    type: str
    protocol_version: str = "1"
    correlation_id: str = Field(default_factory=_new_correlation_id)
    trace_id: str
    span_id: str
    timestamp: datetime = Field(default_factory=_now)
    agent_id: str

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class HelloFrame(_FrameBase):
    """First frame on a new socket. Agent → router."""

    type: Literal["Hello"] = "Hello"
    auth_token: str
    sdk_version: str
    agent_info: AgentInfo
    resume_token: Optional[str] = None


class WelcomeFrame(_FrameBase):
    """Router → agent, sent only after successful Hello."""

    type: Literal["Welcome"] = "Welcome"
    session_id: str
    available_destinations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    heartbeat_interval_ms: int = 20_000
    max_payload_bytes: int = 1_048_576


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


class NewTaskFrame(_FrameBase):
    """Spawn (`task_id is None`) or delegate (`task_id` set)."""

    type: Literal["NewTask"] = "NewTask"
    task_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    destination_agent_id: str
    user_id: str
    session_id: str
    priority: TaskPriority = TaskPriority.NORMAL
    deadline: Optional[datetime] = None
    idempotency_key: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    acl_grants: list[dict[str, Any]] = Field(default_factory=list)


class ResultFrame(_FrameBase):
    """Terminal outcome of a task. Exactly one per task, ever."""

    type: Literal["Result"] = "Result"
    task_id: str
    parent_task_id: Optional[str] = None
    status: TaskStatus
    status_code: int
    output: Optional[AgentOutput] = None
    error: Optional[dict[str, Any]] = None


class ProgressFrame(_FrameBase):
    """Interim event during long-running tasks."""

    type: Literal["Progress"] = "Progress"
    task_id: str
    event: str
    """thinking | tool_call | tool_result | chunk | status | <custom>"""
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelFrame(_FrameBase):
    """Request abort of an in-flight task or LLM call.

    Two modes:
      - task abort:  task_id set, ref_correlation_id None (the common
        path; cancels the task and propagates to descendants).
      - LLM abort:   ref_correlation_id set to an in-flight LlmRequest's
        correlation_id, task_id None. Cancels just that one provider
        call so an agent's own cancellation can free server-side
        tokens without tearing down the surrounding task.
    """

    type: Literal["Cancel"] = "Cancel"
    task_id: Optional[str] = None
    ref_correlation_id: Optional[str] = None
    reason: str = "user_aborted"


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


class ErrorFrame(_FrameBase):
    """Protocol-level failure. Not used for task-level failures (use Result)."""

    type: Literal["Error"] = "Error"
    code: str
    message: str
    ref_correlation_id: Optional[str] = None
    retryable: bool = False


class AckFrame(_FrameBase):
    """Receipt acknowledgement."""

    type: Literal["Ack"] = "Ack"
    ref_correlation_id: str
    accepted: bool = True
    reason: Optional[str] = None
    task_id: Optional[str] = None
    """Set on ack of NewTask: the assigned task_id."""


class PingFrame(_FrameBase):
    type: Literal["Ping"] = "Ping"


class PongFrame(_FrameBase):
    type: Literal["Pong"] = "Pong"
    ref_correlation_id: str


# ---------------------------------------------------------------------------
# LLM service (router-side LlmService over the same WS channel)
# ---------------------------------------------------------------------------


LlmCallKind = Literal["generate", "embed", "count_tokens"]


class LlmRequestFrame(_FrameBase):
    """Agent → router. Invoke the router-side `LlmService`.

    `kind` selects between generate / embed / count_tokens. Field set
    used by each:
      generate:     messages, tools, tool_choice, temperature,
                    max_tokens, stream, provider_options
      embed:        text
      count_tokens: messages
    """

    type: Literal["LlmRequest"] = "LlmRequest"
    kind: LlmCallKind = "generate"
    model: str = "default"

    # generate
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    provider_options: Optional[dict[str, Any]] = None

    # embed
    text: Optional[list[str]] = None

    # context (propagated for quotas + audit)
    user_id: Optional[str] = None
    task_id: Optional[str] = None


class LlmDeltaFrame(_FrameBase):
    """Router → agent. One streaming chunk in a generate(stream=True) call.

    The terminal `LlmResultFrame` always follows the last delta and ends
    the iterator on the SDK side.
    """

    type: Literal["LlmDelta"] = "LlmDelta"
    ref_correlation_id: str
    text: Optional[str] = None
    tool_call: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None
    usage: Optional[dict[str, int]] = None


class LlmResultFrame(_FrameBase):
    """Router → agent. Terminal response for a `LlmRequestFrame`.

    Field set populated depends on `kind`:
      generate:     text, tool_calls, finish_reason, usage, raw
      embed:        vectors
      count_tokens: total_tokens

    `error` is set when the call failed; SDK raises on receipt.
    """

    type: Literal["LlmResult"] = "LlmResult"
    ref_correlation_id: str

    # generate
    text: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    # embed
    vectors: list[list[float]] = Field(default_factory=list)

    # count_tokens
    total_tokens: int = 0

    error: Optional[dict[str, str]] = None


# ---------------------------------------------------------------------------
# Discriminated union + parser
# ---------------------------------------------------------------------------


Frame = Annotated[
    Union[
        HelloFrame,
        WelcomeFrame,
        NewTaskFrame,
        ResultFrame,
        ProgressFrame,
        CancelFrame,
        ErrorFrame,
        AckFrame,
        PingFrame,
        PongFrame,
        LlmRequestFrame,
        LlmDeltaFrame,
        LlmResultFrame,
    ],
    Field(discriminator="type"),
]


_FRAME_ADAPTER: TypeAdapter[Frame] = TypeAdapter(Frame)


def parse_frame(data: dict[str, Any] | str | bytes) -> Frame:
    """Validate and parse a JSON object/string/bytes into a typed Frame.

    Raises `pydantic.ValidationError` on invalid input. The router edge
    should catch and respond with `Error{code:"frame_invalid"}` rather
    than letting the exception propagate.
    """
    if isinstance(data, (str, bytes)):
        return _FRAME_ADAPTER.validate_json(data)
    return _FRAME_ADAPTER.validate_python(data)


def serialize_frame(frame: Frame) -> str:
    """Serialise a frame to a JSON string ready for `ws.send_text()`."""
    return frame.model_dump_json()


# ---------------------------------------------------------------------------
# Error code catalog (`docs/router/protocol.md` §6)
# ---------------------------------------------------------------------------


class ErrorCode:
    """Canonical error codes used in `ErrorFrame.code`."""

    PROTOCOL_VERSION = "protocol_version"
    FRAME_INVALID = "frame_invalid"
    AUTH_FAILED = "auth_failed"
    AUTH_EXPIRED = "auth_expired"
    AGENT_SUSPENDED = "agent_suspended"
    SCHEMA_MISMATCH = "schema_mismatch"
    ACL_DENIED = "acl_denied"
    ACL_GRANT_INVALID = "acl_grant_invalid"
    QUOTA_EXCEEDED = "quota_exceeded"
    BACKPRESSURE_TIMEOUT = "backpressure_timeout"
    ACK_TIMEOUT = "ack_timeout"
    AGENT_DISCONNECTED = "agent_disconnected"
    AGENT_NOT_FOUND = "agent_not_found"
    INTERNAL_ERROR = "internal_error"
