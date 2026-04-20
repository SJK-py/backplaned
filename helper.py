"""
helper.py — Client-side utilities for the Unified Router for Agents system.

Provides Pydantic models, message builders, LLM tool builders, onboarding
helpers, and a RouterClient for agents to interact with the central router.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Literal, Optional

import httpx
from pydantic import BaseModel

_helper_logger = logging.getLogger("helper")


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256, stdlib only)
# ---------------------------------------------------------------------------

_PW_ITERATIONS = 600_000  # OWASP recommendation for PBKDF2-SHA256


def hash_password(password: str) -> str:
    """Return a ``salt:hash`` string using PBKDF2-SHA256."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PW_ITERATIONS)
    return f"{salt}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify *password* against a ``salt:hash`` string from :func:`hash_password`.

    Also accepts a plain-text ``stored`` value (no ``:``) for backward
    compatibility — the caller should re-hash and persist afterward.
    """
    if ":" not in stored:
        # Legacy plain-text — constant-time compare
        return hmac.compare_digest(password, stored)
    salt, expected_hex = stored.split(":", 1)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PW_ITERATIONS)
    return hmac.compare_digest(dk.hex(), expected_hex)


def is_password_hashed(stored: str) -> bool:
    """Return True if *stored* looks like a ``salt:hash`` from :func:`hash_password`."""
    if ":" not in stored:
        return False
    salt, hx = stored.split(":", 1)
    return len(salt) == 32 and len(hx) == 64


class PasswordFile:
    """Manage a single hashed admin password stored in a JSON file.

    The file format is ``{"password_hash": "<salt>:<hash>"}``.

    On first use, if the file doesn't exist, the password from the
    environment variable (``initial_password``) is hashed and persisted.
    """

    def __init__(self, path: str | Path, initial_password: str = "") -> None:
        self._path = Path(path)
        self._data: dict[str, str] = {}
        self._load(initial_password)

    def _load(self, initial_password: str) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}
        if "password_hash" not in self._data:
            # First run or missing — hash the initial password from env
            if initial_password:
                self._data["password_hash"] = hash_password(initial_password)
            else:
                self._data["password_hash"] = ""
            self._save()
        elif initial_password and not is_password_hashed(self._data["password_hash"]):
            # Migrate legacy plain-text stored value
            self._data["password_hash"] = hash_password(self._data["password_hash"])
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def verify(self, password: str) -> bool:
        """Check *password* against the stored hash."""
        stored = self._data.get("password_hash", "")
        if not stored:
            return False
        ok = verify_password(password, stored)
        # Auto-migrate legacy plain-text on successful verify
        if ok and not is_password_hashed(stored):
            self._data["password_hash"] = hash_password(password)
            self._save()
        return ok

    def change(self, new_password: str) -> None:
        """Replace the stored hash with a new password."""
        self._data["password_hash"] = hash_password(new_password)
        self._save()


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class ProxyFile(BaseModel):
    """
    Represents a file managed by the router's proxy file system.

    Agents share files by passing ProxyFile objects. The router garbage-
    collects unreferenced files after their associated tasks complete.
    """

    path: str
    """Logical path or URL used to retrieve the file."""

    protocol: Literal["router-proxy", "http", "localfile"]
    """Transport protocol for this file."""

    key: Optional[str] = None
    """Per-file access key issued by the router on upload."""

    original_filename: Optional[str] = None
    """Original filename hint. Used by fetch() and router ingestion to
    preserve the real filename when the path/URL doesn't contain it."""


# TODO: ProxyStream — future streaming support (WebSocket / SSE) between
# router and agents.  Planned fields: stream_id, task_id, origin_agent_id,
# destination_agent_id, protocol ("websocket"|"sse"), endpoint, key,
# created_at, expires_at.


class AgentInfo(BaseModel):
    """
    Metadata that an agent publishes to the router so other agents can
    discover its capabilities and build correct call payloads.
    """

    agent_id: str
    description: str
    input_schema: str
    """
    A type-definition string describing the agent's expected inputs,
    e.g. "file: ProxyFile, use_vlm: bool".
    """
    output_schema: str
    """
    A type-definition string describing what the agent returns,
    e.g. "summary: str, pages: int".
    """
    required_input: list[str]
    """Names of fields that must be present in every request."""
    hidden: bool = False
    """
    If True, this agent is excluded from LLM tool generation
    (``build_anthropic_tools`` / ``build_openai_tools``).  The agent
    remains routable via the router ACL — only the automatic tool-schema
    construction is suppressed.  Useful for infrastructure agents such as
    llm_agent, admin frontends, and MCP bridges.
    """
    documentation_url: Optional[str] = None
    """
    Optional URL pointing to this agent's markdown documentation.
    The router fetches it on onboarding, stores it in the proxy vault,
    and exposes it as a ``documentation_file`` ProxyFile in
    ``available_destinations``.
    """


class LLMData(BaseModel):
    """
    Optional structured LLM context that can be attached to a routing
    payload when an agent wants to pass prompt-level data to a downstream
    LLM-backed agent.
    """

    agent_instruction: Optional[str] = None
    """System-level instruction for the downstream agent's LLM."""
    context: Optional[str] = None
    """Additional background context string."""
    prompt: str
    """The user-facing prompt to be processed by the downstream agent."""


