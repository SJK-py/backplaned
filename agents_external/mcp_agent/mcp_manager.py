"""
mcp_manager.py — MCP server connection manager.

Manages connections to multiple MCP servers (stdio, SSE, streamable HTTP),
tracks their tools, and dispatches tool calls to the correct session.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Configuration model (persisted to JSON)
# ---------------------------------------------------------------------------


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""

    name: str
    transport_type: Optional[Literal["stdio", "sse", "streamableHttp"]] = None
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    headers: dict[str, str] = {}
    enabled_tools: list[str] = ["*"]
    tool_timeout: int = 30
    enabled: bool = True


# ---------------------------------------------------------------------------
# Runtime state (not persisted)
# ---------------------------------------------------------------------------


@dataclass
class ToolDef:
    """Snapshot of a tool definition from an MCP server."""

    server_name: str
    name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPServerState:
    """Runtime state for a single MCP server connection."""

    config: MCPServerConfig
    session: Any = None  # mcp.ClientSession
    tools: list[ToolDef] = field(default_factory=list)
    status: str = "disconnected"  # connected, disconnected, error
    error: Optional[str] = None
    exit_stack: Optional[AsyncExitStack] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# MCPManager
# ---------------------------------------------------------------------------


class MCPManager:
    """
    Manages connections to multiple MCP servers and dispatches tool calls.
    """

    def __init__(self, config_file: Path) -> None:
        self._config_file = config_file
        self._servers: dict[str, MCPServerState] = {}
        self._tool_index: dict[str, MCPServerState] = {}  # namespaced_name -> state
        self._load_config()

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        if self._config_file.exists():
            try:
                raw = json.loads(self._config_file.read_text())
                configs = [MCPServerConfig(**s) for s in raw.get("servers", [])]
            except Exception:
                configs = []
        else:
            configs = []

        self._servers.clear()
        for cfg in configs:
            self._servers[cfg.name] = MCPServerState(config=cfg)

    def _save_config(self) -> None:
        # Preserve non-server keys (e.g. LLM_AGENT_ID, LLM_MODEL_ID)
        data: dict = {}
        if self._config_file.exists():
            try:
                data = json.loads(self._config_file.read_text())
            except Exception:
                pass
        data["servers"] = [s.config.model_dump() for s in self._servers.values()]
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._config_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(self._config_file)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect_all(self) -> None:
        """Connect to all enabled servers."""
        for name, state in list(self._servers.items()):
            if state.config.enabled and state.status != "connected":
                await self.connect_server(name)

    async def connect_server(self, name: str) -> None:
        """Connect to a single MCP server by name."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        state = self._servers.get(name)
        if state is None:
            return

        # Disconnect first if already connected.
        if state.exit_stack is not None:
            await self.disconnect_server(name)

        cfg = state.config
        try:
            transport_type = cfg.transport_type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    state.status = "error"
                    state.error = "No command or url configured"
                    return

            stack = AsyncExitStack()
            await stack.__aenter__()

            if transport_type == "stdio":
                params = StdioServerParameters(
                    command=cfg.command,
                    args=cfg.args,
                    env=cfg.env or None,
                )
                read, write = await stack.enter_async_context(stdio_client(params))

            elif transport_type == "sse":
                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {**(cfg.headers or {}), **(headers or {})}
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )

            elif transport_type == "streamableHttp":
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                state.status = "error"
                state.error = f"Unknown transport type: {transport_type}"
                await stack.aclose()
                return

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            # List and filter tools.
            tools_result = await session.list_tools()
            enabled_set = set(cfg.enabled_tools)
            allow_all = "*" in enabled_set

            tools: list[ToolDef] = []
            for tool_def in tools_result.tools:
                namespaced = f"{name}__{tool_def.name}"
                if not allow_all and tool_def.name not in enabled_set and namespaced not in enabled_set:
                    continue
                tools.append(ToolDef(
                    server_name=name,
                    name=tool_def.name,
                    namespaced_name=namespaced,
                    description=tool_def.description or tool_def.name,
                    input_schema=tool_def.inputSchema or {"type": "object", "properties": {}},
                ))

            state.session = session
            state.tools = tools
            state.exit_stack = stack
            state.status = "connected"
            state.error = None

            # Rebuild tool index.
            self._rebuild_tool_index()

            print(f"[mcp] Server '{name}': connected, {len(tools)} tools registered")

        except Exception as exc:
            state.status = "error"
            state.error = str(exc)
            state.session = None
            state.tools = []
            if state.exit_stack is not None:
                try:
                    await state.exit_stack.aclose()
                except Exception:
                    pass
                state.exit_stack = None
            self._rebuild_tool_index()
            print(f"[mcp] Server '{name}': failed to connect: {exc}")

    async def disconnect_server(self, name: str) -> None:
        """Disconnect a single MCP server."""
        state = self._servers.get(name)
        if state is None:
            return

        if state.exit_stack is not None:
            try:
                await state.exit_stack.aclose()
            except Exception:
                pass

        state.session = None
        state.tools = []
        state.exit_stack = None
        state.status = "disconnected"
        state.error = None
        self._rebuild_tool_index()
        print(f"[mcp] Server '{name}': disconnected")

    async def disconnect_all(self) -> None:
        """Disconnect all servers."""
        for name in list(self._servers.keys()):
            await self.disconnect_server(name)

    async def reconnect_server(self, name: str) -> None:
        """Disconnect then reconnect a server."""
        await self.disconnect_server(name)
        await self.connect_server(name)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """
        Call an MCP tool by its namespaced name.

        Returns the result as a text string.
        Raises KeyError if tool not found, RuntimeError on call failure.
        """
        from mcp import types

        state = self._tool_index.get(tool_name)
        if state is None:
            available = sorted(self._tool_index.keys())
            raise KeyError(
                f"Tool '{tool_name}' not found. Available tools: {', '.join(available) or '(none)'}"
            )

        if state.session is None:
            raise RuntimeError(f"Server '{state.config.name}' is not connected.")

        # Find the raw MCP tool name.
        raw_name = tool_name
        prefix = f"{state.config.name}__"
        if raw_name.startswith(prefix):
            raw_name = raw_name[len(prefix):]

        timeout = state.config.tool_timeout

        async with state.lock:
            try:
                result = await asyncio.wait_for(
                    state.session.call_tool(raw_name, arguments=arguments),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"MCP tool '{tool_name}' timed out after {timeout}s"
                )
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                raise RuntimeError(
                    f"MCP tool '{tool_name}' was cancelled by server/SDK"
                )

        parts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        return "\n".join(parts) or "(no output)"

    # ------------------------------------------------------------------
    # Tool queries
    # ------------------------------------------------------------------

    def get_all_tools(self) -> list[ToolDef]:
        """Return all tools from all connected, enabled servers."""
        tools: list[ToolDef] = []
        for state in self._servers.values():
            if state.status == "connected":
                tools.extend(state.tools)
        return tools

    def get_server_tools(self, name: str) -> list[ToolDef]:
        """Return tools from a specific server."""
        state = self._servers.get(name)
        if state is None:
            return []
        return list(state.tools)

    # ------------------------------------------------------------------
    # Server config mutations
    # ------------------------------------------------------------------

    def add_server(self, config: MCPServerConfig) -> None:
        """Add a new server configuration."""
        if config.name in self._servers:
            raise ValueError(f"Server '{config.name}' already exists.")
        self._servers[config.name] = MCPServerState(config=config)
        self._save_config()

    def remove_server_config(self, name: str) -> None:
        """Remove a server configuration (caller should disconnect first)."""
        self._servers.pop(name, None)
        self._rebuild_tool_index()
        self._save_config()

    def update_server_config(self, name: str, **kwargs: Any) -> None:
        """Update fields on a server's config."""
        state = self._servers.get(name)
        if state is None:
            raise KeyError(f"Server '{name}' not found.")
        data = state.config.model_dump()
        data.update(kwargs)
        state.config = MCPServerConfig(**data)
        self._save_config()

    # ------------------------------------------------------------------
    # Server state queries
    # ------------------------------------------------------------------

    def get_server_states(self) -> dict[str, dict[str, Any]]:
        """Return a summary of all servers and their status."""
        result: dict[str, dict[str, Any]] = {}
        for name, state in self._servers.items():
            result[name] = {
                "config": state.config.model_dump(),
                "status": state.status,
                "error": state.error,
                "tool_count": len(state.tools),
                "tools": [
                    {
                        "name": t.namespaced_name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                    }
                    for t in state.tools
                ],
            }
        return result

    def get_server_names(self) -> list[str]:
        return list(self._servers.keys())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rebuild_tool_index(self) -> None:
        self._tool_index.clear()
        for state in self._servers.values():
            if state.status == "connected":
                for tool in state.tools:
                    self._tool_index[tool.namespaced_name] = state
