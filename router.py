"""
router.py — Central Router for the Unified Router for Agents system.

Implements the ESB-style router that handles all task routing, state
management, proxy file serving, access control, and agent onboarding.

Stack: FastAPI (asyncio), SQLite (WAL mode), httpx.

NOTE: For high-traffic production deployments, replace the synchronous
      sqlite3 DB calls with aiosqlite to avoid blocking the event loop.
      All DB helper functions are structured so the switch is straightforward:
      replace `sqlite3.connect` / `conn.cursor()` with `aiosqlite.connect`
      and add `await` where indicated.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from helper import AgentInfo, OnboardRequest

# ---------------------------------------------------------------------------
# Configuration (from environment variables with defaults)
# ---------------------------------------------------------------------------

DB_PATH: str = os.environ.get("DB_PATH", "router.db")
PROXYFILE_DIR: str = os.environ.get("PROXYFILE_DIR", "proxyfiles")
_PROJECT_ROOT: str = str(Path(__file__).resolve().parent)


def _is_safe_path(path: str, allowed_roots: list[str] | None = None) -> bool:
    """Check that a resolved path is under an allowed root directory."""
    resolved = Path(path).resolve()
    roots = [Path(r).resolve() for r in (allowed_roots or [_PROJECT_ROOT])]
    return any(resolved == root or str(resolved).startswith(str(root) + os.sep) for root in roots)


def _sanitize_task_id(task_id: str) -> str:
    """Strip path-traversal characters from task_id."""
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_\-]', '_', task_id)
    if not sanitized:
        sanitized = "unknown"
    return sanitized




AGENTS_DIR: str = os.environ.get("AGENTS_DIR", "agents")
GLOBAL_TIMEOUT_HOURS: int = int(os.environ.get("GLOBAL_TIMEOUT_HOURS", "1"))
MAX_DEPTH: int = int(os.environ.get("MAX_DEPTH", "10"))
MAX_WIDTH: int = int(os.environ.get("MAX_WIDTH", "50"))
MAX_PAYLOAD_BYTES: int = int(os.environ.get("MAX_PAYLOAD_BYTES", str(1 * 1024 * 1024)))  # 1 MB
MAX_FILE_BYTES: int = int(os.environ.get("MAX_FILE_BYTES", str(50 * 1024 * 1024)))  # 50 MB
ADMIN_TOKEN: str = os.environ.get("ADMIN_TOKEN", "")
EMBEDDED_AGENT_TIMEOUT: float = float(os.environ.get("EMBEDDED_AGENT_TIMEOUT", "300"))

# In-memory registry of loaded embedded agent ASGI apps.
# NOTE: For high-traffic production deployments, consider aiosqlite for DB
#       and a proper service-registry if agents can be hot-loaded.
embedded_apps: dict[str, Any] = {}

# Alive agents set — agents that are reachable.
# Embedded agents are always alive. External agents are probed periodically.
# Agents that send messages are auto-added.
_alive_agents: set[str] = set()
AGENT_HEALTH_INTERVAL: int = int(os.environ.get("AGENT_HEALTH_INTERVAL", "60"))

# In-memory progress event pub/sub.
# task_id → list of subscriber asyncio.Queues.
# Cleaned up when task reaches terminal state or all subscribers disconnect.
# Thread-safety note: All access happens in the single-threaded asyncio event
# loop.  Dict/list mutations between ``await`` points are atomic under
# cooperative scheduling, so no explicit lock is needed.
_progress_queues: dict[str, list[asyncio.Queue]] = {}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """
    Open and return a SQLite connection in WAL mode.

    NOTE: For high-traffic production deployments, replace with aiosqlite:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            ...

    Returns:
        A configured sqlite3.Connection with WAL mode and row_factory set.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """
    Initialise the database schema.

    Creates all tables if they do not already exist and inserts the default
    embedded→embedded group allowlist entry.

    NOTE: For high-traffic production deployments, use aiosqlite and await
          all execute/commit calls.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                parent_task_id TEXT,
                identifier TEXT,
                origin_agent_id TEXT NOT NULL,
                handler_agent_id TEXT,
                depth_count INTEGER NOT NULL,
                width_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                timeout_at DATETIME NOT NULL,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                destination_agent_id TEXT,
                event_type TEXT NOT NULL,
                status_code INTEGER,
                payload TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);

            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                endpoint_url TEXT,
                agent_path TEXT,
                auth_token TEXT NOT NULL,
                inbound_groups TEXT DEFAULT '[]',
                outbound_groups TEXT DEFAULT '[]',
                is_embedded INTEGER DEFAULT 0,
                agent_info TEXT DEFAULT '{}',
                documentation_path TEXT,
                registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS invitation_tokens (
                token TEXT PRIMARY KEY,
                inbound_groups TEXT DEFAULT '[]',
                outbound_groups TEXT DEFAULT '[]',
                expires_at DATETIME NOT NULL,
                used INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS group_allowlist (
                inbound_group TEXT NOT NULL,
                outbound_group TEXT NOT NULL,
                PRIMARY KEY (inbound_group, outbound_group)
            );

            CREATE TABLE IF NOT EXISTS individual_allowlist (
                agent_id TEXT NOT NULL,
                destination_agent_id TEXT NOT NULL,
                PRIMARY KEY (agent_id, destination_agent_id)
            );

            CREATE TABLE IF NOT EXISTS proxy_files (
                file_key TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                original_filename TEXT,
                task_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            );
        """)

        # Migration: add handler_agent_id column if missing (for existing DBs).
        cols = {row[1] for row in cursor.execute("PRAGMA table_info(tasks)").fetchall()}
        if "handler_agent_id" not in cols:
            cursor.execute("ALTER TABLE tasks ADD COLUMN handler_agent_id TEXT")

        # Seed default group allowlist rules.
        # These define the ACL routing policy between agent groups.
        # Additional rules can be added via the admin API at runtime.
        _default_rules = [
            ("embedded", "embedded"),   # legacy compat: embedded agents can call each other
            ("core", "infra"),          # core can call LLM
            ("core", "tool"),           # core can call stateless tools
            ("core", "usertool"),       # core can call user-specific tools
            ("core", "channel"),        # core can send DMs via channel
            ("channel", "core"),        # channel routes user messages to core
            ("tool", "infra"),          # tools can call LLM
            ("usertool", "infra"),      # user-tools can call LLM
            ("usertool", "tool"),       # user-tools can call stateless tools (e.g. kb→md_converter)
            ("notify", "core"),         # proactive agents (reminder/cron) can reach core
            ("notify", "channel"),      # proactive agents can reach channel for direct delivery
            ("bridge", "tool"),         # MCP bridge can expose stateless tools
            ("bridge", "infra"),        # MCP bridge can call LLM
            ("admin", "core"),          # admin web UI can test core
            ("admin", "tool"),          # admin can test tools
            ("admin", "usertool"),      # admin can test user-tools
            ("admin", "infra"),         # admin can test LLM
            ("admin", "channel"),       # admin can test channel delivery
        ]
        for outbound, inbound in _default_rules:
            cursor.execute(
                "INSERT OR IGNORE INTO group_allowlist (inbound_group, outbound_group) VALUES (?, ?)",
                (inbound, outbound),
            )

        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class RouteRequest(BaseModel):
    """Routing payload sent by agents to POST /route."""

    agent_id: str
    task_id: str
    identifier: Optional[str] = None
    parent_task_id: Optional[str] = None
    destination_agent_id: Optional[str] = None
    timestamp: str
    status_code: Optional[int] = None
    payload: dict[str, Any] = {}
    available_destinations: Optional[dict[str, Any]] = None




class InvitationCreateRequest(BaseModel):
    """Admin request to create a new invitation token."""

    inbound_groups: list[str] = []
    outbound_groups: list[str] = []
    expires_in_hours: int = 24


class GroupAllowlistRequest(BaseModel):
    """Admin request to add a group-level routing permission."""

    inbound_group: str
    outbound_group: str


class IndividualAllowlistRequest(BaseModel):
    """Admin request to add an individual agent routing permission."""

    agent_id: str
    destination_agent_id: str


class UpdateAgentGroupsRequest(BaseModel):
    """Admin request to update an agent's inbound/outbound group membership."""

    inbound_groups: list[str]
    outbound_groups: list[str]


class UpdateAgentInfoRequest(BaseModel):
    """Agent self-update of its own AgentInfo."""

    agent_id: str
    description: Optional[str] = None
    input_schema: Optional[str] = None
    output_schema: Optional[str] = None
    required_input: Optional[list[str]] = None
    documentation_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Embedded agent loader
# ---------------------------------------------------------------------------