class LLMCall(BaseModel):
    """
    Raw LLM inference request sent to llm_agent.

    Unlike ``LLMData`` (which carries a high-level prompt), ``LLMCall``
    carries the full messages array and tool definitions — giving the
    caller complete control over the conversation fed to the model.
    """

    messages: list[dict[str, Any]]
    """Full message array in OpenAI chat-completions format."""
    tools: list[dict[str, Any]] = []
    """Tool definitions in OpenAI function-tool format."""
    tool_choice: Optional[Any] = None
    """Tool choice constraint (e.g. "auto", "none", or {"type":"function","function":{"name":"..."}})."""
    temperature: Optional[float] = None
    """Per-call temperature override (uses model default if None)."""
    max_tokens: Optional[int] = None
    """Per-call max_tokens override (uses model default if None)."""
    model_id: Optional[str] = None
    """Model config key from llm_agent's config.json (uses "default" if None)."""


class AgentOutput(BaseModel):
    """
    Standardised return value produced by an agent at the end of its work.
    Either ``content`` or ``files`` (or both) should be populated.
    """

    content: Optional[str] = None
    """Free-form text result."""
    files: Optional[list[ProxyFile]] = None
    """Any files produced by the agent, expressed as ProxyFile references."""


# ---------------------------------------------------------------------------
# Standalone progress push (for embedded agents without RouterClient)
# ---------------------------------------------------------------------------


