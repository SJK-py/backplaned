"""bp_router.api.llm — HTTP endpoint that exposes LlmService to agents.

This is the bridge between the agent-side `LlmServiceClient` (in bp_sdk)
and the router-side `LlmService`. The frame-channel transport for LLM
calls is a future enhancement; for now agents POST here over HTTPS with
their bearer token.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bp_router.llm.service import (
    LlmDelta,
    LlmResponse,
    Message,
    ToolSpec,
)
from bp_router.security.jwt import TokenError, verify_agent_token

logger = logging.getLogger(__name__)
router = APIRouter()


class LlmGenerateRequest(BaseModel):
    messages: list[dict[str, Any]]
    model: str = "default"
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    provider_options: Optional[dict[str, Any]] = None
    user_id: Optional[str] = None
    task_id: Optional[str] = None


class LlmGenerateResponse(BaseModel):
    text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class LlmEmbedRequest(BaseModel):
    text: str | list[str]
    model: str = "default"


class LlmEmbedResponse(BaseModel):
    vectors: list[list[float]]


class LlmCountRequest(BaseModel):
    messages: list[dict[str, Any]]
    model: str = "default"


class LlmCountResponse(BaseModel):
    total_tokens: int


# ---------------------------------------------------------------------------
# Auth dependency — agents authenticate with their JWT
# ---------------------------------------------------------------------------


async def _require_agent(request: Request, authorization: str) -> str:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("bearer "):].strip()
    settings = request.app.state.bp.settings
    revoked: set[str] = set()
    if request.app.state.bp.redis is not None:
        members = await request.app.state.bp.redis.smembers("router:revoked_jti")
        revoked = set(members) if members else set()
    try:
        principal = verify_agent_token(
            token,
            secret=settings.jwt_secret.get_secret_value(),
            revoked_jti=revoked,
            key_version=settings.jwt_key_version,
            algorithm=settings.jwt_algorithm,
        )
    except TokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    return principal.agent_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _to_messages(raw: list[dict[str, Any]]) -> list[Message]:
    out: list[Message] = []
    for m in raw:
        out.append(
            Message(
                role=m["role"],
                content=m["content"],
                name=m.get("name"),
                tool_call_id=m.get("tool_call_id"),
            )
        )
    return out


def _to_toolspecs(raw: Optional[list[dict[str, Any]]]) -> Optional[list[ToolSpec]]:
    if not raw:
        return None
    return [
        ToolSpec(
            name=t["name"],
            description=t.get("description", ""),
            parameters=t.get("parameters") or t.get("input_schema") or {},
        )
        for t in raw
    ]


@router.post("/generate")
async def generate(
    req: LlmGenerateRequest,
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
):  # type: ignore[no-untyped-def]
    agent_id = await _require_agent(request, authorization)
    state = request.app.state.bp

    if req.stream:
        async def _sse() -> AsyncIterator[bytes]:
            iterator = await state.llm_service.generate(
                _to_messages(req.messages),
                model=req.model,
                tools=_to_toolspecs(req.tools),
                tool_choice=req.tool_choice,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                stream=True,
                provider_options=req.provider_options,
                user_id=req.user_id,
                task_id=req.task_id,
            )
            try:
                async for delta in iterator:  # type: ignore[union-attr]
                    body = {
                        "text": delta.text,
                        "tool_call": (
                            {
                                "id": delta.tool_call.id,
                                "name": delta.tool_call.name,
                                "args": delta.tool_call.args,
                            }
                            if delta.tool_call
                            else None
                        ),
                        "finish_reason": delta.finish_reason,
                        "usage": (
                            {
                                "input_tokens": delta.usage.input_tokens,
                                "output_tokens": delta.usage.output_tokens,
                            }
                            if delta.usage
                            else None
                        ),
                    }
                    yield (
                        b"data: " + json.dumps(body).encode("utf-8") + b"\n\n"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "llm_stream_failed",
                    extra={"event": "llm_stream_failed", "agent_id": agent_id},
                )
                yield (
                    b"data: "
                    + json.dumps({"error": str(exc)}).encode("utf-8")
                    + b"\n\n"
                )
            yield b"data: {\"done\": true}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    resp: LlmResponse = await state.llm_service.generate(
        _to_messages(req.messages),
        model=req.model,
        tools=_to_toolspecs(req.tools),
        tool_choice=req.tool_choice,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        stream=False,
        provider_options=req.provider_options,
        user_id=req.user_id,
        task_id=req.task_id,
    )  # type: ignore[assignment]
    return LlmGenerateResponse(
        text=resp.text,
        tool_calls=[
            {"id": tc.id, "name": tc.name, "args": tc.args} for tc in resp.tool_calls
        ],
        finish_reason=resp.finish_reason,
        usage={
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        },
        raw={},
    )


@router.post("/embed", response_model=LlmEmbedResponse)
async def embed(
    req: LlmEmbedRequest,
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
) -> LlmEmbedResponse:
    await _require_agent(request, authorization)
    vectors = await request.app.state.bp.llm_service.embed(req.text, model=req.model)
    return LlmEmbedResponse(vectors=vectors)


@router.post("/count-tokens", response_model=LlmCountResponse)
async def count_tokens(
    req: LlmCountRequest,
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
) -> LlmCountResponse:
    await _require_agent(request, authorization)
    total = await request.app.state.bp.llm_service.count_tokens(
        _to_messages(req.messages), model=req.model
    )
    return LlmCountResponse(total_tokens=total)