async def load_embedded_agents() -> None:
    """
    Scan AGENTS_DIR for embedded agent packages and register them.

    Each subdirectory of AGENTS_DIR is treated as a potential agent package.
    A valid agent directory must contain ``agent.py`` (with a ``app: FastAPI``
    attribute) and optionally a ``.env`` file and an ``AGENT_INFO: AgentInfo``
    module-level attribute.

    Agents not yet present in the DB are auto-registered with:
    - A freshly generated auth token.
    - Group assignments per ``_EMBEDDED_AGENT_GROUPS`` mapping.
    - ``is_embedded = 1``.

    The loaded ASGI app is placed in the module-level ``embedded_apps`` dict
    keyed by agent_id (the subdirectory name).
    """
    # Per-agent group assignments for embedded agents.
    _EMBEDDED_AGENT_GROUPS: dict[str, tuple[list[str], list[str]]] = {
        # agent_id: (inbound_groups, outbound_groups)
        "core_personal_agent": (["core"], ["core"]),
        "llm_agent":           (["infra"], ["infra"]),
        "md_converter":        (["tool"], ["tool"]),
        "memory_agent":        (["usertool"], ["usertool"]),
        "web_agent":           (["tool"], ["tool"]),
    }
    _DEFAULT_GROUPS = (["embedded"], ["embedded"])

    agents_path = Path(AGENTS_DIR)
    if not agents_path.exists():
        return

    conn = get_db()
    try:
        for entry in agents_path.iterdir():
            if not entry.is_dir():
                continue

            agent_py = entry / "agent.py"
            if not agent_py.exists():
                continue

            agent_id = entry.name

            # Dynamically import the agent module.
            # (Agents read their own config.json directly — no env injection needed.)
            spec = importlib.util.spec_from_file_location(
                f"agents.{agent_id}.agent", str(agent_py)
            )
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            sys.modules[f"agents.{agent_id}.agent"] = module
            try:
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Exception as exc:
                print(f"[router] Failed to load embedded agent '{agent_id}': {exc}")
                continue

            app = getattr(module, "app", None)
            if app is None:
                print(f"[router] Skipping '{agent_id}': no 'app' attribute in agent.py")
                continue

            embedded_apps[agent_id] = app

            # Persist agent record if not already in DB.
            # NOTE: For high-traffic production deployments, use aiosqlite here.
            existing = conn.execute(
                "SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()

            if existing is None:
                auth_token = secrets.token_urlsafe(32)
                agent_info_obj = getattr(module, "AGENT_INFO", None)
                agent_info_json: str
                doc_url: Optional[str] = None
                if agent_info_obj is not None:
                    try:
                        agent_info_json = agent_info_obj.model_dump_json()
                        doc_url = getattr(agent_info_obj, "documentation_url", None)
                    except Exception:
                        agent_info_json = "{}"
                else:
                    agent_info_json = "{}"

                # Fetch and store documentation if the agent provides a URL.
                documentation_path: Optional[str] = None
                if doc_url:
                    try:
                        doc_bytes = await _fetch_documentation(doc_url)
                    except Exception as _doc_exc:
                        print(f"[router] Failed to fetch documentation for '{agent_id}': {_doc_exc}")
                        doc_bytes = None
                    if doc_bytes:
                        documentation_path = _store_agent_documentation(
                            agent_id, doc_bytes, conn
                        )

                inbound_g, outbound_g = _EMBEDDED_AGENT_GROUPS.get(agent_id, _DEFAULT_GROUPS)
                conn.execute(
                    """
                    INSERT INTO agents
                        (agent_id, agent_path, auth_token, inbound_groups,
                         outbound_groups, is_embedded, agent_info, documentation_path)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        agent_id,
                        str(agent_py),
                        auth_token,
                        json.dumps(inbound_g),
                        json.dumps(outbound_g),
                        agent_info_json,
                        documentation_path,
                    ),
                )
                conn.commit()
                print(f"[router] Registered embedded agent '{agent_id}'")
            else:
                # Existing embedded agent: regenerate a fresh token so we can
                # inject the raw value into the environment for the agent.
                auth_token = secrets.token_urlsafe(32)
                conn.execute(
                    "UPDATE agents SET auth_token = ? WHERE agent_id = ?",
                    (auth_token, agent_id),
                )
                conn.commit()

            # Inject the raw auth token as an env var for the agent to read.
            # NOTE: This runs AFTER exec_module(), so embedded agents must NOT
            # read this env var at import time — only lazily on first request.
            os.environ[f"{agent_id.upper()}_AUTH_TOKEN"] = auth_token

    finally:
        conn.close()




async def _fetch_documentation(url: str) -> Optional[bytes]:
    """
    Fetch documentation content from a URL.

    Supports ``http://``, ``https://``, ``file://``, and bare filesystem paths.

    Args:
        url: The documentation URL or local path.

    Returns:
        The raw bytes of the documentation, or None on failure.
    """
    if not url:
        return None

    # file:// URI or bare filesystem path
    if url.startswith("file://"):
        local_path = Path(url[7:])
    elif not url.startswith(("http://", "https://")):
        local_path = Path(url)
    else:
        local_path = None

    if local_path is not None:
        if not _is_safe_path(str(local_path)):
            print(f"[router] _fetch_documentation: rejected path outside project root: {local_path}")
            return None
        try:
            return local_path.read_bytes()
        except Exception as exc:
            print(f"[router] _fetch_documentation: failed to read '{local_path}': {exc}")
            return None

    # HTTP(S) fetch
    try:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            resp = await http_client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        print(f"[router] _fetch_documentation: failed to fetch '{url}': {exc}")
        return None


def _store_agent_documentation(
    agent_id: str,
    doc_bytes: bytes,
    conn: sqlite3.Connection,
) -> Optional[str]:
    """
    Write documentation bytes to the proxy vault and register in proxy_files.

    Args:
        agent_id: The agent this documentation belongs to.
        doc_bytes: Raw documentation content.
        conn: An open database connection (caller must commit).

    Returns:
        The on-disk path string, or None on failure.
    """
    try:
        doc_dir = Path(PROXYFILE_DIR) / "agent_documentations"
        doc_dir.mkdir(parents=True, exist_ok=True)
        doc_filename = f"{agent_id}.md"
        doc_dest = doc_dir / doc_filename
        doc_dest.write_bytes(doc_bytes)
        documentation_path = str(doc_dest)

        # Upsert proxy_files row.
        existing_pf = conn.execute(
            "SELECT file_key FROM proxy_files WHERE file_path = ?",
            (documentation_path,),
        ).fetchone()
        if not existing_pf:
            doc_file_key = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO proxy_files (file_key, file_path, original_filename, task_id) "
                "VALUES (?, ?, ?, NULL)",
                (doc_file_key, documentation_path, doc_filename),
            )
        return documentation_path
    except Exception as exc:
        print(f"[router] _store_agent_documentation: failed for '{agent_id}': {exc}")
        return None


# ---------------------------------------------------------------------------
# ACL helpers
# ---------------------------------------------------------------------------


def can_route(agent_id: str, dest_id: str, conn: sqlite3.Connection) -> bool:
    """
    Determine whether ``agent_id`` is permitted to send messages to ``dest_id``.

    ACL resolution order:
    1. If ``individual_allowlist`` has **any** entries for ``agent_id``, only
       those explicit destinations are permitted (group rules are ignored).
    2. Otherwise, resolve via group membership:
       - Fetch the agent's ``outbound_groups``.
       - Look up which ``inbound_groups`` are allowed to receive from those
         outbound groups via ``group_allowlist``.
       - Check if ``dest_id``'s ``inbound_groups`` intersect the allowed set.

    NOTE: For high-traffic production deployments, use aiosqlite here.

    Args:
        agent_id: The source agent ID.
        dest_id: The target agent ID.
        conn: An open database connection.

    Returns:
        True if routing is permitted, False otherwise.
    """
    # Check for any individual allowlist entries for this agent.
    individual_entries = conn.execute(
        "SELECT destination_agent_id FROM individual_allowlist WHERE agent_id = ?",
        (agent_id,),
    ).fetchall()

    if individual_entries:
        # Individual rules fully supersede group rules.
        allowed_ids = {row["destination_agent_id"] for row in individual_entries}
        return dest_id in allowed_ids

    # Group-based resolution.
    src_row = conn.execute(
        "SELECT outbound_groups FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if src_row is None:
        return False

    outbound_groups: list[str] = json.loads(src_row["outbound_groups"] or "[]")
    if not outbound_groups:
        return False

    # Find all inbound_groups that the outbound_groups are allowed to reach.
    placeholders = ",".join("?" * len(outbound_groups))
    allowed_inbound_rows = conn.execute(
        f"""
        SELECT DISTINCT inbound_group
        FROM group_allowlist
        WHERE outbound_group IN ({placeholders})
        """,
        outbound_groups,
    ).fetchall()

    allowed_inbound_groups = {row["inbound_group"] for row in allowed_inbound_rows}
    if not allowed_inbound_groups:
        return False

    # Check if dest_id belongs to any of the allowed inbound groups.
    dst_row = conn.execute(
        "SELECT inbound_groups FROM agents WHERE agent_id = ?", (dest_id,)
    ).fetchone()
    if dst_row is None:
        return False

    dest_inbound_groups: list[str] = json.loads(dst_row["inbound_groups"] or "[]")
    return bool(set(dest_inbound_groups) & allowed_inbound_groups)


def get_available_destinations(agent_id: str, conn: sqlite3.Connection) -> dict[str, Any]:
    """
    Build the ACL-filtered map of destinations the given agent may contact.

    The returned dict is keyed by agent_id and contains the destination
    agent's AgentInfo fields (description, input_schema, output_schema,
    required_input).

    NOTE: For high-traffic production deployments, use aiosqlite here.

    Args:
        agent_id: The agent whose reachable destinations should be computed.
        conn: An open database connection.

    Returns:
        A dict mapping reachable agent IDs to their AgentInfo metadata.
    """
    all_agents = conn.execute(
        "SELECT agent_id, agent_info, documentation_path FROM agents WHERE agent_id != ?",
        (agent_id,),
    ).fetchall()

    destinations: dict[str, Any] = {}
    for row in all_agents:
        dest_id: str = row["agent_id"]
        if dest_id not in _alive_agents:
            continue
        if can_route(agent_id, dest_id, conn):
            try:
                info = json.loads(row["agent_info"] or "{}")
            except json.JSONDecodeError:
                info = {}
            doc_path: Optional[str] = row["documentation_path"]
            if doc_path:
                doc_key_row = conn.execute(
                    "SELECT file_key FROM proxy_files WHERE file_path = ?",
                    (doc_path,),
                ).fetchone()
                info["documentation_file"] = {
                    "path": f"/docs/{dest_id}",
                    "protocol": "router-proxy",
                    "key": doc_key_row["file_key"] if doc_key_row else None,
                }
            else:
                info["documentation_file"] = None
            destinations[dest_id] = info

    return destinations


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _require_admin(authorization: str) -> None:
    """
    Validate an admin-level Authorization header.

    Args:
        authorization: The ``Authorization`` header value.

    Raises:
        HTTPException 401: If the header is missing or malformed.
        HTTPException 403: If the token does not match ADMIN_TOKEN.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Bearer token.")
    token = authorization[len("Bearer "):]
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_TOKEN environment variable is not configured.",
        )
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid admin token.")


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------


