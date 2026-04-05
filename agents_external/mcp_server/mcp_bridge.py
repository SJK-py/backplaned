"""
mcp_bridge.py — MCP Server that exposes router agents as MCP tools.

Uses the low-level ``mcp.server.lowlevel.Server`` API for fully dynamic
tool registration.  Agents discovered by polling ``GET /admin/agents``
are kept in an internal registry and served as tools to any connected
MCP client.

Provides SSE and streamable-HTTP transport builders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable, Awaitable, Optional

import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

logger = logging.getLogger("mcp_bridge")

# ---------------------------------------------------------------------------
# Schema parser — imported from helper.py
# ---------------------------------------------------------------------------

from helper import (  # noqa: E402
    _PRIMITIVE_TYPE_MAP,
    _COMPLEX_MODEL_SCHEMAS,
    _parse_param_type,
    _parse_input_schema_string,
)


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------


class _AgentEntry:
    """Represents a router agent exposed as an MCP tool."""

    __slots__ = ("agent_id", "tool_name", "description", "input_schema", "required_input")

    def __init__(self, agent_id: str, info: dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.tool_name = f"call_{agent_id}"
        self.description: str = info.get("description", agent_id)
        schema_str: str = info.get("input_schema", "")
        self.required_input: list[str] = info.get("required_input", [])

        properties, parsed_required = _parse_input_schema_string(schema_str)
        self.input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        # Prefer explicit required_input from agent_info; fall back to parsed.
        final_required = self.required_input or parsed_required
        if final_required:
            self.input_schema["required"] = final_required

    def to_tool(self) -> types.Tool:
        return types.Tool(
            name=self.tool_name,
            description=self.description,
            inputSchema=self.input_schema,
        )


# ---------------------------------------------------------------------------
# MCPBridge
# ---------------------------------------------------------------------------


class MCPBridge:
    """
    Bridges router agents into the MCP protocol.

    * Maintains a registry of agents as MCP tools.
    * Provides ``list_tools`` / ``call_tool`` handlers for the MCP server.
    * Tracks pending tool calls via ``asyncio.Event`` for synchronous response.
    """

    def __init__(
        self,
        *,
        router_url: str,
        spawn_task: Callable[[str, dict[str, Any]], Awaitable[str]],
        tool_timeout: float = 60.0,
        exclude_agents: set[str] | None = None,
    ) -> None:
        self._router_url = router_url
        self._spawn_task = spawn_task  # async (dest_agent_id, payload) -> task_id
        self._tool_timeout = tool_timeout
        self._exclude = exclude_agents or set()

        # Agent registry.
        self._agents: dict[str, _AgentEntry] = {}  # tool_name -> entry
        self._agent_id_to_tool: dict[str, str] = {}  # agent_id -> tool_name

        # Pending tool calls: task_id -> (Event, result_holder).
        self._pending: dict[str, asyncio.Event] = {}
        self._results: dict[str, dict[str, Any]] = {}

        # MCP server instance.
        self._server = Server(name="agent-router-mcp")
        self._register_handlers()

    # ------------------------------------------------------------------
    # MCP handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        @self._server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [entry.to_tool() for entry in self._agents.values()]

        @self._server.call_tool(validate_input=False)
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
            entry = self._agents.get(name)
            if entry is None:
                return [types.TextContent(type="text", text=f"Error: unknown tool '{name}'")]

            try:
                task_id = await self._spawn_task(entry.agent_id, arguments)
            except Exception as exc:
                return [types.TextContent(
                    type="text",
                    text=f"Error spawning task to '{entry.agent_id}': {exc}",
                )]

            # Wait for result.
            event = asyncio.Event()
            self._pending[task_id] = event

            try:
                await asyncio.wait_for(event.wait(), timeout=self._tool_timeout)
            except asyncio.TimeoutError:
                self._pending.pop(task_id, None)
                self._results.pop(task_id, None)
                return [types.TextContent(
                    type="text",
                    text=f"Error: tool call to '{entry.agent_id}' timed out after {self._tool_timeout}s",
                )]

            result = self._results.pop(task_id, {})
            self._pending.pop(task_id, None)

            payload = result.get("payload", {})
            content = payload.get("content", "")
            status_code = result.get("status_code", 200)

            if status_code and status_code >= 400:
                return [types.TextContent(
                    type="text",
                    text=f"Error (status {status_code}): {content}",
                )]

            return [types.TextContent(type="text", text=content or "(no output)")]

    # ------------------------------------------------------------------
    # Result delivery (called from /receive endpoint)
    # ------------------------------------------------------------------

    def deliver_result(self, task_id: str, data: dict[str, Any]) -> bool:
        """
        Deliver a router result to a pending tool call.
        Returns True if the result was matched to a pending call.
        """
        event = self._pending.get(task_id)
        if event is None:
            return False
        self._results[task_id] = data
        event.set()
        return True

    # ------------------------------------------------------------------
    # Agent registry updates
    # ------------------------------------------------------------------

    def update_agents(self, agents_list: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """
        Update the internal agent registry from a router ``GET /admin/agents``
        response.  Returns (added, removed) agent_id lists.
        """
        new_agents: dict[str, _AgentEntry] = {}
        new_id_map: dict[str, str] = {}

        for agent in agents_list:
            agent_id = agent.get("agent_id", "")
            if not agent_id or agent_id in self._exclude:
                continue
            info = agent.get("agent_info", {})
            if not info or info.get("hidden", False):
                continue
            entry = _AgentEntry(agent_id, info)
            new_agents[entry.tool_name] = entry
            new_id_map[agent_id] = entry.tool_name

        old_ids = set(self._agent_id_to_tool.keys())
        new_ids = set(new_id_map.keys())
        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)

        self._agents = new_agents
        self._agent_id_to_tool = new_id_map

        return added, removed

    def get_tools_summary(self) -> list[dict[str, Any]]:
        """Return a summary of all exposed tools for the admin UI."""
        result = []
        for entry in self._agents.values():
            result.append({
                "tool_name": entry.tool_name,
                "agent_id": entry.agent_id,
                "description": entry.description,
                "input_schema": entry.input_schema,
            })
        return result

    def get_agent_count(self) -> int:
        return len(self._agents)

    # ------------------------------------------------------------------
    # Transport builders
    # ------------------------------------------------------------------

    def build_sse_app(self) -> Starlette:
        """Build a Starlette ASGI app serving MCP over SSE."""
        sse_transport = SseServerTransport("/messages/")
        server = self._server

        async def handle_sse(request: Request) -> Response:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send,
            ) as streams:
                await server.run(
                    streams[0],
                    streams[1],
                    server.create_initialization_options(),
                )
            return Response()

        return Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse_transport.handle_post_message),
            ],
        )

    def build_streamable_http_app(self) -> Starlette:
        """Build a Starlette ASGI app serving MCP over streamable HTTP."""
        from mcp.server.streamable_http import StreamableHTTPServerTransport

        server = self._server

        async def handle_mcp(request: Request) -> Response:
            transport = StreamableHTTPServerTransport()
            async with transport.connect(
                request.scope, request.receive, request._send,
            ) as streams:
                await server.run(
                    streams[0],
                    streams[1],
                    server.create_initialization_options(),
                )
            return Response()

        return Starlette(routes=[Route("/mcp", endpoint=handle_mcp, methods=["POST"])])