async def push_progress_direct(
    router_url: str,
    auth_token: str,
    task_id: str,
    event_type: str,
    content: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """
    Push a progress event directly via HTTP (for embedded agents).

    Best-effort — failures are silently ignored.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{router_url.rstrip('/')}/tasks/{task_id}/progress",
                json={
                    "type": event_type,
                    "content": content,
                    "metadata": metadata or {},
                },
                headers={"Authorization": f"Bearer {auth_token}"},
            )
    except Exception:
        _helper_logger.debug("push_progress_direct failed", exc_info=True)


# ---------------------------------------------------------------------------
# ProxyFileManager — file registry for agents
# ---------------------------------------------------------------------------


def _file_hash(path: str) -> Optional[str]:
    """Compute a short SHA-256 hash of a file for integrity checks."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except Exception:
        return None
    return h.hexdigest()[:16]


class ProxyFileManager:
    """
    Manages the translation between ProxyFile objects (router protocol) and
    local file paths (what LLMs see).

    Inbound: fetches router-proxy files to a local directory, registers the
    mapping ``local_path → (ProxyFile, content_hash)``.

    Outbound: converts local file paths back to ProxyFile objects — reusing
    the original if the content hash matches, or creating a new ``localfile``
    reference for embedded agents / ``http`` reference for external agents.

    Args:
        inbox_dir: Local directory for downloaded files.
        router_url: Router base URL (for downloading router-proxy files).
        agent_url: This agent's public URL (for serving files to
            the router via HTTP).  None for embedded agents.
        persist: If True, inbox files are not cleaned up automatically.
    """

    # Inbox GC defaults
    _DEFAULT_INBOX_MAX_AGE: float = 21600.0  # 6 hours
    _GC_MIN_INTERVAL: float = 60.0  # run at most once per 60 seconds

    def __init__(
        self,
        inbox_dir: str | Path,
        router_url: str = "http://localhost:8000",
        agent_url: Optional[str] = None,
        persist: bool = False,
        inbox_max_age: Optional[float] = None,
    ) -> None:
        self.inbox_dir = Path(inbox_dir)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.router_url = router_url.rstrip("/")
        self.agent_url = agent_url
        self.persist = persist
        self.inbox_max_age = inbox_max_age or self._DEFAULT_INBOX_MAX_AGE
        # local_path → (original ProxyFile dict, content_hash)
        self._registry: dict[str, tuple[dict[str, Any], Optional[str]]] = {}
        self._last_gc: float = 0.0

    # Class-level: key → (local_path, created_at) for file serving.
    # NOTE: This is an in-process dict — it is NOT shared across OS processes.
    # Multi-worker deployments (e.g. gunicorn with workers > 1) will break file
    # serving because a key generated in one worker is invisible to another.
    # The current architecture assumes a single-process uvicorn server.
    _serve_keys: dict[str, tuple[str, float]] = {}
    _SERVE_KEY_TTL: float = 300.0  # keys expire after 5 minutes

    # ------------------------------------------------------------------
    # Inbox garbage collection
    # ------------------------------------------------------------------

    def cleanup_inbox(self) -> int:
        """
        Delete files in the inbox directory older than ``inbox_max_age``
        seconds (by mtime).  Also removes stale entries from the registry.

        Skipped if ``persist`` is True.  Rate-limited to run at most once
        per ``_GC_MIN_INTERVAL`` seconds.

        Returns the number of files deleted.
        """
        if self.persist:
            return 0
        import time
        now = time.time()
        if now - self._last_gc < self._GC_MIN_INTERVAL:
            return 0
        self._last_gc = now

        deleted = 0
        cutoff = now - self.inbox_max_age
        try:
            for p in self.inbox_dir.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    if p.stat().st_mtime < cutoff:
                        sp = str(p)
                        p.unlink()
                        self._registry.pop(sp, None)
                        deleted += 1
                except Exception:
                    pass
        except Exception:
            pass
        if deleted:
            _helper_logger.debug("Inbox GC: deleted %d files from %s", deleted, self.inbox_dir)
        return deleted

    # ------------------------------------------------------------------
    # Inbound: fetch ProxyFile → local path
    # ------------------------------------------------------------------

    async def fetch(self, pf: dict[str, Any], task_id: str = "") -> str:
        """
        Fetch a ProxyFile to the local inbox and register the mapping.

        For ``localfile`` protocol, the path is used directly (no download).
        For ``router-proxy``, the file is downloaded via HTTP.
        For ``http``, the file is downloaded from the URL.

        Triggers inbox GC on each call (rate-limited internally).

        Returns the local file path as a string.
        """
        self.cleanup_inbox()
        protocol = pf.get("protocol", "")
        path = pf.get("path", "")
        key = pf.get("key")

        if protocol == "localfile":
            # Already local — just register it
            local_path = path
            if os.path.exists(local_path):
                h = _file_hash(local_path)
                self._registry[local_path] = (pf, h)
            return local_path

        # Download to inbox — prefer original_filename over URL-derived name
        filename = pf.get("original_filename") or Path(path.split("?")[0]).name or "file"
        dest = self.inbox_dir / filename
        # Avoid collisions
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            import uuid as _uuid
            dest = dest.with_name(f"{stem}_{_uuid.uuid4().hex[:6]}{suffix}")

        if protocol == "router-proxy":
            url = f"{self.router_url}{path}"
        elif protocol == "http":
            url = path
        else:
            raise ValueError(f"Unsupported ProxyFile protocol: {protocol}")

        params: dict[str, str] = {}
        if key and "key=" not in url:
            params["key"] = key

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", url, params=params) as r:
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        fh.write(chunk)

        local_path = str(dest)
        h = _file_hash(local_path)
        self._registry[local_path] = (pf, h)
        _helper_logger.debug("Fetched %s → %s (hash=%s)", path, local_path, h)
        return local_path

    async def fetch_all(
        self, files: list[dict[str, Any]], task_id: str = "",
    ) -> list[str]:
        """Fetch a list of ProxyFile dicts and return local paths."""
        paths: list[str] = []
        for pf in files:
            try:
                p = await self.fetch(pf, task_id)
                paths.append(p)
            except Exception as exc:
                _helper_logger.warning("Failed to fetch file %s: %s", pf.get("path"), exc)
        return paths

    def register(self, local_path: str, proxy_file: dict[str, Any]) -> None:
        """Manually register a local_path → ProxyFile mapping."""
        h = _file_hash(local_path)
        self._registry[local_path] = (proxy_file, h)

    # ------------------------------------------------------------------
    # Outbound: local path → ProxyFile
    # ------------------------------------------------------------------

    def resolve(self, local_path: str) -> Optional[dict[str, Any]]:
        """
        Convert a local file path back to a ProxyFile dict.

        If the path is in the registry and the content hash still matches,
        the original ProxyFile is reused (no re-transfer needed).

        For **embedded** agents (no ``agent_url``): returns a
        ``localfile`` ProxyFile — the router reads from the shared filesystem.

        For **external** agents (``agent_url`` set): generates a
        one-time key and returns an ``http`` ProxyFile pointing to the
        agent's file-serve endpoint.  The router fetches via HTTP and
        converts to ``router-proxy``.

        Returns None if the path cannot be resolved (e.g. path traversal
        blocked for a relative path).
        """
        # Try exact match first, then resolve relative paths
        entry = self._registry.get(local_path)
        if entry is None and not Path(local_path).is_absolute():
            # Try inbox_dir first (bare filenames), then inbox_dir.parent (prefixed paths)
            for base in [self.inbox_dir, self.inbox_dir.parent]:
                abs_candidate = str((base / local_path).resolve())
                # Containment check: resolved path must stay within the base
                base_resolved = str(base.resolve())
                if not abs_candidate.startswith(base_resolved + os.sep) and abs_candidate != base_resolved:
                    continue
                entry = self._registry.get(abs_candidate)
                if entry is not None or Path(abs_candidate).exists():
                    local_path = abs_candidate
                    break
            else:
                abs_candidate = str((self.inbox_dir / local_path).resolve())
                inbox_resolved = str(self.inbox_dir.resolve())
                if not abs_candidate.startswith(inbox_resolved + os.sep) and abs_candidate != inbox_resolved:
                    return None  # path traversal blocked
                local_path = abs_candidate
        if entry is not None:
            original_pf, original_hash = entry
            current_hash = _file_hash(local_path)
            if current_hash and current_hash == original_hash:
                return original_pf

        if self.agent_url:
            # External agent: serve via HTTP
            import secrets as _secrets
            import time as _time
            key = _secrets.token_urlsafe(32)
            ProxyFileManager._serve_keys[key] = (os.path.abspath(local_path), _time.monotonic())
            filename = Path(local_path).name
            return {
                "path": f"{self.agent_url}/files/serve?key={key}",
                "protocol": "http",
                "key": None,
                "original_filename": filename,
            }

        # Embedded agent: return localfile for router to ingest
        return {
            "path": os.path.abspath(local_path),
            "protocol": "localfile",
            "key": None,
            "original_filename": Path(local_path).name,
        }

    @classmethod
    def serve_file(cls, key: str) -> Optional[str]:
        """
        Look up a file-serve key and return the local path, or None.

        Called by the agent's ``/files/serve`` HTTP endpoint to fulfil
        router fetch requests.  Keys are single-use and expire after
        ``_SERVE_KEY_TTL`` seconds.
        """
        import time as _time
        now = _time.monotonic()
        # Prune expired keys
        expired = [k for k, (_, ts) in cls._serve_keys.items() if now - ts > cls._SERVE_KEY_TTL]
        for k in expired:
            cls._serve_keys.pop(k, None)
        entry = cls._serve_keys.pop(key, None)
        if entry is None:
            return None
        path, created_at = entry
        if now - created_at > cls._SERVE_KEY_TTL:
            return None  # expired
        return path

    # Keys that are known to carry file paths — always resolve even if
    # the path is not in the registry (fall back to localfile ProxyFile).
    _FILE_KEYS = {"file", "files", "file_path"}

    def resolve_in_args(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Scan tool-call arguments for string values that look like local file
        paths and replace them with ProxyFile dicts.

        For keys in ``_FILE_KEYS`` (file, files, file_path), **all** string
        values are converted to ProxyFile dicts — even if the file is not in
        the registry or does not exist on disk.  This ensures downstream
        agents always receive ProxyFile objects, not raw path strings.

        For other keys, only registered paths are resolved.

        If ``resolve()`` returns None (e.g. path traversal blocked), the
        original string value is kept unchanged to avoid injecting nulls
        into downstream payloads.
        """
        result = dict(arguments)
        for key, val in result.items():
            is_file_key = key in self._FILE_KEYS
            if isinstance(val, str):
                if val in self._registry or is_file_key:
                    resolved = self.resolve(val)
                    if resolved is not None:
                        result[key] = resolved
            elif isinstance(val, list) and is_file_key:
                result[key] = [
                    (self.resolve(item) or item) if isinstance(item, str) else item
                    for item in val
                ]
        return result

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_local_path(self, proxy_file_path: str) -> Optional[str]:
        """Find the local path for a given ProxyFile path, if registered."""
        for local, (pf, _) in self._registry.items():
            if pf.get("path") == proxy_file_path:
                return local
        return None

    def get_proxy_file(self, local_path: str) -> Optional[dict[str, Any]]:
        """Get the original ProxyFile dict for a local path, if registered."""
        entry = self._registry.get(local_path)
        return entry[0] if entry else None

    def list_files(self) -> dict[str, dict[str, Any]]:
        """Return all registered local_path → ProxyFile mappings."""
        return {lp: pf for lp, (pf, _) in self._registry.items()}

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> int:
        """Remove downloaded inbox files (not persistent ones). Returns count."""
        if self.persist:
            return 0
        deleted = 0
        inbox = str(self.inbox_dir)
        for local_path in list(self._registry.keys()):
            if local_path.startswith(inbox):
                try:
                    Path(local_path).unlink(missing_ok=True)
                    deleted += 1
                except Exception:
                    pass
                self._registry.pop(local_path, None)
        return deleted


# ---------------------------------------------------------------------------
# Message Builders
# ---------------------------------------------------------------------------


def build_spawn_request(
    agent_id: str,
    identifier: str,
    parent_task_id: Optional[str],
    destination_agent_id: str,
    payload: dict[str, Any],
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Build a routing payload that spawns a new task (task_id == "new").

    Args:
        agent_id: The ID of the agent issuing the spawn.
        identifier: Caller's internal tracking string; the router stores it
            and re-injects it when the result is delivered back.
        parent_task_id: ID of the parent task, or None for root-level spawns.
        destination_agent_id: The agent that should handle this new task.
        payload: Arbitrary data to forward to the destination agent.
        timestamp: Override the message timestamp (defaults to UTC now).

    Returns:
        A dict ready to POST to the router's ``/route`` endpoint.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    return {
        "agent_id": agent_id,
        "task_id": "new",
        "identifier": identifier,
        "parent_task_id": parent_task_id,
        "destination_agent_id": destination_agent_id,
        "timestamp": timestamp.isoformat(),
        "payload": payload,
    }


async def extract_result_text(
    result_data: dict[str, Any],
    pfm: "ProxyFileManager",
    task_id: str = "result",
    path_display_base: Optional[Path] = None,
) -> str:
    """
    Extract text from a sub-agent result, including any attached files.

    Reads ``payload.content`` for the text body.  If ``payload.files``
    contains ProxyFile dicts, fetches each one locally via *pfm* and
    appends a ``[Result files: ...]`` block with local paths so the
    calling LLM can reference them in subsequent tool calls.

    Args:
        result_data: The raw result dict delivered by the router.
        pfm: A ProxyFileManager instance for fetching files.
        task_id: Task ID used as the fetch context (for inbox subdirs).
        path_display_base: If set, display paths relative to this directory.

    Returns:
        A single string suitable for a ``tool`` message content.
    """
    payload = result_data.get("payload", {})
    text = payload.get("content", "") or "(no content)"

    status_code = result_data.get("status_code")
    if status_code and status_code >= 400:
        return text

    result_files = payload.get("files")
    if result_files:
        file_lines: list[str] = []
        for rf in result_files:
            try:
                lp = await pfm.fetch(rf, task_id)
                if path_display_base:
                    try:
                        rel = str(Path(lp).relative_to(path_display_base))
                        display = rel
                    except ValueError:
                        display = lp
                else:
                    display = lp
                file_lines.append(f'  - {Path(lp).name} (file: "{display}")')
            except Exception:
                pass
        if file_lines:
            text += "\n\n[Result files:\n" + "\n".join(file_lines) + "\n]"

    return text


def build_result_request(
    agent_id: str,
    task_id: str,
    parent_task_id: Optional[str],
    status_code: int,
    output: AgentOutput,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Build a routing payload that reports a task result back to the router.

    Setting ``destination_agent_id`` to ``None`` signals to the router that
    this is a result / completion message rather than a delegation or spawn.

    Args:
        agent_id: The ID of the agent reporting the result.
        task_id: The task being reported on.
        parent_task_id: Parent task ID (used for error propagation chains).
        status_code: HTTP-style status code (200 for success, 4xx/5xx for errors).
        output: The agent's structured output.
        timestamp: Override the message timestamp (defaults to UTC now).

    Returns:
        A dict ready to POST to the router's ``/route`` endpoint.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    return {
        "agent_id": agent_id,
        "task_id": task_id,
        "identifier": None,
        "parent_task_id": parent_task_id,
        "destination_agent_id": None,
        "timestamp": timestamp.isoformat(),
        "status_code": status_code,
        "payload": output.model_dump(),
    }


def build_delegation_payload(
    agent_id: str,
    task_id: str,
    parent_task_id: Optional[str],
    destination_agent_id: str,
    llmdata: Optional[LLMData] = None,
    files: Optional[list[ProxyFile]] = None,
    handoff_note: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Build a routing payload for task delegation.

    Delegation keeps the same task_id (unlike spawning) and increments the
    router's ``width_count`` for the task.

    Args:
        agent_id: The delegating agent's ID.
        task_id: The existing task being handed off.
        parent_task_id: Parent task ID for tracking.
        destination_agent_id: The agent that will take over handling.
        llmdata: Optional LLM-level prompt/context to pass along.
        files: Optional list of proxy files to include.
        handoff_note: Optional free-text note from the delegating agent.
        timestamp: Override the message timestamp (defaults to UTC now).

    Returns:
        A dict ready to POST to the router's ``/route`` endpoint.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    inner_payload: dict[str, Any] = {}
    if llmdata is not None:
        inner_payload["llmdata"] = llmdata.model_dump()
    if files is not None:
        inner_payload["files"] = [f.model_dump() for f in files]
    if handoff_note is not None:
        inner_payload["handoff_note"] = handoff_note

    return {
        "agent_id": agent_id,
        "task_id": task_id,
        "identifier": None,
        "parent_task_id": parent_task_id,
        "destination_agent_id": destination_agent_id,
        "timestamp": timestamp.isoformat(),
        "payload": inner_payload,
    }


# ---------------------------------------------------------------------------
# LLM Tool Builders
# ---------------------------------------------------------------------------

_PRIMITIVE_TYPE_MAP: dict[str, dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "bool": {"type": "boolean"},
    "float": {"type": "number"},
    "dict": {"type": "object"},
}

_COMPLEX_MODEL_SCHEMAS: dict[str, dict[str, Any]] = {
    "ProxyFile": {
        "type": "string",
        "description": "Local file path.",
    },
    "LLMData": {
        "type": "object",
        "description": "Structured LLM prompt data.",
        "properties": {
            "agent_instruction": {"type": "string"},
            "context": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["prompt"],
    },
    "AgentOutput": {
        "type": "object",
        "description": "Standardised agent output container.",
        "properties": {
            "content": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "protocol": {"type": "string"},
                        "key": {"type": "string"},
                    },
                    "required": ["path", "protocol"],
                },
            },
        },
    },
}


def _parse_param_type(type_str: str) -> dict[str, Any]:
    """
    Convert a type annotation string into a JSON Schema fragment.

    Supported forms:
    - Primitives: ``str``, ``int``, ``bool``, ``float``, ``dict``
    - Named models: ``ProxyFile``, ``LLMData``, ``AgentOutput``
    - Generic wrappers: ``List[X]``, ``Optional[X]``

    Unknown types fall back to ``{"type": "string"}``.

    Args:
        type_str: A type annotation string such as "List[ProxyFile]".

    Returns:
        A JSON Schema dict fragment.
    """
    type_str = type_str.strip()

    # Optional[X] → X (nullable)
    optional_match = re.fullmatch(r"Optional\[(.+)\]", type_str)
    if optional_match:
        inner = _parse_param_type(optional_match.group(1))
        # JSON Schema draft-07 nullable representation
        return {"oneOf": [inner, {"type": "null"}]}

    # List[X]
    list_match = re.fullmatch(r"List\[(.+)\]", type_str)
    if list_match:
        inner = _parse_param_type(list_match.group(1))
        return {"type": "array", "items": inner}

    # Primitives
    if type_str in _PRIMITIVE_TYPE_MAP:
        return _PRIMITIVE_TYPE_MAP[type_str]

    # Named model schemas
    if type_str in _COMPLEX_MODEL_SCHEMAS:
        return _COMPLEX_MODEL_SCHEMAS[type_str]

    # Unknown — fall back to string
    return {"type": "string", "description": f"Unrecognised type: {type_str}"}


def _parse_input_schema_string(schema_str: str) -> tuple[dict[str, Any], list[str]]:
    """
    Parse an agent's ``input_schema`` string into a JSON Schema properties
    dict and a required-fields list.

    The schema string format is::

        "name: type, name2: type2, name3: Optional[type3]"

    The parser splits on commas that are not inside square brackets so that
    generic types such as ``List[str, int]`` are handled correctly.

    Args:
        schema_str: A type-definition string from ``AgentInfo.input_schema``.

    Returns:
        A tuple of (properties dict, required list).
    """
    if not schema_str or not schema_str.strip():
        return {}, []

    # Split by commas that are NOT inside brackets.
    # Strategy: track bracket depth and only split at depth 0.
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in schema_str:
        if char == "[":
            depth += 1
            current.append(char)
        elif char == "]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())

    properties: dict[str, Any] = {}
    required: list[str] = []

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            # Bare name with no type — treat as string
            properties[part] = {"type": "string"}
            required.append(part)
            continue

        name, _, type_annotation = part.partition(":")
        name = name.strip()
        type_annotation = type_annotation.strip()

        schema_fragment = _parse_param_type(type_annotation)
        properties[name] = schema_fragment

        # Fields not wrapped in Optional are required
        if not re.fullmatch(r"Optional\[.+\]", type_annotation):
            required.append(name)

    return properties, required


def _visible_destinations(available_destinations: dict[str, Any]) -> dict[str, Any]:
    """Filter out hidden agents (not intended for LLM tool use)."""
    return {
        aid: info for aid, info in available_destinations.items()
        if not info.get("hidden", False)
    }


def _agents_with_docs(available_destinations: dict[str, Any]) -> list[str]:
    """Return agent IDs from available_destinations that have documentation."""
    return [
        agent_id
        for agent_id, info in available_destinations.items()
        if info.get("documentation_file") and info["documentation_file"].get("key")
    ]


def _annotate_description(description: str, has_docs: bool) -> str:
    """Append a documentation hint to a tool description when available."""
    if has_docs:
        return f"{description} [documentation available — call fetch_agent_documentation to read full usage docs before calling]"
    return description


def _build_doc_tool_anthropic(doc_agent_ids: list[str]) -> dict[str, Any]:
    """Build the Anthropic-format fetch_agent_documentation tool definition."""
    return {
        "name": "fetch_agent_documentation",
        "description": (
            "Fetch the full documentation for an agent before calling it. "
            "Use this to understand the agent's capabilities, expected inputs, "
            "and usage patterns in detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent ID to fetch documentation for.",
                    "enum": doc_agent_ids,
                },
            },
            "required": ["agent_id"],
        },
    }


def _build_doc_tool_openai(doc_agent_ids: list[str]) -> dict[str, Any]:
    """Build the OpenAI-format fetch_agent_documentation tool definition."""
    return {
        "type": "function",
        "function": {
            "name": "fetch_agent_documentation",
            "description": (
                "Fetch the full documentation for an agent before calling it. "
                "Use this to understand the agent's capabilities, expected inputs, "
                "and usage patterns in detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent ID to fetch documentation for.",
                        "enum": doc_agent_ids,
                    },
                },
                "required": ["agent_id"],
            },
        },
    }


def build_anthropic_tools(available_destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build a list of Anthropic-format tool definitions from the router's
    ``available_destinations`` map.

    Each destination agent becomes a tool named ``call_{agent_id}``.
    Agents with documentation get an annotation in their description.
    When any destination has documentation, a ``fetch_agent_documentation``
    tool is appended so the LLM can read docs before calling.

    Args:
        available_destinations: The ``available_destinations`` dict injected
            by the router into every inbound payload.  Keys are agent IDs;
            values are dicts containing at least ``description``,
            ``input_schema``, and ``required_input``.

    Returns:
        A list of tool dicts compatible with the Anthropic Messages API
        ``tools`` parameter.
    """
    visible = _visible_destinations(available_destinations)
    doc_agent_ids = _agents_with_docs(visible)
    doc_set = set(doc_agent_ids)
    tools: list[dict[str, Any]] = []

    for agent_id, info in visible.items():
        description: str = info.get("description", "")
        input_schema_str: str = info.get("input_schema", "")
        required_input: list[str] = info.get("required_input", [])

        properties, schema_required = _parse_input_schema_string(input_schema_str)

        # Honour explicit required_input from AgentInfo when present
        if required_input:
            schema_required = required_input

        tool = {
            "name": f"call_{agent_id}",
            "description": _annotate_description(description, agent_id in doc_set),
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": schema_required,
            },
        }
        tools.append(tool)

    if doc_agent_ids:
        tools.append(_build_doc_tool_anthropic(doc_agent_ids))

    return tools


def build_openai_tools(available_destinations: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build a list of OpenAI-format function tool definitions from the router's
    ``available_destinations`` map.

    Each destination agent becomes a function named ``call_{agent_id}``.
    Agents with documentation get an annotation in their description.
    When any destination has documentation, a ``fetch_agent_documentation``
    tool is appended so the LLM can read docs before calling.

    Args:
        available_destinations: The ``available_destinations`` dict injected
            by the router into every inbound payload.  Keys are agent IDs;
            values are dicts containing at least ``description``,
            ``input_schema``, and ``required_input``.

    Returns:
        A list of tool dicts compatible with the OpenAI Chat Completions API
        ``tools`` parameter (type: "function").
    """
    visible = _visible_destinations(available_destinations)
    doc_agent_ids = _agents_with_docs(visible)
    doc_set = set(doc_agent_ids)
    tools: list[dict[str, Any]] = []

    for agent_id, info in visible.items():
        description: str = info.get("description", "")
        input_schema_str: str = info.get("input_schema", "")
        required_input: list[str] = info.get("required_input", [])

        properties, schema_required = _parse_input_schema_string(input_schema_str)

        if required_input:
            schema_required = required_input

        tool = {
            "type": "function",
            "function": {
                "name": f"call_{agent_id}",
                "description": _annotate_description(description, agent_id in doc_set),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": schema_required,
                },
            },
        }
        tools.append(tool)

    if doc_agent_ids:
        tools.append(_build_doc_tool_openai(doc_agent_ids))

    return tools


async def handle_fetch_agent_documentation(
    agent_id: str,
    available_destinations: dict[str, Any],
    router_url: str = "http://localhost:8000",
) -> str:
    """
    Handle the ``fetch_agent_documentation`` tool call.

    Looks up the documentation file for an agent in available_destinations,
    fetches the content (from local file or via the router), and returns
    it as a string.  Returns a human-readable error if the agent has no
    documentation.

    This is a centralized handler that any agent with a tool-calling loop
    can call directly when the LLM invokes ``fetch_agent_documentation``.

    Args:
        agent_id: The agent whose documentation is requested.
        available_destinations: The router-injected destinations map.
        router_url: Router base URL for fetching router-proxy files.

    Returns:
        The documentation text, or an error message.
    """
    info = available_destinations.get(agent_id)
    if info is None:
        return f"Agent '{agent_id}' not found in available destinations."

    doc_file = info.get("documentation_file")
    if doc_file is None or not doc_file.get("key"):
        return f"No documentation available for agent '{agent_id}'."

    path = doc_file.get("path", "")
    protocol = doc_file.get("protocol", "router-proxy")
    key = doc_file.get("key", "")

    try:
        if protocol == "localfile":
            return Path(path).read_text(encoding="utf-8")

        if protocol == "router-proxy":
            url = f"{router_url.rstrip('/')}{path}"
        elif protocol == "http":
            url = path
        else:
            return f"Unsupported documentation protocol: {protocol}"

        params: dict[str, str] = {}
        if key and "key=" not in url:
            params["key"] = key
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.text
    except Exception as exc:
        return f"Error fetching documentation for '{agent_id}': {exc}"


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------


class OnboardRequest(BaseModel):
    """
    Request body sent by an external agent to self-register with the router.
    """

    invitation_token: str
    """Single-use token obtained from the router admin."""
    endpoint_url: str
    """The URL at which this agent listens for POST requests from the router."""
    agent_info: AgentInfo
    """Capability metadata for this agent."""


class OnboardResponse(BaseModel):
    """
    Response returned by the router after successful agent registration.
    """

    agent_id: str
    auth_token: str
    """Bearer token the agent must include in all subsequent requests."""
    inbound_groups: list[str]
    outbound_groups: list[str]
    available_destinations: dict[str, Any]
    """ACL-filtered map of agents this newly registered agent may contact."""


async def onboard(
    router_url: str,
    invitation_token: str,
    endpoint_url: str,
    agent_info: AgentInfo,
) -> OnboardResponse:
    """
    Register an external agent with the router using a one-time invitation token.

    Args:
        router_url: Base URL of the router (e.g. "http://localhost:8000").
        invitation_token: Single-use token issued by a router admin.
        endpoint_url: The URL at which this agent will receive router POSTs.
        agent_info: Capability metadata to publish for this agent.

    Returns:
        An ``OnboardResponse`` containing the assigned agent_id, auth_token,
        group memberships, and the initial available_destinations map.

    Raises:
        httpx.HTTPStatusError: If the router returns a non-2xx response.
    """
    request = OnboardRequest(
        invitation_token=invitation_token,
        endpoint_url=endpoint_url,
        agent_info=agent_info,
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{router_url.rstrip('/')}/onboard",
            json=request.model_dump(),
        )
        response.raise_for_status()
        return OnboardResponse.model_validate(response.json())


# ---------------------------------------------------------------------------
# RouterClient
# ---------------------------------------------------------------------------


class RouterClient:
    """
    Convenience client for agents interacting with the central router.

    Wraps the low-level HTTP calls behind typed methods so agent code stays
    clean.  All methods are async and share a single ``httpx.AsyncClient``
    per instance.

    Args:
        router_url: Base URL of the router (e.g. "http://localhost:8000").
        agent_id: This agent's registered ID.
        auth_token: Bearer token issued by the router during registration.
    """

    def __init__(self, router_url: str, agent_id: str, auth_token: str) -> None:
        self.router_url = router_url.rstrip("/")
        self.agent_id = agent_id
        self.auth_token = auth_token
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=30.0,
        )

    # ------------------------------------------------------------------
    # Core routing
    # ------------------------------------------------------------------

    async def route(self, payload: dict[str, Any]) -> httpx.Response:
        """
        POST an arbitrary routing payload to the router's ``/route`` endpoint.

        The caller is responsible for constructing a well-formed payload (use
        the ``build_*`` helpers above).  ``agent_id`` is injected automatically
        from the client's configuration.

        Args:
            payload: A routing payload dict.

        Returns:
            The raw ``httpx.Response`` from the router.
        """
        payload = dict(payload)
        payload["agent_id"] = self.agent_id
        response = await self._client.post(f"{self.router_url}/route", json=payload)
        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    async def spawn(
        self,
        identifier: str,
        parent_task_id: Optional[str],
        destination_agent_id: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        """
        Spawn a new task targeting ``destination_agent_id``.

        Args:
            identifier: Internal tracking string; re-injected by the router
                when the result is delivered back to this agent.
            parent_task_id: Parent task ID, or None for root-level spawns.
            destination_agent_id: The agent that should handle the new task.
            payload: Task data forwarded to the destination agent.

        Returns:
            The raw ``httpx.Response`` from the router.
        """
        body = build_spawn_request(
            agent_id=self.agent_id,
            identifier=identifier,
            parent_task_id=parent_task_id,
            destination_agent_id=destination_agent_id,
            payload=payload,
        )
        return await self.route(body)

    async def report_result(
        self,
        task_id: str,
        parent_task_id: Optional[str],
        status_code: int,
        output: AgentOutput,
    ) -> httpx.Response:
        """
        Report a task result (or failure) back to the router.

        Args:
            task_id: The task being reported on.
            parent_task_id: Parent task ID.
            status_code: HTTP-style status (200 success, 4xx/5xx errors).
            output: The structured result to deliver to the origin agent.

        Returns:
            The raw ``httpx.Response`` from the router.
        """
        body = build_result_request(
            agent_id=self.agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=status_code,
            output=output,
        )
        return await self.route(body)

    async def delegate(
        self,
        task_id: str,
        parent_task_id: Optional[str],
        destination_agent_id: str,
        llmdata: Optional[LLMData] = None,
        files: Optional[list[ProxyFile]] = None,
        handoff_note: Optional[str] = None,
    ) -> httpx.Response:
        """
        Delegate the current task to another agent.

        Delegation preserves the task_id and increments the router's
        width_count.

        Args:
            task_id: The existing task to hand off.
            parent_task_id: Parent task ID.
            destination_agent_id: The agent taking over.
            llmdata: Optional LLM prompt / context data.
            files: Optional proxy files to include.
            handoff_note: Optional plain-text note for the receiving agent.

        Returns:
            The raw ``httpx.Response`` from the router.
        """
        body = build_delegation_payload(
            agent_id=self.agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            destination_agent_id=destination_agent_id,
            llmdata=llmdata,
            files=files,
            handoff_note=handoff_note,
        )
        return await self.route(body)

    # ------------------------------------------------------------------
    # Agent info refresh
    # ------------------------------------------------------------------

    async def refresh_agent_info(
        self,
        description: Optional[str] = None,
        input_schema: Optional[str] = None,
        output_schema: Optional[str] = None,
        required_input: Optional[list[str]] = None,
        documentation_url: Optional[str] = None,
        endpoint_url: Optional[str] = None,
    ) -> httpx.Response:
        """
        Update this agent's AgentInfo on the router.

        Only non-None fields are merged into the existing info.
        If ``endpoint_url`` is provided, the agent's registered endpoint
        is also updated (useful when the port changes between restarts).

        Returns:
            The raw ``httpx.Response`` from the router.

        Raises:
            httpx.HTTPStatusError: If the router returns a non-2xx response.
        """
        body: dict[str, Any] = {"agent_id": self.agent_id}
        if description is not None:
            body["description"] = description
        if input_schema is not None:
            body["input_schema"] = input_schema
        if output_schema is not None:
            body["output_schema"] = output_schema
        if required_input is not None:
            body["required_input"] = required_input
        if documentation_url is not None:
            body["documentation_url"] = documentation_url
        if endpoint_url is not None:
            body["endpoint_url"] = endpoint_url
        resp = await self._client.put(f"{self.router_url}/agent-info", json=body)
        resp.raise_for_status()
        return resp

    async def refresh_from_agent_info(
        self, info: "AgentInfo", endpoint_url: Optional[str] = None,
    ) -> httpx.Response:
        """Update this agent's info on the router from an AgentInfo object."""
        return await self.refresh_agent_info(
            description=info.description,
            input_schema=info.input_schema,
            output_schema=info.output_schema,
            required_input=info.required_input,
            documentation_url=info.documentation_url,
            endpoint_url=endpoint_url,
        )

    # ------------------------------------------------------------------
    # Destinations
    # ------------------------------------------------------------------

    async def get_destinations(self) -> dict[str, Any]:
        """
        Fetch this agent's ACL-filtered available destinations from the router.

        Returns:
            A dict with ``agent_id``, ``status``, and
            ``available_destinations``.

        Raises:
            httpx.HTTPStatusError: If the router returns a non-2xx response.
        """
        resp = await self._client.get(f"{self.router_url}/agent/destinations")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Progress events
    # ------------------------------------------------------------------

    async def push_progress(
        self,
        task_id: str,
        event_type: str,
        content: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Push a progress event for a task.

        Args:
            task_id: The task this event belongs to.
            event_type: One of "thinking", "tool_call", "tool_result",
                "status", "chunk", "done".
            content: Human-readable event content.
            metadata: Optional extra data (e.g. tool name, arguments).
        """
        try:
            await self._client.post(
                f"{self.router_url}/tasks/{task_id}/progress",
                json={
                    "type": event_type,
                    "content": content,
                    "metadata": metadata or {},
                },
            )
        except Exception:
            pass  # Progress events are best-effort

    async def subscribe_progress(
        self,
        task_id: str,
    ) -> "AsyncGenerator[dict[str, Any], None]":
        """
        Subscribe to progress events for a task via SSE.

        Yields event dicts as they arrive.  The iterator ends when a
        ``done`` event is received or the connection times out.
        """

        async with self._client.stream(
            "GET",
            f"{self.router_url}/tasks/{task_id}/progress",
            timeout=360.0,
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except (json.JSONDecodeError, ValueError):
                    continue
                yield event
                if event.get("type") == "done":
                    return

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client and release connections."""
        await self._client.aclose()

    async def __aenter__(self) -> "RouterClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()


# ---------------------------------------------------------------------------
# Image resize + base64 encoding (shared by core_personal_agent & coding_agent)
# ---------------------------------------------------------------------------

_IMAGE_MAX_LONG_SIDE = 1568


def resize_and_encode_image(file_path: str) -> tuple[str, str]:
    """Read an image, resize if needed, and return (mime_type, base64_data).

    If the longest side exceeds ``_IMAGE_MAX_LONG_SIDE`` (1568 px — Claude's
    tile boundary, a good cross-model default), the image is resized
    proportionally.  Photos are re-encoded as JPEG quality 85 for smaller
    base64; images with transparency are kept as PNG.

    Returns the MIME type and the base64-encoded string.
    """
    import base64 as _b64
    import io as _io
    import mimetypes as _mt

    from PIL import Image

    mime = _mt.guess_type(file_path)[0] or "image/png"
    if not mime.startswith("image/"):
        mime = "image/png"

    img = Image.open(file_path)

    needs_resize = max(img.size) > _IMAGE_MAX_LONG_SIDE
    has_alpha = img.mode in ("RGBA", "LA", "PA")

    if needs_resize:
        img.thumbnail((_IMAGE_MAX_LONG_SIDE, _IMAGE_MAX_LONG_SIDE), Image.LANCZOS)

    if has_alpha:
        out_format = "PNG"
        out_mime = "image/png"
    else:
        out_format = "JPEG"
        out_mime = "image/jpeg"
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

    if not needs_resize and out_mime == mime:
        # No resize, same format — use original bytes (skip re-encode).
        raw = Path(file_path).read_bytes()
    else:
        buf = _io.BytesIO()
        save_kwargs: dict[str, Any] = {"format": out_format}
        if out_format == "JPEG":
            save_kwargs["quality"] = 85
        img.save(buf, **save_kwargs)
        raw = buf.getvalue()

    return out_mime, _b64.b64encode(raw).decode("ascii")