async def deliver_to_agent(agent_id: str, payload: dict[str, Any]) -> None:
    """
    Deliver a routing payload to an agent (embedded or external).

    For **embedded** agents: the router makes an in-process ASGI call via
    ``httpx.ASGITransport`` to ``POST /receive``.  A 200 response body is
    expected to be a valid routing payload dict; the router processes it
    inline via ``_process_route_internal``.

    For **external** agents: the router POSTs the payload to the agent's
    ``endpoint_url`` and expects a ``202`` acknowledgment.  Failure to
    receive 2xx is treated as a delivery error and the task is failed.

    NOTE: For high-traffic production deployments, use aiosqlite here.

    Args:
        agent_id: The target agent's ID.
        payload: The routing payload dict to deliver.
    """
    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT endpoint_url, is_embedded, auth_token FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        task_id = payload.get("task_id")
        if task_id and task_id != "new":
            conn2 = get_db()
            try:
                await _fail_task(
                    task_id,
                    agent_id,
                    f"Destination agent '{agent_id}' not found.",
                    conn2,
                )
            finally:
                conn2.close()
        return

    if row["is_embedded"]:
        await _deliver_embedded(agent_id, payload)
    else:
        await _deliver_external(agent_id, row["endpoint_url"], payload, row["auth_token"])


async def _deliver_embedded(agent_id: str, payload: dict[str, Any]) -> None:
    """
    Deliver a payload to an embedded agent via in-process ASGI transport.

    The embedded agent's ``POST /receive`` endpoint is called synchronously
    (from the router's perspective). A 200 response body is parsed as a
    routing payload and fed back into ``_process_route_internal``.

    Args:
        agent_id: The embedded agent's ID.
        payload: The routing payload to deliver.
    """
    app = embedded_apps.get(agent_id)
    if app is None:
        task_id = payload.get("task_id")
        if task_id and task_id != "new":
            conn = get_db()
            try:
                await _fail_task(
                    task_id,
                    agent_id,
                    f"Embedded agent '{agent_id}' ASGI app not loaded.",
                    conn,
                )
            finally:
                conn.close()
        return

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://embedded") as client:
            response = await client.post("/receive", json=payload, timeout=EMBEDDED_AGENT_TIMEOUT)
    except Exception as exc:
        task_id = payload.get("task_id")
        if task_id and task_id != "new":
            conn = get_db()
            try:
                await _fail_task(
                    task_id,
                    agent_id,
                    f"ASGI delivery to embedded agent '{agent_id}' raised: {exc}",
                    conn,
                )
            finally:
                conn.close()
        return

    if response.status_code == 200:
        try:
            response_data = response.json()
        except Exception:
            return
        if isinstance(response_data, dict):
            try:
                await _process_route_internal(response_data)
            except Exception as exc:
                print(f"[router] Error processing embedded agent '{agent_id}' response: {exc}")
                resp_task_id = response_data.get("task_id")
                if resp_task_id and resp_task_id != "new":
                    conn = get_db()
                    try:
                        await _fail_task(
                            resp_task_id,
                            agent_id,
                            f"Error processing response from embedded agent '{agent_id}': {exc}",
                            conn,
                        )
                    finally:
                        conn.close()
    else:
        task_id = payload.get("task_id")
        if task_id and task_id != "new":
            conn = get_db()
            try:
                await _fail_task(
                    task_id,
                    agent_id,
                    f"Embedded agent '{agent_id}' returned HTTP {response.status_code}.",
                    conn,
                )
            finally:
                conn.close()


async def _deliver_external(
    agent_id: str, endpoint_url: str, payload: dict[str, Any],
    auth_token: str = "",
) -> None:
    """
    Deliver a payload to an external agent via HTTP POST.

    Expects a ``202`` acknowledgment.  Any non-2xx status or connection
    error causes the task to be failed.

    Args:
        agent_id: The external agent's ID.
        endpoint_url: The URL to POST to.
        payload: The routing payload to deliver.
        auth_token: Raw auth token to send as Bearer header for verification.
    """
    # NOTE: For high-traffic production deployments, consider a shared
    #       AsyncClient with connection pooling rather than per-call clients.
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(endpoint_url, json=payload, headers=headers)
    except Exception as exc:
        task_id = payload.get("task_id")
        if task_id and task_id != "new":
            conn = get_db()
            try:
                await _fail_task(
                    task_id,
                    agent_id,
                    f"HTTP delivery to external agent '{agent_id}' failed: {exc}",
                    conn,
                )
            finally:
                conn.close()
        return

    if not (200 <= response.status_code < 300):
        task_id = payload.get("task_id")
        if task_id and task_id != "new":
            conn = get_db()
            try:
                await _fail_task(
                    task_id,
                    agent_id,
                    (
                        f"External agent '{agent_id}' returned HTTP "
                        f"{response.status_code} instead of 202."
                    ),
                    conn,
                )
            finally:
                conn.close()


