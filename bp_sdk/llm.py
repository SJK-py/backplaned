"""bp_sdk.llm — Agent-side LLM service client.

Calls the router-side LlmService via a dedicated frame channel rather
than spawning subtasks. See `docs/design/sdk/services.md` §1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, Optional, Union

from pydantic import BaseModel

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext
    from bp_sdk.dispatch import Dispatcher


# Re-export types for symmetry with bp_router.llm.service so agent
# code and router code can share Message / ToolSpec / LlmResponse.
@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]]]
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LlmServiceClient:
    """Per-task LLM facade.

    Bound to a `TaskContext`; user_id/task_id are propagated in every
    call so the router-side LlmService can enforce quotas and record
    audit / cost metrics under the right keys.

    Streaming responses are auto-forwarded as Progress(chunk) frames
    by the dispatcher — agents can choose to also iterate the deltas
    in handler code.
    """

    def __init__(self, ctx: "TaskContext", dispatcher: "Dispatcher") -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher

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
        """Provider-neutral LLM call.

        For Gemini-native features (search grounding, code execution,
        image generation, thinking budgets), pass `provider_options`.
        See `docs/design/sdk/services.md` §1.3.
        """
        raise NotImplementedError

    async def embed(
        self,
        text: Union[str, list[str]],
        *,
        model: str = "default",
    ) -> list[list[float]]:
        raise NotImplementedError

    async def count_tokens(
        self,
        prompt: Union[str, list[Message]],
        *,
        model: str = "default",
    ) -> int:
        raise NotImplementedError
