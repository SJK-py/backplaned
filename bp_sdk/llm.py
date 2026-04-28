"""bp_sdk.llm — Agent-side LLM service client.

Routes calls to the router-side LlmService over HTTPS using the agent's
bearer token. Streaming uses Server-Sent Events. Future work will
migrate this to the WebSocket frame channel; the API surface stays
the same.

See `docs/sdk/services.md` §1.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, Optional, Union

import httpx

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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LlmServiceClient:
    """Per-task LLM facade. Authenticates with the agent's bearer token.

    Lifetime is the task; constructed by the dispatcher. Auto-forwarding
    of streaming deltas as Progress(chunk) frames is the dispatcher's
    job — `generate(stream=True)` simply iterates without re-emitting.
    """

    def __init__(self, ctx: "TaskContext", dispatcher: "Dispatcher") -> None:
        self._ctx = ctx
        self._dispatcher = dispatcher
        self._http: Optional[httpx.AsyncClient] = None

    @property
    def _base_url(self) -> str:
        return self._dispatcher._http_router_url()  # type: ignore[attr-defined]

    @property
    def _auth(self) -> dict[str, str]:
        token = self._dispatcher.agent.config.auth_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=5.0),
                headers=self._auth,
            )
        return self._http

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
        if isinstance(prompt, str):
            messages = [Message(role="user", content=prompt)]
        else:
            messages = prompt

        body: dict[str, Any] = {
            "messages": [m.model_dump() for m in messages],
            "model": model,
            "stream": stream,
            "user_id": self._ctx.user_id,
            "task_id": self._ctx.task_id,
        }
        if tools:
            body["tools"] = [t.model_dump() for t in tools]
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if provider_options is not None:
            body["provider_options"] = provider_options

        if stream:
            return self._stream(body)

        client = self._client()
        resp = await client.post("/v1/llm/generate", json=body)
        resp.raise_for_status()
        return _parse_response(resp.json())

    async def _stream(self, body: dict[str, Any]) -> AsyncIterator[LlmDelta]:
        client = self._client()
        async with client.stream("POST", "/v1/llm/generate", json=body) as resp:
            resp.raise_for_status()
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                payload = raw[len("data: ") :].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("done"):
                    return
                if "error" in obj:
                    raise RuntimeError(f"LLM stream error: {obj['error']}")
                yield _parse_delta(obj)

    # ------------------------------------------------------------------
    # embed / count_tokens
    # ------------------------------------------------------------------

    async def embed(
        self,
        text: Union[str, list[str]],
        *,
        model: str = "default",
    ) -> list[list[float]]:
        client = self._client()
        resp = await client.post("/v1/llm/embed", json={"text": text, "model": model})
        resp.raise_for_status()
        return resp.json()["vectors"]

    async def count_tokens(
        self,
        prompt: Union[str, list[Message]],
        *,
        model: str = "default",
    ) -> int:
        if isinstance(prompt, str):
            messages = [Message(role="user", content=prompt).model_dump()]
        else:
            messages = [m.model_dump() for m in prompt]
        client = self._client()
        resp = await client.post(
            "/v1/llm/count-tokens", json={"messages": messages, "model": model}
        )
        resp.raise_for_status()
        return int(resp.json()["total_tokens"])

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def _parse_response(obj: dict[str, Any]) -> LlmResponse:
    return LlmResponse(
        text=obj.get("text", ""),
        tool_calls=[
            ToolCall(id=tc["id"], name=tc["name"], args=tc.get("args", {}))
            for tc in obj.get("tool_calls", [])
        ],
        finish_reason=obj.get("finish_reason", "stop"),
        usage=TokenUsage(
            input_tokens=obj.get("usage", {}).get("input_tokens", 0),
            output_tokens=obj.get("usage", {}).get("output_tokens", 0),
        ),
        raw=obj.get("raw", {}),
    )


def _parse_delta(obj: dict[str, Any]) -> LlmDelta:
    tool_call = None
    if obj.get("tool_call"):
        tc = obj["tool_call"]
        tool_call = ToolCall(id=tc["id"], name=tc["name"], args=tc.get("args", {}))

    usage = None
    if obj.get("usage"):
        u = obj["usage"]
        usage = TokenUsage(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
        )

    return LlmDelta(
        text=obj.get("text"),
        tool_call=tool_call,
        finish_reason=obj.get("finish_reason"),
        usage=usage,
    )