async def _fail_task(
    task_id: str,
    reporting_agent_id: str,
    reason: str,
    conn: sqlite3.Connection,
) -> None:
    """
    Mark a task as failed, log an error event, and propagate the error to
    the task's origin agent.

    NOTE: For high-traffic production deployments, use aiosqlite here.

    Args:
        task_id: The task to fail.
        reporting_agent_id: The agent or component reporting the failure.
        reason: Human-readable failure description.
        conn: An open database connection.
    """
    now = datetime.now(timezone.utc).isoformat()

    task_row = conn.execute(
        "SELECT origin_agent_id, identifier, status FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()

    if task_row is None:
        return

    if task_row["status"] in ("completed", "failed", "timeout"):
        return

    conn.execute(
        "UPDATE tasks SET status = 'failed' WHERE task_id = ?",
        (task_id,),
    )

    conn.execute(
        """
        INSERT INTO events (task_id, agent_id, event_type, payload, timestamp)
        VALUES (?, ?, 'error', ?, ?)
        """,
        (task_id, reporting_agent_id, json.dumps({"reason": reason}), now),
    )
    conn.commit()

    origin_agent_id: str = task_row["origin_agent_id"]
    identifier: Optional[str] = task_row["identifier"]

    error_payload: dict[str, Any] = {
        "agent_id": "router",
        "task_id": task_id,
        "identifier": identifier,
        "parent_task_id": None,
        "destination_agent_id": None,
        "timestamp": now,
        "status_code": 500,
        "payload": {"content": reason, "error": reason},
    }

    # Notify progress subscribers that the task failed.
    done_event = {
        "type": "done",
        "content": "",
        "task_id": task_id,
        "status_code": 500,
        "timestamp": now,
    }
    for q in _progress_queues.get(task_id, []):
        try:
            q.put_nowait(done_event)
        except asyncio.QueueFull:
            pass

    # Deliver error asynchronously to avoid recursive DB locks.
    asyncio.create_task(deliver_to_agent(origin_agent_id, error_payload))


# ---------------------------------------------------------------------------
# Core routing logic
# ---------------------------------------------------------------------------


async def _ingest_payload_files(
    payload: dict[str, Any],
    task_id: str,
) -> tuple[dict[str, Any], list[tuple[str, str, str, str]]]:
    """
    Scan *payload* for ProxyFile objects (protocol "http" or "localfile"),
    fetch them into the router's proxy file store, and replace with
    ``router-proxy`` references.

    Returns ``(updated_payload, db_rows)`` where *db_rows* is a list of
    ``(file_key, dest_path, original_filename, task_id)`` tuples to be
    bulk-inserted into ``proxy_files`` by the caller inside its own DB
    connection.  This keeps all async I/O (HTTP fetches, file copies)
    **outside** the synchronous SQLite connection scope.
    """

    db_rows: list[tuple[str, str, str, str]] = []

    def _is_proxy_file(obj: Any) -> bool:
        return (
            isinstance(obj, dict)
            and "path" in obj
            and "protocol" in obj
            and obj["protocol"] in ("http", "localfile")
        )

    async def _ingest_one(pf: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Fetch a single file and return a router-proxy ProxyFile dict."""
        protocol = pf["protocol"]
        path = pf["path"]
        original_filename = pf.get("original_filename") or Path(path.split("?")[0]).name or "file"
        safe_filename = Path(original_filename).name

        try:
            # Prepare destination path under proxyfiles/{task_id}/
            dest_dir = Path(PROXYFILE_DIR) / _sanitize_task_id(task_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / safe_filename
            if dest_path.exists():
                stem = dest_path.stem
                suffix = dest_path.suffix
                dest_path = dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
                safe_filename = dest_path.name

            if protocol == "http":
                async with httpx.AsyncClient(timeout=120.0) as client:
                    async with client.stream("GET", path) as resp:
                        resp.raise_for_status()
                        with open(dest_path, "wb") as fh:
                            async for chunk in resp.aiter_bytes():
                                fh.write(chunk)
            elif protocol == "localfile":
                if not _is_safe_path(path):
                    print(f"[router] Rejected localfile outside project root: {path}")
                    return None  # drop unsafe file
                import shutil
                shutil.copy2(path, dest_path)
            else:
                print(f"[router] Unknown protocol '{protocol}' for file '{path}'")
                return None  # drop unknown protocol

            # Enforce file size limit
            file_size = dest_path.stat().st_size
            if file_size > MAX_FILE_BYTES:
                dest_path.unlink(missing_ok=True)
                print(f"[router] Rejected file '{original_filename}' ({file_size} bytes) exceeding MAX_FILE_BYTES ({MAX_FILE_BYTES})")
                return None

            file_key = secrets.token_urlsafe(32)
            logical_path = f"/files/{task_id}/{safe_filename}"

            db_rows.append((file_key, str(dest_path), original_filename, task_id))

            print(f"[router] Ingested file '{original_filename}' → {logical_path}")
            return {
                "path": logical_path,
                "protocol": "router-proxy",
                "key": file_key,
            }
        except Exception as exc:
            print(f"[router] Failed to ingest file '{path}': {exc}")
            return None  # drop unresolvable file

    # Scan for "files" key containing a list of ProxyFile dicts.
    files_list = payload.get("files")
    if isinstance(files_list, list):
        new_files = []
        for item in files_list:
            if _is_proxy_file(item):
                result = await _ingest_one(item)
                if result is not None:
                    new_files.append(result)
            else:
                new_files.append(item)
        payload["files"] = new_files if new_files else None

    # Also check top-level keys for individual ProxyFile objects.
    for key, val in list(payload.items()):
        if key == "files":
            continue
        if _is_proxy_file(val):
            result = await _ingest_one(val)
            if result is not None:
                payload[key] = result
            else:
                # Remove unresolvable file reference
                del payload[key]

    return payload, db_rows


def _flush_proxy_file_rows(
    conn: sqlite3.Connection,
    db_rows: list[tuple[str, str, str, str]],
) -> None:
    """Bulk-insert ingested proxy-file rows into the database."""
    for row in db_rows:
        conn.execute(
            "INSERT INTO proxy_files (file_key, file_path, original_filename, task_id) "
            "VALUES (?, ?, ?, ?)",
            row,
        )


async def _process_route_internal(data: dict[str, Any]) -> dict[str, Any]:
    """
    Execute the full routing logic for a routing payload dict.

    This function is the heart of the router. It handles three cases:

    1. **Spawn** (``task_id == "new"``): Generate a new task UUID, validate
       depth/width limits, persist the task and an event record, strip the
       ``identifier`` from the forwarded payload, inject ``available_destinations``,
       and deliver to the destination agent.

    2. **Result / completion** (``destination_agent_id is None``): Validate
       that the task exists and the caller is a legitimate reporter, update
       task status (completed / failed), inject the stored ``identifier``,
       and deliver the result back to the origin agent.

    3. **Delegation** (``task_id`` is an existing UUID and
       ``destination_agent_id`` is not None): Only valid between LLM-backed
       agents. Increment ``width_count``, log a delegation event, inject
       ``available_destinations``, and deliver to the new handler.

    Each case uses short-lived DB connections: a read-only conn for
    validation, then async file ingestion (no conn held), then a
    write conn for persisting state.

    Args:
        data: A routing payload dict (may come from the /route endpoint or
              from an embedded agent's synchronous 200 response).

    Returns:
        A dict with ``{"status": "accepted", "task_id": <task_id>}``.

    Raises:
        HTTPException: On validation failures (400, 403, 508).
    """
    agent_id: str = data.get("agent_id", "")
    task_id: str = data.get("task_id", "")
    identifier: Optional[str] = data.get("identifier")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    destination_agent_id: Optional[str] = data.get("destination_agent_id")
    payload: dict[str, Any] = data.get("payload", {})
    status_code: Optional[int] = data.get("status_code")

    # Auto-revive: if we receive a message from a registered agent, mark it alive.
    if agent_id and agent_id not in _alive_agents:
        _alive_agents.add(agent_id)

    # ----------------------------------------------------------------
    # Case 1: Spawn new task
    # ----------------------------------------------------------------
    if task_id == "new":
        if destination_agent_id is None:
            raise HTTPException(
                status_code=400,
                detail="destination_agent_id is required when spawning a new task.",
            )

        # --- validation reads (short-lived conn) ---
        conn = get_db()
        try:
            if not can_route(agent_id, destination_agent_id, conn):
                raise HTTPException(
                    status_code=403,
                    detail=f"Agent '{agent_id}' is not permitted to route to '{destination_agent_id}'.",
                )

            parent_depth = 0
            if parent_task_id:
                parent_row = conn.execute(
                    "SELECT depth_count FROM tasks WHERE task_id = ?",
                    (parent_task_id,),
                ).fetchone()
                if parent_row:
                    parent_depth = parent_row["depth_count"]
        finally:
            conn.close()

        new_depth = parent_depth + 1
        if new_depth > MAX_DEPTH:
            raise HTTPException(
                status_code=508,
                detail=f"Maximum task depth of {MAX_DEPTH} exceeded.",
            )

        new_task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        timeout_at = now + timedelta(hours=GLOBAL_TIMEOUT_HOURS)

        # --- async file ingestion (no conn held) ---
        payload, file_rows = await _ingest_payload_files(payload, new_task_id)

        # --- DB writes (short-lived conn) ---
        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO tasks
                    (task_id, parent_task_id, identifier, origin_agent_id,
                     handler_agent_id, depth_count, width_count,
                     created_at, timeout_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'active')
                """,
                (
                    new_task_id,
                    parent_task_id,
                    identifier,
                    agent_id,
                    destination_agent_id,
                    new_depth,
                    now.isoformat(),
                    timeout_at.isoformat(),
                ),
            )

            _flush_proxy_file_rows(conn, file_rows)

            conn.execute(
                """
                INSERT INTO events
                    (task_id, agent_id, destination_agent_id, event_type,
                     payload, timestamp)
                VALUES (?, ?, ?, 'spawn', ?, ?)
                """,
                (
                    new_task_id,
                    agent_id,
                    destination_agent_id,
                    json.dumps(payload),
                    now.isoformat(),
                ),
            )
            conn.commit()

            available_destinations = get_available_destinations(destination_agent_id, conn)
        finally:
            conn.close()

        forward_payload: dict[str, Any] = {
            "agent_id": agent_id,
            "task_id": new_task_id,
            # identifier is stripped before forwarding to the destination
            "identifier": None,
            "parent_task_id": parent_task_id,
            "destination_agent_id": destination_agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
            "available_destinations": available_destinations,
        }

        asyncio.create_task(deliver_to_agent(destination_agent_id, forward_payload))
        return {"status": "accepted", "task_id": new_task_id}

    # ----------------------------------------------------------------
    # Case 2: Result / completion (destination_agent_id is None)
    # ----------------------------------------------------------------
    if destination_agent_id is None:
        # --- validation reads (short-lived conn) ---
        conn = get_db()
        try:
            task_row = conn.execute(
                """
                SELECT task_id, origin_agent_id, handler_agent_id, identifier, status
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        finally:
            conn.close()

        if task_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Task '{task_id}' not found.",
            )

        if task_row["status"] in ("completed", "failed", "timeout"):
            raise HTTPException(
                status_code=409,
                detail=f"Task '{task_id}' is already in terminal state '{task_row['status']}'.",
            )

        # Verify the sender is the agent this task was assigned to.
        handler = task_row["handler_agent_id"]
        if handler and handler != agent_id:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{agent_id}' is not the handler of task '{task_id}'.",
            )

        new_status = "completed" if (status_code is None or status_code < 400) else "failed"
        origin_agent_id: str = task_row["origin_agent_id"]
        stored_identifier: Optional[str] = task_row["identifier"]

        # --- async file ingestion (no conn held) ---
        payload, file_rows = await _ingest_payload_files(payload, task_id)

        # --- DB writes (short-lived conn) ---
        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        try:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (new_status, task_id),
            )

            _flush_proxy_file_rows(conn, file_rows)

            conn.execute(
                """
                INSERT INTO events
                    (task_id, agent_id, event_type, status_code, payload, timestamp)
                VALUES (?, ?, 'result', ?, ?, ?)
                """,
                (
                    task_id,
                    agent_id,
                    status_code,
                    json.dumps(payload),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        result_payload: dict[str, Any] = {
            "agent_id": agent_id,
            "task_id": task_id,
            "identifier": stored_identifier,
            "parent_task_id": parent_task_id,
            "destination_agent_id": None,
            "timestamp": now,
            "status_code": status_code,
            "payload": payload,
        }

        asyncio.create_task(deliver_to_agent(origin_agent_id, result_payload))

        # Notify progress subscribers that the task is done.
        done_event = {
            "type": "done",
            "content": "",
            "task_id": task_id,
            "status_code": status_code,
            "timestamp": now,
        }
        for q in _progress_queues.get(task_id, []):
            try:
                q.put_nowait(done_event)
            except asyncio.QueueFull:
                pass

        return {"status": "accepted", "task_id": task_id}

    # ----------------------------------------------------------------
    # Case 3: Delegation (existing task_id + destination_agent_id set)
    # ----------------------------------------------------------------
    # --- validation reads (short-lived conn) ---
    conn = get_db()
    try:
        task_row = conn.execute(
            """
            SELECT task_id, origin_agent_id, handler_agent_id, width_count, status
            FROM tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()

        if task_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Task '{task_id}' not found.",
            )

        if task_row["status"] in ("completed", "failed", "timeout"):
            raise HTTPException(
                status_code=409,
                detail=f"Task '{task_id}' is already in terminal state '{task_row['status']}'.",
            )

        # Verify the sender is the agent this task was assigned to.
        handler = task_row["handler_agent_id"]
        if handler and handler != agent_id:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{agent_id}' is not the handler of task '{task_id}'.",
            )

        if not can_route(agent_id, destination_agent_id, conn):
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{agent_id}' is not permitted to route to '{destination_agent_id}'.",
            )
    finally:
        conn.close()

    new_width = task_row["width_count"] + 1
    if new_width > MAX_WIDTH:
        raise HTTPException(
            status_code=508,
            detail=f"Maximum task width of {MAX_WIDTH} exceeded.",
        )

    # --- async file ingestion (no conn held) ---
    payload, file_rows = await _ingest_payload_files(payload, task_id)

    # --- DB writes (short-lived conn) ---
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE tasks SET width_count = ?, handler_agent_id = ? WHERE task_id = ?",
            (new_width, destination_agent_id, task_id),
        )

        _flush_proxy_file_rows(conn, file_rows)

        conn.execute(
            """
            INSERT INTO events
                (task_id, agent_id, destination_agent_id, event_type,
                 payload, timestamp)
            VALUES (?, ?, ?, 'delegation', ?, ?)
            """,
            (
                task_id,
                agent_id,
                destination_agent_id,
                json.dumps(payload),
                now,
            ),
        )
        conn.commit()

        available_destinations = get_available_destinations(destination_agent_id, conn)
    finally:
        conn.close()

    delegation_payload: dict[str, Any] = {
        "agent_id": agent_id,
        "task_id": task_id,
        "identifier": None,
        "parent_task_id": parent_task_id,
        "destination_agent_id": destination_agent_id,
        "timestamp": now,
        "payload": payload,
        "available_destinations": available_destinations,
    }

    asyncio.create_task(deliver_to_agent(destination_agent_id, delegation_payload))
    return {"status": "accepted", "task_id": task_id}


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def timeout_sweep() -> None:
    """
    Background loop that runs every 60 seconds.

    Finds tasks whose ``timeout_at`` has passed and status is still ``active``,
    marks them ``timeout``, and propagates an error payload back to each
    task's origin agent.

    NOTE: For high-traffic production deployments, use aiosqlite here.
    """
    while True:
        await asyncio.sleep(60)
        now = datetime.now(timezone.utc).isoformat()
        # NOTE: For high-traffic production deployments, use aiosqlite here.
        conn = get_db()
        try:
            timed_out = conn.execute(
                """
                SELECT task_id, origin_agent_id, identifier
                FROM tasks
                WHERE status = 'active' AND timeout_at <= ?
                """,
                (now,),
            ).fetchall()

            actually_timed_out: list[sqlite3.Row] = []
            for row in timed_out:
                task_id: str = row["task_id"]

                cur = conn.execute(
                    "UPDATE tasks SET status = 'timeout' WHERE task_id = ? AND status = 'active'",
                    (task_id,),
                )
                if cur.rowcount == 0:
                    continue  # task completed/failed between SELECT and UPDATE
                actually_timed_out.append(row)
                conn.execute(
                    """
                    INSERT INTO events
                        (task_id, agent_id, event_type, payload, timestamp)
                    VALUES (?, 'router', 'error', ?, ?)
                    """,
                    (
                        task_id,
                        json.dumps({"reason": "Task timed out."}),
                        now,
                    ),
                )

            conn.commit()

            for row in actually_timed_out:
                task_id = row["task_id"]
                origin_agent_id = row["origin_agent_id"]
                identifier = row["identifier"]

                error_payload: dict[str, Any] = {
                    "agent_id": "router",
                    "task_id": task_id,
                    "identifier": identifier,
                    "parent_task_id": None,
                    "destination_agent_id": None,
                    "timestamp": now,
                    "status_code": 504,
                    "payload": {"content": "Task timed out.", "error": "Task timed out."},
                }
                asyncio.create_task(deliver_to_agent(origin_agent_id, error_payload))

                # Notify progress subscribers that the task timed out.
                done_event = {
                    "type": "done",
                    "content": "",
                    "task_id": task_id,
                    "status_code": 504,
                    "timestamp": now,
                }
                for q in _progress_queues.get(task_id, []):
                    try:
                        q.put_nowait(done_event)
                    except asyncio.QueueFull:
                        pass

        except Exception as exc:
            print(f"[router] timeout_sweep error: {exc}")
        finally:
            conn.close()


async def gc_proxy_files() -> None:
    """
    Background loop that runs every 3600 seconds (1 hour).

    Deletes proxy files on disk whose associated tasks have reached a terminal
    state (completed, failed, or timeout).  Also removes the corresponding
    records from the ``proxy_files`` table.

    NOTE: For high-traffic production deployments, use aiosqlite here.
    """
    while True:
        await asyncio.sleep(3600)
        # NOTE: For high-traffic production deployments, use aiosqlite here.
        conn = get_db()
        try:
            orphaned = conn.execute(
                """
                SELECT pf.file_key, pf.file_path
                FROM proxy_files pf
                JOIN tasks t ON pf.task_id = t.task_id
                WHERE t.status IN ('completed', 'failed', 'timeout')
                """,
            ).fetchall()

            deleted_keys: list[str] = []
            for row in orphaned:
                file_path = Path(row["file_path"])
                try:
                    if file_path.exists():
                        file_path.unlink()
                    # Try to remove the parent directory if empty.
                    parent = file_path.parent
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError as exc:
                    print(f"[router] gc_proxy_files: failed to delete {file_path}: {exc}")
                    continue
                deleted_keys.append(row["file_key"])

            if deleted_keys:
                placeholders = ",".join("?" * len(deleted_keys))
                conn.execute(
                    f"DELETE FROM proxy_files WHERE file_key IN ({placeholders})",
                    deleted_keys,
                )
                conn.commit()
                print(f"[router] gc_proxy_files: cleaned {len(deleted_keys)} file(s).")

        except Exception as exc:
            print(f"[router] gc_proxy_files error: {exc}")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Agent health check
# ---------------------------------------------------------------------------


AGENT_HEALTH_INITIAL_DELAY: int = int(os.environ.get("AGENT_HEALTH_INITIAL_DELAY", "30"))
AGENT_INFO_REFRESH_CYCLES: int = int(os.environ.get("AGENT_INFO_REFRESH_CYCLES", "10"))  # refresh info every N health cycles


async def _agent_health_loop() -> None:
    """
    Periodically probe all registered agents and maintain ``_alive_agents``.

    Embedded agents are always alive.  External agents are probed by
    attempting a HEAD/GET to their endpoint_url.  Agents that fail are
    removed from the alive set (but not unregistered).
    """
    # Initial population: assume all registered agents are alive (optimistic).
    # This avoids a window where external agents are invisible before the
    # first health check.  Dead agents will be pruned after the first cycle.
    conn = get_db()
    try:
        all_ids = [r["agent_id"] for r in conn.execute("SELECT agent_id FROM agents").fetchall()]
        _alive_agents.update(all_ids)
    finally:
        conn.close()

    # Delay the first health check to give external agents time to start.
    await asyncio.sleep(AGENT_HEALTH_INITIAL_DELAY)

    cycle = 0
    while True:
        cycle += 1
        do_info_refresh = (cycle % AGENT_INFO_REFRESH_CYCLES == 0)

        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT agent_id, endpoint_url, is_embedded, auth_token FROM agents"
            ).fetchall()
        finally:
            conn.close()

        new_alive: set[str] = set()
        for row in rows:
            agent_id = row["agent_id"]
            if row["is_embedded"]:
                new_alive.add(agent_id)
                # Refresh embedded agent info from loaded module.
                if do_info_refresh:
                    module = sys.modules.get(f"agents.{agent_id}.agent")
                    if module is not None:
                        info_obj = getattr(module, "AGENT_INFO", None)
                        if info_obj is not None:
                            try:
                                info_json = info_obj.model_dump_json()
                                conn2 = get_db()
                                try:
                                    conn2.execute(
                                        "UPDATE agents SET agent_info = ? WHERE agent_id = ?",
                                        (info_json, agent_id),
                                    )
                                    conn2.commit()
                                finally:
                                    conn2.close()
                            except Exception:
                                pass
                continue

            endpoint_url = row["endpoint_url"]
            if not endpoint_url:
                continue

            # Derive base URL from endpoint_url.
            base = endpoint_url.rsplit("/receive", 1)[0] if endpoint_url.endswith("/receive") else endpoint_url.rstrip("/")

            # Health probe.
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{base}/health")
                if r.status_code < 500:
                    new_alive.add(agent_id)
            except Exception:
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        r = await client.head(base)
                    if r.status_code < 500:
                        new_alive.add(agent_id)
                except Exception:
                    pass

            # Trigger agent info refresh for alive external agents.
            if do_info_refresh and agent_id in new_alive:
                try:
                    refresh_headers = {"Authorization": f"Bearer {row['auth_token']}"}
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(f"{base}/refresh-info", headers=refresh_headers)
                except Exception:
                    pass

        removed = _alive_agents - new_alive
        added = new_alive - _alive_agents
        if removed:
            print(f"[router] Agents went offline: {', '.join(sorted(removed))}")
        if added:
            print(f"[router] Agents came online: {', '.join(sorted(added))}")

        _alive_agents.clear()
        _alive_agents.update(new_alive)

        await asyncio.sleep(AGENT_HEALTH_INTERVAL)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager.

    On startup:
    - Initialise the database schema.
    - Scan AGENTS_DIR and load embedded agents.
    - Start background sweeper tasks.

    On shutdown:
    - Cancel background tasks gracefully.
    """
    init_db()
    await load_embedded_agents()

    sweep_task = asyncio.create_task(timeout_sweep())
    gc_task = asyncio.create_task(gc_proxy_files())
    health_task = asyncio.create_task(_agent_health_loop())

    yield

    sweep_task.cancel()
    gc_task.cancel()
    health_task.cancel()
    try:
        await sweep_task
    except asyncio.CancelledError:
        pass
    try:
        await gc_task
    except asyncio.CancelledError:
        pass
    try:
        await health_task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Unified Router for Agents",
    description=(
        "Central ESB-style router for AI agent communication. "
        "Handles all routing, state, file proxies, and access control."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _check_content_length(request, call_next):
    """Reject oversized request bodies early, before Pydantic parsing."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_PAYLOAD_BYTES:
        return JSONResponse(
            status_code=413,
            content={"detail": f"Payload exceeds maximum allowed size of {MAX_PAYLOAD_BYTES} bytes."},
        )
    # For POST/PUT/PATCH without Content-Length (e.g. chunked), read and check.
    if request.method in ("POST", "PUT", "PATCH") and not cl:
        body = await request.body()
        if len(body) > MAX_PAYLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Payload exceeds maximum allowed size of {MAX_PAYLOAD_BYTES} bytes."},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a simple liveness probe response."""
    return {"status": "ok"}


@app.post("/route")
async def route(
    request: RouteRequest,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Main routing endpoint.  All agent-to-router communication flows through
    here.

    - Validates the Bearer token against the agent_id in the request body.
    - Enforces the MAX_PAYLOAD_BYTES limit.
    - Delegates to ``_process_route_internal`` for routing logic.

    Returns:
        ``{"status": "accepted", "task_id": <task_id>}``
    """
    # Auth
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Bearer token.")
    token = authorization[len("Bearer "):]

    def _db_check_route_auth(agent_id: str) -> Optional[str]:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT auth_token FROM agents WHERE agent_id = ?", (agent_id,),
            ).fetchone()
            return row["auth_token"] if row else None
        finally:
            conn.close()

    stored_token = await asyncio.to_thread(_db_check_route_auth, request.agent_id)
    if stored_token is None or not secrets.compare_digest(stored_token, token):
        raise HTTPException(status_code=403, detail="Invalid credentials.")

    # Payload size guard (rough estimate via JSON serialisation).
    payload_bytes = len(json.dumps(request.model_dump()).encode("utf-8"))
    if payload_bytes > MAX_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Payload exceeds maximum allowed size of {MAX_PAYLOAD_BYTES} bytes.",
        )

    result = await _process_route_internal(request.model_dump())
    return result


@app.post("/onboard")
async def onboard(request: OnboardRequest) -> dict[str, Any]:
    """
    Register an external agent using a one-time invitation token.

    Validates that the token exists, has not been used, and has not expired.
    Generates a fresh auth token for the agent, persists the agent record,
    marks the invitation token as used, and returns the agent's credentials
    and initial available_destinations.

    Returns:
        A dict with agent_id, auth_token, inbound_groups, outbound_groups,
        and available_destinations.
    """
    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        token_row = conn.execute(
            """
            SELECT token, inbound_groups, outbound_groups, expires_at, used
            FROM invitation_tokens
            WHERE token = ?
            """,
            (request.invitation_token,),
        ).fetchone()

        if token_row is None:
            raise HTTPException(status_code=400, detail="Invalid invitation token.")

        if token_row["used"]:
            raise HTTPException(status_code=400, detail="Invitation token has already been used.")

        expires_at = datetime.fromisoformat(token_row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=400, detail="Invitation token has expired.")

        inbound_groups: list[str] = json.loads(token_row["inbound_groups"] or "[]")
        outbound_groups: list[str] = json.loads(token_row["outbound_groups"] or "[]")

        requested_id = (request.agent_info.agent_id or "").strip()
        if requested_id:
            import re as _re_onboard
            if not _re_onboard.fullmatch(r'[a-zA-Z0-9_\-]{1,64}', requested_id):
                raise HTTPException(
                    status_code=400,
                    detail="agent_id must be 1-64 characters, alphanumeric/underscore/hyphen only.",
                )
            conflict = conn.execute(
                "SELECT 1 FROM agents WHERE agent_id = ?", (requested_id,)
            ).fetchone()
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail=f"Agent ID '{requested_id}' is already registered.",
                )
            agent_id = requested_id
        else:
            agent_id = str(uuid.uuid4())
        auth_token = secrets.token_urlsafe(32)

        # Fetch and store documentation if a URL was provided.
        documentation_path: Optional[str] = None
        documentation_url: Optional[str] = request.agent_info.documentation_url
        if documentation_url:
            doc_bytes = await _fetch_documentation(documentation_url)
            if doc_bytes:
                documentation_path = _store_agent_documentation(
                    agent_id, doc_bytes, conn
                )

        agent_info_json = request.agent_info.model_dump_json()

        conn.execute(
            """
            INSERT INTO agents
                (agent_id, endpoint_url, auth_token, inbound_groups,
                 outbound_groups, is_embedded, agent_info, documentation_path)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                agent_id,
                request.endpoint_url,
                auth_token,
                json.dumps(inbound_groups),
                json.dumps(outbound_groups),
                agent_info_json,
                documentation_path,
            ),
        )

        conn.execute(
            "UPDATE invitation_tokens SET used = 1 WHERE token = ?",
            (request.invitation_token,),
        )
        conn.commit()

        available_destinations = get_available_destinations(agent_id, conn)

    finally:
        conn.close()

    # Make the newly onboarded agent immediately visible to other agents
    # (otherwise it's invisible until the next health-loop cycle).
    _alive_agents.add(agent_id)

    return {
        "agent_id": agent_id,
        "auth_token": auth_token,
        "inbound_groups": inbound_groups,
        "outbound_groups": outbound_groups,
        "available_destinations": available_destinations,
    }


@app.put("/agent-info")
async def update_agent_info(
    request: UpdateAgentInfoRequest,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Allow an authenticated agent to update its own AgentInfo.

    Performs a partial update: only non-None fields in the request body are
    merged into the existing ``agent_info`` JSON stored in the database.
    If ``documentation_url`` is provided and differs from the stored value,
    the new documentation is fetched and stored in the proxy file vault.

    Returns:
        A dict with ``status`` and the merged ``agent_info``.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Bearer token.")
    token = authorization[len("Bearer "):]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT auth_token, agent_info, documentation_path FROM agents WHERE agent_id = ?",
            (request.agent_id,),
        ).fetchone()

        if row is None or not secrets.compare_digest(row["auth_token"], token):
            raise HTTPException(status_code=403, detail="Invalid credentials.")

        try:
            existing_info = json.loads(row["agent_info"] or "{}")
        except json.JSONDecodeError:
            existing_info = {}

        # Merge non-None fields.
        if request.description is not None:
            existing_info["description"] = request.description
        if request.input_schema is not None:
            existing_info["input_schema"] = request.input_schema
        if request.output_schema is not None:
            existing_info["output_schema"] = request.output_schema
        if request.required_input is not None:
            existing_info["required_input"] = request.required_input

        # Handle documentation_url change.
        documentation_path: Optional[str] = row["documentation_path"]
        if request.documentation_url is not None:
            old_doc_url = existing_info.get("documentation_url")
            existing_info["documentation_url"] = request.documentation_url
            if request.documentation_url != old_doc_url and request.documentation_url:
                doc_bytes = await _fetch_documentation(request.documentation_url)
                if doc_bytes:
                    new_path = _store_agent_documentation(
                        request.agent_id, doc_bytes, conn
                    )
                    if new_path:
                        documentation_path = new_path

        conn.execute(
            "UPDATE agents SET agent_info = ?, documentation_path = ? WHERE agent_id = ?",
            (json.dumps(existing_info), documentation_path, request.agent_id),
        )
        conn.commit()

    finally:
        conn.close()

    return {"status": "updated", "agent_info": existing_info}


async def _authenticate_agent(authorization: str) -> str:
    """
    Validate an agent-level Authorization header.

    Returns the agent_id on success.

    Raises:
        HTTPException: 401 if malformed, 403 if credentials are invalid.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Bearer token.")
    token = authorization[len("Bearer "):]

    def _db_lookup() -> Optional[str]:
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT agent_id FROM agents WHERE auth_token = ?",
                (token,),
            ).fetchone()
            return row["agent_id"] if row else None
        finally:
            conn.close()

    aid = await asyncio.to_thread(_db_lookup)
    if aid is None:
        raise HTTPException(status_code=403, detail="Invalid credentials.")
    # Auto-revive: agent authenticated successfully, so it's alive.
    _alive_agents.add(aid)
    return aid


@app.get("/agent/destinations")
async def agent_get_destinations(
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Return connection health and ACL-filtered available destinations for the
    calling agent.

    Authenticated with the agent's own Bearer token (not admin token).
    Useful for external agents that need to refresh their destinations
    cache outside of normal task delivery.

    Returns:
        A dict with ``agent_id``, ``status`` ("ok"), and
        ``available_destinations``.
    """
    agent_id = await _authenticate_agent(authorization)
    conn = get_db()
    try:
        destinations = get_available_destinations(agent_id, conn)
    finally:
        conn.close()
    return {
        "agent_id": agent_id,
        "status": "ok",
        "available_destinations": destinations,
    }


# ---------------------------------------------------------------------------
# Progress event pub/sub
# ---------------------------------------------------------------------------


class ProgressEvent(BaseModel):
    """A lightweight progress event pushed during task execution."""
    type: str  # "thinking", "tool_call", "tool_result", "status", "chunk"
    content: str = ""
    metadata: dict[str, Any] = {}


@app.post("/tasks/{task_id}/progress")
async def push_progress(
    task_id: str,
    event: ProgressEvent,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Push a progress event for an active task.

    Authenticated with the agent's Bearer token.  The event is delivered
    to all SSE subscribers for this task.
    """
    agent_id = await _authenticate_agent(authorization)

    event_data = {
        "type": event.type,
        "content": event.content,
        "metadata": event.metadata,
        "agent_id": agent_id,
        "task_id": task_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    queues = _progress_queues.get(task_id, [])
    for q in queues:
        try:
            q.put_nowait(event_data)
        except asyncio.QueueFull:
            pass  # Drop if subscriber is too slow

    return {"status": "ok"}


@app.get("/tasks/{task_id}/progress")
async def subscribe_progress(
    task_id: str,
    authorization: str = Header(...),
) -> StreamingResponse:
    """
    Subscribe to progress events for a task via Server-Sent Events.

    The stream stays open until the task completes, the client disconnects,
    or a 5-minute inactivity timeout is reached.
    """
    await _authenticate_agent(authorization)

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _progress_queues.setdefault(task_id, []).append(queue)

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'timeout'})}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            subs = _progress_queues.get(task_id, [])
            if queue in subs:
                subs.remove(queue)
            if not subs:
                _progress_queues.pop(task_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/files/{task_id}/{filename}")
async def download_file(
    task_id: str,
    filename: str,
    key: str,
) -> FileResponse:
    """
    Serve a proxy file.

    Validates the per-file ``key`` query parameter against the
    ``proxy_files`` table before returning the file.

    Query params:
        key: The per-file access key returned at upload time.

    Returns:
        A streaming FileResponse for the requested file.
    """
    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT file_path, file_key FROM proxy_files WHERE file_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="File not found or invalid key.")

    file_path = Path(row["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File no longer exists on disk.")

    return FileResponse(path=str(file_path), filename=filename)


@app.get("/docs/{agent_id}")
async def download_agent_docs(
    agent_id: str,
    key: str,
) -> FileResponse:
    """
    Serve an agent's documentation file from the proxy vault.

    Documentation files are stored with ``task_id=NULL`` and cannot be
    served through the regular ``/files/{task_id}/{filename}`` endpoint.

    Query params:
        key: The per-file access key for the documentation file.

    Returns:
        A streaming FileResponse for the documentation markdown.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT documentation_path FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None or not row["documentation_path"]:
            raise HTTPException(status_code=404, detail="No documentation for this agent.")

        doc_path = row["documentation_path"]
        pf_row = conn.execute(
            "SELECT file_key FROM proxy_files WHERE file_path = ?",
            (doc_path,),
        ).fetchone()
    finally:
        conn.close()

    if pf_row is None or not secrets.compare_digest(pf_row["file_key"], key):
        raise HTTPException(status_code=403, detail="Invalid documentation key.")

    file_path = Path(doc_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Documentation file no longer exists on disk.")

    return FileResponse(path=str(file_path), filename=f"{agent_id}.md")


@app.post("/admin/invitation")
async def create_invitation(
    request: InvitationCreateRequest,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Create a single-use invitation token for external agent onboarding.

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        A dict with token, inbound_groups, outbound_groups, and expires_at.
    """
    _require_admin(authorization)

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=request.expires_in_hours)

    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO invitation_tokens
                (token, inbound_groups, outbound_groups, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                token,
                json.dumps(request.inbound_groups),
                json.dumps(request.outbound_groups),
                expires_at.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "token": token,
        "inbound_groups": request.inbound_groups,
        "outbound_groups": request.outbound_groups,
        "expires_at": expires_at.isoformat(),
    }


@app.get("/admin/agents")
async def list_agents(
    authorization: str = Header(...),
) -> list[dict[str, Any]]:
    """
    List all registered agents (auth tokens excluded).

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        A list of agent records.
    """
    _require_admin(authorization)

    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT agent_id, endpoint_url, agent_path, inbound_groups,
                   outbound_groups, is_embedded, agent_info,
                   documentation_path, registered_at
            FROM agents
            ORDER BY registered_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        agent_id = row["agent_id"]
        result.append(
            {
                "agent_id": agent_id,
                "endpoint_url": row["endpoint_url"],
                "agent_path": row["agent_path"],
                "inbound_groups": json.loads(row["inbound_groups"] or "[]"),
                "outbound_groups": json.loads(row["outbound_groups"] or "[]"),
                "is_embedded": bool(row["is_embedded"]),
                "is_alive": agent_id in _alive_agents,
                "agent_info": json.loads(row["agent_info"] or "{}"),
                "documentation_path": row["documentation_path"],
                "registered_at": row["registered_at"],
            }
        )
    return result


@app.delete("/admin/agents/{agent_id}")
async def disconnect_agent(
    agent_id: str,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Remove an external agent from the router.

    Embedded agents cannot be removed via this endpoint.
    Also removes the agent's individual allowlist entries.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT is_embedded FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found.")
        if row["is_embedded"]:
            raise HTTPException(status_code=400, detail="Embedded agents cannot be removed.")
        conn.execute("DELETE FROM individual_allowlist WHERE agent_id = ? OR destination_agent_id = ?", (agent_id, agent_id))
        conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        conn.commit()
    finally:
        conn.close()
    _alive_agents.discard(agent_id)
    return {"status": "ok"}


@app.patch("/admin/agents/{agent_id}/groups")
async def update_agent_groups(
    agent_id: str,
    request: UpdateAgentGroupsRequest,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Update the inbound and outbound group membership of a registered agent.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found.")
        conn.execute(
            "UPDATE agents SET inbound_groups = ?, outbound_groups = ? WHERE agent_id = ?",
            (json.dumps(request.inbound_groups), json.dumps(request.outbound_groups), agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "agent_id": agent_id,
        "inbound_groups": request.inbound_groups,
        "outbound_groups": request.outbound_groups,
    }


@app.get("/admin/agents/{agent_id}/config")
async def admin_get_agent_config(
    agent_id: str,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Read the config.json for an embedded agent.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    agent_dir = Path(AGENTS_DIR) / agent_id
    if not _is_safe_path(str(agent_dir), [AGENTS_DIR]):
        raise HTTPException(status_code=400, detail="Invalid agent_id.")
    if not agent_dir.is_dir():
        raise HTTPException(status_code=404, detail="Agent directory not found.")
    config_path = agent_dir / "config.json"
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="No config.json for this agent.")
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read config: {exc}")


class UpdateAgentConfigRequest(BaseModel):
    """Admin request to write an embedded agent's config.json."""
    config: dict[str, Any]


@app.put("/admin/agents/{agent_id}/config")
async def admin_put_agent_config(
    agent_id: str,
    request: UpdateAgentConfigRequest,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Write config.json for an embedded agent.

    Requires the ADMIN_TOKEN in the Authorization header.
    Changes take effect on the next request to the agent.
    """
    _require_admin(authorization)
    agent_dir = Path(AGENTS_DIR) / agent_id
    if not _is_safe_path(str(agent_dir), [AGENTS_DIR]):
        raise HTTPException(status_code=400, detail="Invalid agent_id.")
    if not agent_dir.is_dir():
        raise HTTPException(status_code=404, detail="Agent directory not found.")
    config_path = agent_dir / "config.json"
    try:
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(request.config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.rename(config_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {exc}")
    return {"status": "updated", "agent_id": agent_id}


@app.get("/admin/agents/{agent_id}/config-example")
async def admin_get_agent_config_example(
    agent_id: str,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Read the config.example for an embedded agent (field descriptions).

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    agent_dir = Path(AGENTS_DIR) / agent_id
    if not _is_safe_path(str(agent_dir), [AGENTS_DIR]):
        raise HTTPException(status_code=400, detail="Invalid agent_id.")
    if not agent_dir.is_dir():
        raise HTTPException(status_code=404, detail="Agent directory not found.")
    example_path = agent_dir / "config.example"
    if not example_path.exists():
        return {}
    try:
        return json.loads(example_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read config.example: {exc}")


class UpdateAgentDocumentationRequest(BaseModel):
    """Admin request to set or replace an agent's documentation content."""
    content: str


@app.put("/admin/agents/{agent_id}/documentation")
async def admin_update_agent_documentation(
    agent_id: str,
    request: UpdateAgentDocumentationRequest,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Create or overwrite the documentation file for an agent and update the
    router's ``agents.documentation_path`` and ``proxy_files`` table.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT documentation_path FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found.")

        doc_bytes = request.content.encode("utf-8")
        documentation_path = _store_agent_documentation(agent_id, doc_bytes, conn)
        if documentation_path is None:
            raise HTTPException(status_code=500, detail="Failed to store documentation.")

        conn.execute(
            "UPDATE agents SET documentation_path = ? WHERE agent_id = ?",
            (documentation_path, agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "updated", "agent_id": agent_id, "documentation_path": documentation_path}


@app.get("/admin/agents/{agent_id}/documentation")
async def admin_get_agent_documentation(
    agent_id: str,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Read the documentation content for an agent.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT documentation_path, agent_info FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    doc_path = row["documentation_path"]
    content = ""
    if doc_path:
        p = Path(doc_path)
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8")
            except Exception:
                content = "(unable to read file)"
    try:
        info = json.loads(row["agent_info"] or "{}")
    except json.JSONDecodeError:
        info = {}
    return {
        "agent_id": agent_id,
        "documentation_url": info.get("documentation_url"),
        "documentation_path": doc_path,
        "content": content,
    }


@app.post("/admin/agents/{agent_id}/refresh-info")
async def admin_refresh_agent_info(
    agent_id: str,
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Refresh an agent's stored AgentInfo and re-fetch its documentation.

    For **embedded** agents the ``AGENT_INFO`` attribute is re-read from
    the loaded module and documentation is re-fetched from
    ``documentation_url``.

    For **external** agents the router POSTs to the agent's
    ``/refresh-info`` endpoint (derived from ``endpoint_url``), which
    tells the agent to re-push its AgentInfo via ``PUT /agent-info``.
    The router then re-reads the updated record.

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        A dict with the refresh status and updated agent_info.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT agent_info, documentation_path, is_embedded, endpoint_url, auth_token "
            "FROM agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found.")

        try:
            info = json.loads(row["agent_info"] or "{}")
        except json.JSONDecodeError:
            info = {}

        info_refreshed = False
        doc_refreshed = False
        documentation_path: Optional[str] = row["documentation_path"]
        agent_signal_error: Optional[str] = None

        if row["is_embedded"]:
            # Re-read AGENT_INFO from the loaded module.
            module = sys.modules.get(f"agents.{agent_id}.agent")
            if module is not None:
                agent_info_obj = getattr(module, "AGENT_INFO", None)
                if agent_info_obj is not None:
                    try:
                        info = json.loads(agent_info_obj.model_dump_json())
                        info_refreshed = True
                    except Exception:
                        pass

            # Re-fetch documentation from documentation_url.
            doc_url = info.get("documentation_url")
            if doc_url:
                doc_bytes = await _fetch_documentation(doc_url)
                if doc_bytes:
                    new_path = _store_agent_documentation(agent_id, doc_bytes, conn)
                    if new_path:
                        documentation_path = new_path
                        doc_refreshed = True

            if info_refreshed or doc_refreshed:
                conn.execute(
                    "UPDATE agents SET agent_info = ?, documentation_path = ? WHERE agent_id = ?",
                    (json.dumps(info), documentation_path, agent_id),
                )
                conn.commit()
        else:
            # External agent: signal it to re-push its AgentInfo.
            endpoint_url: Optional[str] = row["endpoint_url"]
            if endpoint_url:
                # Derive base URL from endpoint_url (strip /receive suffix).
                base_url = endpoint_url.rsplit("/receive", 1)[0] if endpoint_url.endswith("/receive") else endpoint_url.rstrip("/")
                refresh_url = f"{base_url}/refresh-info"
                try:
                    refresh_headers = {"Authorization": f"Bearer {row['auth_token']}"}
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(refresh_url, headers=refresh_headers)
                    if 200 <= resp.status_code < 300:
                        info_refreshed = True
                    else:
                        agent_signal_error = f"Agent returned {resp.status_code}"
                except Exception as exc:
                    agent_signal_error = str(exc)
            else:
                agent_signal_error = "No endpoint_url registered"

            # Re-read the updated agent record (the agent may have pushed new info).
            if info_refreshed:
                updated_row = conn.execute(
                    "SELECT agent_info, documentation_path FROM agents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                if updated_row:
                    try:
                        info = json.loads(updated_row["agent_info"] or "{}")
                    except json.JSONDecodeError:
                        pass
                    documentation_path = updated_row["documentation_path"]
                    doc_refreshed = documentation_path != row["documentation_path"]

    finally:
        conn.close()

    result: dict[str, Any] = {
        "status": "refreshed" if (info_refreshed or doc_refreshed) else "no_change",
        "agent_id": agent_id,
        "info_refreshed": info_refreshed,
        "doc_refreshed": doc_refreshed,
        "agent_info": info,
    }
    if agent_signal_error:
        result["agent_signal_error"] = agent_signal_error
    return result


@app.delete("/admin/proxy-files/{file_key}")
async def delete_proxy_file(
    file_key: str,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Delete a proxy file record and the file from disk.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT file_path, task_id FROM proxy_files WHERE file_key = ?", (file_key,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Proxy file not found.")
        if row["task_id"] is None:
            raise HTTPException(
                status_code=403,
                detail="Cannot delete agent documentation files via this endpoint.",
            )
        file_path = row["file_path"]
        conn.execute("DELETE FROM proxy_files WHERE file_key = ?", (file_key,))
        conn.commit()
    finally:
        conn.close()
    try:
        Path(file_path).unlink(missing_ok=True)
    except Exception:
        pass
    return {"status": "ok"}


@app.delete("/admin/invitation-tokens/{token}")
async def delete_invitation_token(
    token: str,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Delete an invitation token.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM invitation_tokens WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Token not found.")
        conn.execute("DELETE FROM invitation_tokens WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.delete("/admin/log")
async def clear_log(
    authorization: str = Header(...),
) -> dict[str, Any]:
    """
    Delete all completed/failed/timeout task records and their events.

    Active tasks are not removed.

    Requires the ADMIN_TOKEN in the Authorization header.
    """
    _require_admin(authorization)
    conn = get_db()
    try:
        result = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status != 'active'"
        ).fetchone()
        count = result[0] if result else 0
        # Collect file paths before deleting records so we can unlink from disk.
        file_rows = conn.execute(
            """
            SELECT file_path FROM proxy_files WHERE task_id IN (
                SELECT task_id FROM tasks WHERE status != 'active'
            )
            """
        ).fetchall()
        conn.execute(
            """
            DELETE FROM proxy_files WHERE task_id IN (
                SELECT task_id FROM tasks WHERE status != 'active'
            )
            """
        )
        conn.execute(
            """
            DELETE FROM events WHERE task_id IN (
                SELECT task_id FROM tasks WHERE status != 'active'
            )
            """
        )
        conn.execute("DELETE FROM tasks WHERE status != 'active'")
        conn.commit()
    finally:
        conn.close()
    # Remove actual files from disk.
    for row in file_rows:
        try:
            Path(row[0]).unlink(missing_ok=True)
        except Exception:
            pass
    return {"status": "ok", "deleted_tasks": count}


@app.get("/admin/tasks")
async def list_tasks(
    authorization: str = Header(...),
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    List tasks with optional filters.

    Requires the ADMIN_TOKEN in the Authorization header.

    Query params:
        status: Filter by task status (active, completed, failed, timeout).
        agent_id: Filter by origin_agent_id.
        limit: Maximum number of results (default 100).

    Returns:
        A list of task records.
    """
    _require_admin(authorization)

    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []

    if status:
        query += " AND status = ?"
        params.append(status)

    if agent_id:
        query += " AND origin_agent_id = ?"
        params.append(agent_id)

    limit = min(limit, 10000)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows]


@app.post("/admin/group-allowlist")
async def add_group_allowlist(
    request: GroupAllowlistRequest,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Add a group-level routing permission entry.

    Agents in ``outbound_group`` will be allowed to send messages to agents
    in ``inbound_group``.

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        ``{"status": "ok"}``
    """
    _require_admin(authorization)

    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO group_allowlist (inbound_group, outbound_group)
            VALUES (?, ?)
            """,
            (request.inbound_group, request.outbound_group),
        )
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok"}


@app.post("/admin/individual-allowlist")
async def add_individual_allowlist(
    request: IndividualAllowlistRequest,
    authorization: str = Header(...),
) -> dict[str, str]:
    """
    Add an individual agent routing permission entry.

    Once any individual entry exists for an agent, that agent's routing
    decisions are governed solely by the individual allowlist (group rules
    are ignored for that agent).

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        ``{"status": "ok"}``
    """
    _require_admin(authorization)

    # NOTE: For high-traffic production deployments, use aiosqlite here.
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO individual_allowlist
                (agent_id, destination_agent_id)
            VALUES (?, ?)
            """,
            (request.agent_id, request.destination_agent_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok"}


@app.get("/admin/group-allowlist")
async def list_group_allowlist(
    authorization: str = Header(...),
) -> list[dict[str, str]]:
    """List all group-level routing permission entries."""
    _require_admin(authorization)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT inbound_group, outbound_group FROM group_allowlist ORDER BY inbound_group, outbound_group"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.delete("/admin/group-allowlist")
async def delete_group_allowlist(
    request: GroupAllowlistRequest,
    authorization: str = Header(...),
) -> dict[str, str]:
    """Remove a group-level routing permission entry."""
    _require_admin(authorization)
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM group_allowlist WHERE inbound_group = ? AND outbound_group = ?",
            (request.inbound_group, request.outbound_group),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/admin/individual-allowlist")
async def list_individual_allowlist(
    authorization: str = Header(...),
) -> list[dict[str, str]]:
    """List all individual agent routing permission entries."""
    _require_admin(authorization)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT agent_id, destination_agent_id FROM individual_allowlist ORDER BY agent_id, destination_agent_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.delete("/admin/individual-allowlist")
async def delete_individual_allowlist(
    request: IndividualAllowlistRequest,
    authorization: str = Header(...),
) -> dict[str, str]:
    """Remove an individual agent routing permission entry."""
    _require_admin(authorization)
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM individual_allowlist WHERE agent_id = ? AND destination_agent_id = ?",
            (request.agent_id, request.destination_agent_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.get("/admin/invitations")
async def list_invitations(
    authorization: str = Header(...),
) -> list[dict[str, Any]]:
    """
    List all invitation tokens.

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        A list of invitation token records (tokens included).
    """
    _require_admin(authorization)

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT token, inbound_groups, outbound_groups,
                   expires_at, used, created_at
            FROM invitation_tokens
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "token": row["token"],
                "inbound_groups": json.loads(row["inbound_groups"] or "[]"),
                "outbound_groups": json.loads(row["outbound_groups"] or "[]"),
                "expires_at": row["expires_at"],
                "used": bool(row["used"]),
                "created_at": row["created_at"],
            }
        )
    return result


@app.get("/admin/proxy-files")
async def list_proxy_files(
    authorization: str = Header(...),
) -> list[dict[str, Any]]:
    """
    List all proxy files in the vault.

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        A list of proxy file records.
    """
    _require_admin(authorization)

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT file_key, original_filename, task_id, created_at
            FROM proxy_files
            WHERE task_id IS NOT NULL
            ORDER BY created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows]


@app.get("/admin/events/{task_id}")
async def list_events(
    task_id: str,
    authorization: str = Header(...),
) -> list[dict[str, Any]]:
    """
    List all events for a specific task in chronological order.

    Requires the ADMIN_TOKEN in the Authorization header.

    Returns:
        A list of event records for the task.
    """
    _require_admin(authorization)

    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT event_id, task_id, agent_id, destination_agent_id,
                   event_type, status_code, payload, timestamp
            FROM events
            WHERE task_id = ?
            ORDER BY event_id ASC
            """,
            (task_id,),
        ).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows]
