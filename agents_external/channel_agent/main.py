"""
channel_agent/main.py — Channel inbound agent (external, always-running).

Bridges Telegram and Discord to the router's core personal agent.
Maintains per-user session state, routes messages through the router,
and delivers responses back to the originating chat.

Run:
    cd channel_agent
    uvicorn main:app --host 0.0.0.0 --port 8081

Environment: channel_agent/.env
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import sys
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from helper import AgentInfo, AgentOutput, OnboardResponse, PasswordFile, build_result_request, build_spawn_request, onboard

load_dotenv(Path(__file__).parent / "data" / ".env")

import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST: str = os.environ.get("AGENT_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("AGENT_PORT", "8081"))
ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    import warnings as _w
    _w.warn("ADMIN_PASSWORD is not set — web UI login will be unavailable until configured", stacklevel=1)
SESSION_SECRET: str = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000").rstrip("/")
INVITATION_TOKEN: str = os.environ.get("INVITATION_TOKEN", "")
AGENT_ENDPOINT_URL: str = os.environ.get("AGENT_ENDPOINT_URL", f"http://localhost:{PORT}")
RECEIVE_URL: str = os.environ.get("RECEIVE_URL", f"{AGENT_ENDPOINT_URL}/receive")

TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")
DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")

# Runtime settings from data/config.json (hot-reloadable)
_CHAN_CONFIG_PATH = Path(__file__).parent / "data" / "config.json"

def _load_chan_config() -> dict:
    try:
        return json.loads(_CHAN_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

_chan_cfg = _load_chan_config()
CORE_AGENT_ID: str = _chan_cfg.get("CORE_AGENT_ID") or os.environ.get("CORE_AGENT_ID", "core_personal_agent")
DATA_DIR: Path = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
SESSIONS_FILE: Path = DATA_DIR / "sessions.json"
CREDENTIALS_FILE: Path = DATA_DIR / "credentials.json"
INVITATION_TOKENS_FILE: Path = DATA_DIR / "invitation_tokens.json"
RATE_LIMITS_FILE: Path = DATA_DIR / "rate_limits.json"

RATE_LIMIT_WINDOW: int = int(_chan_cfg.get("RATE_LIMIT_WINDOW") or os.environ.get("RATE_LIMIT_WINDOW", "3600"))
RATE_LIMIT_MAX_TRIALS: int = int(_chan_cfg.get("RATE_LIMIT_MAX_TRIALS") or os.environ.get("RATE_LIMIT_MAX_TRIALS", "5"))
LOG_CAPACITY: int = int(os.environ.get("LOG_CAPACITY", "500"))
FILE_MAX_AGE: int = int(os.environ.get("FILE_MAX_AGE", "3600"))  # seconds, default 1 hour

DATA_DIR.mkdir(parents=True, exist_ok=True)

_admin_pw = PasswordFile(DATA_DIR / "admin_password.json", ADMIN_PASSWORD)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("channel_agent")

# Suppress noisy httpx logs (Telegram polling, etc.)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Ring buffer for the WebUI log view
_log_ring: deque[str] = deque(maxlen=LOG_CAPACITY)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_ring.append(self.format(record))


_ring_handler = _RingHandler()
_ring_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger("channel_agent").addHandler(_ring_handler)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_data_lock = asyncio.Lock()

# Router credentials
_agent_id: Optional[str] = None
_auth_token: Optional[str] = None
_http_client: Optional[httpx.AsyncClient] = None

# Direct-chat WebUI: identifier → (Event, result_str)
_dc_events: dict[str, asyncio.Event] = {}
_dc_results: dict[str, str] = {}

# Pending removal: session_ids that should be cleaned up after result delivery
# (set after /new so the old session is purged once its result arrives)
_pending_removal: set[str] = set()

# Telegram Application (set during startup)
_tg_app: Any = None

# Discord HTTP client + bot user id
_discord_http: Optional[httpx.AsyncClient] = None
_discord_bot_id: Optional[str] = None

# ---------------------------------------------------------------------------
# Sessions data management
# ---------------------------------------------------------------------------
# File structure:
# {
#   "user_mappings": {"platform:platform_user_id": "user_id"},
#   "sessions":      {"platform:chat_id:platform_user_id": "session_id"},
#   "session_details": {
#       "session_id": {user_id, platform, chat_id, platform_user_id, created_at}
#   }
# }

_EMPTY_DATA: dict[str, Any] = {
    "user_mappings": {},
    "sessions": {},
    "session_details": {},
    "core_agent_map": {},
    "verbose_users": [],
}


def _load_data() -> dict[str, Any]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    import copy
    return copy.deepcopy(_EMPTY_DATA)


def _save_data(data: dict[str, Any]) -> None:
    SESSIONS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _session_lookup_key(platform: str, chat_id: str, platform_user_id: str) -> str:
    return f"{platform}:{chat_id}:{platform_user_id}"


def _user_mapping_key(platform: str, platform_user_id: str) -> str:
    return f"{platform}:{platform_user_id}"


def _get_core_agent(data: dict[str, Any], user_id: str) -> str:
    """Look up the per-user core agent, falling back to the global default."""
    return data.get("core_agent_map", {}).get(user_id, CORE_AGENT_ID)


def _is_verbose_user(user_id: str) -> bool:
    """Check if a user has verbose mode enabled."""
    data = _load_data()
    return user_id in data.get("verbose_users", [])


# ---------------------------------------------------------------------------
# Invitation token management
# ---------------------------------------------------------------------------

def _load_tokens_data() -> dict[str, Any]:
    if INVITATION_TOKENS_FILE.exists():
        try:
            return json.loads(INVITATION_TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"tokens": {}}


def _save_tokens_data(data: dict[str, Any]) -> None:
    INVITATION_TOKENS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _create_invitation_token(user_id: str, ttl: int, config: Optional[dict] = None) -> str:
    """Generate a new invitation token and persist it."""
    token = secrets.token_urlsafe(32)
    td = _load_tokens_data()
    td["tokens"][token] = {
        "user_id": user_id,
        "config": config or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ttl": ttl,
    }
    _save_tokens_data(td)
    return token


def _get_valid_tokens() -> list[dict[str, Any]]:
    """Return non-expired tokens with their token string included."""
    td = _load_tokens_data()
    now = datetime.now(timezone.utc)
    result = []
    for tok, info in td["tokens"].items():
        created = datetime.fromisoformat(info["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (now - created).total_seconds() < info.get("ttl", 86400):
            result.append({"token": tok, **info})
    return result


def _consume_token(token_string: str) -> Optional[dict[str, Any]]:
    """Validate and remove an invitation token. Returns token data or None."""
    td = _load_tokens_data()
    info = td["tokens"].get(token_string)
    if not info:
        return None
    created = datetime.fromisoformat(info["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if (now - created).total_seconds() >= info.get("ttl", 86400):
        # Expired — clean up
        del td["tokens"][token_string]
        _save_tokens_data(td)
        return None
    # Valid — consume
    del td["tokens"][token_string]
    _save_tokens_data(td)
    return info


def _delete_invitation_token(token_string: str) -> bool:
    """Remove a token manually. Returns True if it existed."""
    td = _load_tokens_data()
    if token_string in td["tokens"]:
        del td["tokens"][token_string]
        _save_tokens_data(td)
        return True
    return False


# ---------------------------------------------------------------------------
# Rate limiting for unregistered users
# ---------------------------------------------------------------------------

def _check_rate_limit(platform: str, platform_user_id: str) -> bool:
    """
    Check and update rate limit for an unregistered user.
    Returns True if the user is BLOCKED (exceeded limit).
    """
    key = f"{platform}:{platform_user_id}"
    now = datetime.now(timezone.utc)

    # Load
    rl_data: dict[str, Any] = {}
    if RATE_LIMITS_FILE.exists():
        try:
            rl_data = json.loads(RATE_LIMITS_FILE.read_text(encoding="utf-8"))
        except Exception:
            rl_data = {}

    entry = rl_data.get(key)
    if entry:
        last_trial = datetime.fromisoformat(entry["last_trial"])
        if last_trial.tzinfo is None:
            last_trial = last_trial.replace(tzinfo=timezone.utc)
        if (now - last_trial).total_seconds() > RATE_LIMIT_WINDOW:
            # Window expired — reset
            rl_data[key] = {"count": 1, "last_trial": now.isoformat()}
        else:
            entry["count"] += 1
            entry["last_trial"] = now.isoformat()
            if entry["count"] > RATE_LIMIT_MAX_TRIALS:
                RATE_LIMITS_FILE.write_text(
                    json.dumps(rl_data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                return True
    else:
        rl_data[key] = {"count": 1, "last_trial": now.isoformat()}

    RATE_LIMITS_FILE.write_text(
        json.dumps(rl_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return False


async def _stream_progress_to_user(
    task_id: str,
    platform: str,
    chat_id: str,
) -> None:
    """
    Subscribe to progress events for a task and relay them to the user's chat.

    Runs as a background task. Ends when the task completes or times out.
    """
    if not _http_client or not _agent_id:
        return

    url = f"{ROUTER_URL}/tasks/{task_id}/progress"
    headers = {"Authorization": f"Bearer {_auth_token}"}

    _EMOJI = {
        "thinking": "\U0001f4ad",   # 💭
        "tool_call": "\U0001f527",  # 🔧
        "tool_result": "\u2705",    # ✅
        "status": "\u2139\ufe0f",   # ℹ️
        "chunk": "",
    }

    try:
        async with httpx.AsyncClient(timeout=360.0) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except (json.JSONDecodeError, ValueError):
                        continue

                    etype = event.get("type", "")
                    if etype == "done":
                        return
                    if etype == "chunk":
                        # Final response — don't duplicate, it comes via normal result delivery
                        continue

                    content = event.get("content", "")
                    if not content:
                        continue

                    emoji = _EMOJI.get(etype, "")
                    status_msg = f"{emoji} {content}".strip() if emoji else content

                    # Send to the user's chat platform
                    if platform == "telegram":
                        await _send_telegram(chat_id, status_msg)
                    elif platform == "discord":
                        await _send_discord(chat_id, status_msg)

    except Exception as exc:
        logger.debug("Progress stream for task %s ended: %s", task_id, exc)


async def _get_user_id(platform: str, platform_user_id: str) -> Optional[str]:
    async with _data_lock:
        data = _load_data()
        return data["user_mappings"].get(_user_mapping_key(platform, platform_user_id))


async def _get_or_create_session(
    platform: str, chat_id: str, platform_user_id: str, user_id: str
) -> str:
    """Return the active session_id, creating one if needed."""
    async with _data_lock:
        data = _load_data()
        key = _session_lookup_key(platform, chat_id, platform_user_id)
        session_id = data["sessions"].get(key)
        if session_id and session_id in data["session_details"]:
            return session_id
        # Create new session
        session_id = secrets.token_hex(16)
        data["sessions"][key] = session_id
        data["session_details"][session_id] = {
            "user_id": user_id,
            "platform": platform,
            "chat_id": chat_id,
            "platform_user_id": platform_user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_data(data)
        logger.info("New session %s for user %s (%s)", session_id, user_id, platform)
        return session_id


async def _rotate_session(
    platform: str, chat_id: str, platform_user_id: str, user_id: str
) -> tuple[str, str]:
    """
    Create a new session for the user, keeping the old session_details
    alive until its result arrives (so the router result can be routed back).
    Returns (old_session_id, new_session_id).
    """
    async with _data_lock:
        data = _load_data()
        key = _session_lookup_key(platform, chat_id, platform_user_id)
        old_session_id = data["sessions"].get(key, "")
        new_session_id = secrets.token_hex(16)
        data["sessions"][key] = new_session_id
        data["session_details"][new_session_id] = {
            "user_id": user_id,
            "platform": platform,
            "chat_id": chat_id,
            "platform_user_id": platform_user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_data(data)
    return old_session_id, new_session_id


async def _remove_session_details(session_id: str) -> None:
    async with _data_lock:
        data = _load_data()
        data["session_details"].pop(session_id, None)
        # Also clean up any lookup key pointing to this session
        data["sessions"] = {
            k: v for k, v in data["sessions"].items() if v != session_id
        }
        _save_data(data)

# ---------------------------------------------------------------------------
# Router credentials management
# ---------------------------------------------------------------------------


def _load_credentials() -> Optional[dict]:
    if CREDENTIALS_FILE.exists():
        try:
            return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_credentials(agent_id: str, auth_token: str) -> None:
    CREDENTIALS_FILE.write_text(
        json.dumps({"agent_id": agent_id, "auth_token": auth_token}),
        encoding="utf-8",
    )


async def _ensure_registered() -> None:
    global _agent_id, _auth_token, _http_client

    creds = _load_credentials()
    if creds:
        # Verify still registered using the agent's own auth token.
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{ROUTER_URL}/agent/destinations",
                    headers={"Authorization": f"Bearer {creds['auth_token']}"},
                )
                if r.status_code == 200:
                    _agent_id = creds["agent_id"]
                    _auth_token = creds["auth_token"]
                    if _http_client:
                        await _http_client.aclose()
                    _http_client = httpx.AsyncClient(
                        headers={"Authorization": f"Bearer {_auth_token}"},
                        timeout=120.0,
                    )
                    logger.info("Router credentials reloaded for %s", _agent_id)
                    return
                # 401/403 means credentials are invalid — fall through to re-onboard.
        except Exception as exc:
            # Router unreachable — trust saved credentials rather than attempting re-onboard.
            logger.warning("Could not verify router credentials: %s — using saved credentials", exc)
            _agent_id = creds["agent_id"]
            _auth_token = creds["auth_token"]
            _http_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {_auth_token}"},
                timeout=120.0,
            )
            return

    if not INVITATION_TOKEN:
        logger.error(
            "INVITATION_TOKEN not set and no valid saved credentials — "
            "agent will run without router connection."
        )
        return

    agent_info = AgentInfo(
        agent_id="channel_inbound",
        description=(
            "Channel inbound agent. Bridges Telegram and Discord to the router. "
            "Calling this agent sends a direct message to the user on their active "
            "chat platform (Telegram or Discord). Requires user_id and session_id "
            "to identify the recipient."
        ),
        input_schema="user_id: str, session_id: str, message: str",
        output_schema="content: str",
        required_input=["user_id", "session_id", "message"],
    )
    try:
        resp: OnboardResponse = await onboard(
            router_url=ROUTER_URL,
            invitation_token=INVITATION_TOKEN,
            endpoint_url=RECEIVE_URL,
            agent_info=agent_info,
        )
        _agent_id = resp.agent_id
        _auth_token = resp.auth_token
        _save_credentials(_agent_id, _auth_token)
        _http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {_auth_token}"},
            timeout=120.0,
        )
        logger.info("Registered with router as %s", _agent_id)
    except Exception as exc:
        logger.error("Router onboarding failed: %s", exc)


# ---------------------------------------------------------------------------
# File upload helper
# ---------------------------------------------------------------------------


_LOCAL_FILES_DIR = DATA_DIR / "files"
_LOCAL_FILES_DIR.mkdir(parents=True, exist_ok=True)

# Access keys for locally stored files: filename -> key
_local_file_keys: dict[str, str] = {}


def _save_file_locally(file_bytes: bytes, filename: str) -> Optional[dict[str, Any]]:
    """
    Save a file to local storage and return a ProxyFile-shaped dict.

    The file is served via GET /files/{filename}?key=... so that downstream
    agents (and the router) can fetch it over HTTP.
    """
    # Ensure unique filename to avoid collisions.
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"

    dest = _LOCAL_FILES_DIR / unique_name
    try:
        dest.write_bytes(file_bytes)
    except Exception as exc:
        logger.error("Failed to save file '%s' locally: %s", filename, exc)
        return None

    key = secrets.token_urlsafe(32)
    _local_file_keys[unique_name] = key

    # Build the URL other agents will use to fetch this file.
    base_url = AGENT_ENDPOINT_URL
    file_url = f"{base_url}/files/{unique_name}?key={key}"

    logger.info("Saved file '%s' locally → %s", filename, unique_name)
    return {
        "path": file_url,
        "protocol": "http",
        "key": key,
        "original_filename": filename,
    }


def _cleanup_old_files() -> int:
    """Delete local files older than FILE_MAX_AGE seconds. Returns count deleted."""
    import time
    now = time.time()
    deleted = 0
    if not _LOCAL_FILES_DIR.exists():
        return 0
    for f in _LOCAL_FILES_DIR.iterdir():
        if not f.is_file():
            continue
        try:
            age = now - f.stat().st_mtime
            if age > FILE_MAX_AGE:
                f.unlink()
                _local_file_keys.pop(f.name, None)
                deleted += 1
        except Exception:
            pass
    return deleted


async def _file_gc_loop() -> None:
    """Background loop that cleans up expired local files."""
    # On startup, purge all orphaned files from previous runs
    # (keys are in-memory only, so old files are unreachable).
    _cleanup_old_files()

    while True:
        await asyncio.sleep(min(FILE_MAX_AGE, 600))  # check at most every 10 min
        deleted = _cleanup_old_files()
        if deleted:
            logger.info("File GC: deleted %d expired file(s)", deleted)


# ---------------------------------------------------------------------------
# Spawn helper
# ---------------------------------------------------------------------------

async def _spawn_to_core(
    identifier: str,
    user_id: str,
    session_id: str,
    message: str,
    files: Optional[list[dict[str, Any]]] = None,
    core_agent_id: Optional[str] = None,
) -> Optional[str]:
    """
    POST a spawn request to the router targeting the user's core agent.
    identifier is what the router re-injects when delivering the result.
    Returns the task_id string or None on failure.
    """
    if not _http_client or not _agent_id:
        logger.warning("Router not connected; dropping message for session %s", identifier)
        return None

    target_agent = core_agent_id or CORE_AGENT_ID

    payload: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "message": message,
    }
    if files:
        payload["files"] = files

    body = build_spawn_request(
        agent_id=_agent_id,
        identifier=identifier,
        parent_task_id=None,
        destination_agent_id=target_agent,
        payload=payload,
    )
    try:
        r = await _http_client.post(f"{ROUTER_URL}/route", json=body)
        r.raise_for_status()
        return r.json().get("task_id")
    except Exception as exc:
        logger.error("Failed to spawn task to core agent: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Platform send helpers
# ---------------------------------------------------------------------------

_TG_MAX = 4000
_DC_MAX = 2000


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


def _md_to_tg_html(text: str) -> str:
    """Convert basic Markdown to Telegram HTML (best-effort)."""
    # Protect code blocks first
    code_blocks: list[str] = []

    def _save_cb(m: re.Match) -> str:
        inner = m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        code_blocks.append(inner)
        return f"\x00CB{len(code_blocks)-1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", _save_cb, text)

    # Inline code
    inline: list[str] = []

    def _save_ic(m: re.Match) -> str:
        inner = m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        inline.append(inner)
        return f"\x00IC{len(inline)-1}\x00"

    text = re.sub(r"`([^`]+)`", _save_ic, text)

    # Strip headers
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # HTML escape
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    # Bullets
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Restore inline code / blocks
    for i, code in enumerate(inline):
        text = text.replace(f"\x00IC{i}\x00", f"<code>{code}</code>")
    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{code}</code></pre>")

    return text


async def _send_telegram(chat_id: str, text: str) -> None:
    if not _tg_app:
        return
    try:
        cid = int(chat_id)
    except ValueError:
        logger.error("Invalid Telegram chat_id: %s", chat_id)
        return
    for chunk in _split_text(text, _TG_MAX):
        try:
            html = _md_to_tg_html(chunk)
            await _tg_app.bot.send_message(chat_id=cid, text=html, parse_mode="HTML")
        except Exception:
            try:
                await _tg_app.bot.send_message(chat_id=cid, text=chunk)
            except Exception as e:
                logger.error("Failed to send Telegram message: %s", e)


async def _send_discord(channel_id: str, text: str) -> None:
    if not _discord_http:
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    for chunk in _split_text(text, _DC_MAX):
        for attempt in range(3):
            try:
                r = await _discord_http.post(url, headers=headers, json={"content": chunk})
                if r.status_code == 429:
                    await asyncio.sleep(float(r.json().get("retry_after", 1.0)))
                    continue
                r.raise_for_status()
                break
            except Exception as e:
                if attempt == 2:
                    logger.error("Failed to send Discord message: %s", e)
                else:
                    await asyncio.sleep(1)


async def _send_file_telegram(chat_id: str, file_path: str, filename: str) -> None:
    """Send a file as a Telegram document."""
    if not _tg_app:
        return
    try:
        cid = int(chat_id)
        with open(file_path, "rb") as f:
            await _tg_app.bot.send_document(chat_id=cid, document=f, filename=filename)
    except Exception as e:
        logger.error("Failed to send Telegram file '%s': %s", filename, e)


async def _send_file_discord(channel_id: str, file_path: str, filename: str) -> None:
    """Send a file as a Discord attachment."""
    if not _discord_http:
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    try:
        with open(file_path, "rb") as f:
            r = await _discord_http.post(
                url, headers=headers,
                files={"files[0]": (filename, f)},
            )
            r.raise_for_status()
    except Exception as e:
        logger.error("Failed to send Discord file '%s': %s", filename, e)


async def _send_to_chat(
    platform: str, chat_id: str, text: str,
    files: Optional[list[dict[str, Any]]] = None,
) -> None:
    if platform == "telegram":
        await _send_telegram(chat_id, text)
    elif platform == "discord":
        await _send_discord(chat_id, text)

    if files:
        for pf in files:
            # Download router-proxy file to local temp
            file_url = pf.get("path", "")
            key = pf.get("key")
            protocol = pf.get("protocol", "")
            filename = pf.get("original_filename") or Path(file_url.split("?")[0]).name or "file"

            try:
                if protocol == "router-proxy":
                    dl_url = f"{ROUTER_URL}{file_url}"
                    params = {"key": key} if key else {}
                elif protocol == "http":
                    dl_url = file_url
                    params = {}
                else:
                    logger.warning("Unsupported file protocol '%s' for chat delivery", protocol)
                    continue

                async with httpx.AsyncClient(timeout=60.0) as dl:
                    resp = await dl.get(dl_url, params=params)
                    resp.raise_for_status()

                # Save to temp file
                tmp_path = _LOCAL_FILES_DIR / f"dl_{uuid.uuid4().hex[:8]}_{filename}"
                tmp_path.write_bytes(resp.content)

                if platform == "telegram":
                    await _send_file_telegram(chat_id, str(tmp_path), filename)
                elif platform == "discord":
                    await _send_file_discord(chat_id, str(tmp_path), filename)

                # Clean up temp
                tmp_path.unlink(missing_ok=True)
            except Exception as e:
                logger.error("Failed to deliver file '%s' to %s chat: %s", filename, platform, e)


# Track active typing indicators: session_id → asyncio.Task
_typing_tasks: dict[str, asyncio.Task] = {}


async def _send_typing_loop(platform: str, chat_id: str) -> None:
    """Send typing indicator every 5 seconds until cancelled."""
    try:
        while True:
            if platform == "telegram" and TELEGRAM_TOKEN:
                try:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        await c.post(url, json={"chat_id": chat_id, "action": "typing"})
                except Exception:
                    pass
            # Discord doesn't have a persistent typing indicator API that's
            # easy to call externally, so we skip it.
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Incoming message dispatcher (shared by both platforms)
# ---------------------------------------------------------------------------

async def _handle_incoming(
    platform: str,
    chat_id: str,
    platform_user_id: str,
    text: str,
    files: Optional[list[dict[str, Any]]] = None,
) -> None:
    """
    Central handler for all inbound messages from any platform.

    Resolves user_id from mapping, manages session, and spawns task to core.
    """
    text = text.strip()
    if not text and not files:
        return
    if not text:
        text = "(file attached)"

    # --- Slash command pre-parse ---
    cmd = None
    parts: list[str] = []
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()  # strip @botname suffix

    user_id = await _get_user_id(platform, platform_user_id)

    # --- /register: available to unregistered users ---
    if cmd == "register":
        if user_id:
            await _send_to_chat(
                platform, chat_id,
                f"You are already registered as {user_id}. Use /config to review your configuration.",
            )
            return
        # Rate limit check
        if _check_rate_limit(platform, platform_user_id):
            await _send_to_chat(
                platform, chat_id,
                "Too many attempts. Please try again later.",
            )
            return
        token_str = parts[1].strip() if len(parts) > 1 else ""
        if not token_str:
            await _send_to_chat(
                platform, chat_id,
                "Usage: /register <invitation_token>",
            )
            return
        token_info = _consume_token(token_str)
        if not token_info:
            await _send_to_chat(
                platform, chat_id,
                "Invalid or expired invitation token.",
            )
            return
        # Register the user
        new_user_id = token_info["user_id"]
        mapping_key = _user_mapping_key(platform, platform_user_id)
        async with _data_lock:
            data = _load_data()
            data["user_mappings"][mapping_key] = new_user_id
            _save_data(data)
        logger.info("Registered user %s via invitation token (%s:%s)", new_user_id, platform, platform_user_id)
        # Apply pre-configured config if present
        pre_config = token_info.get("config")
        if pre_config and any(v is not None and v != "" for v in pre_config.values()):
            config_json = json.dumps(pre_config, ensure_ascii=False)
            data = _load_data()
            core_agent_id = _get_core_agent(data, new_user_id)
            await _spawn_to_core(
                identifier=f"_noreply_regcfg_{uuid.uuid4().hex[:8]}",
                user_id=new_user_id,
                session_id="SYSTEM",
                message=f"<update_user_config> {config_json}",
                core_agent_id=core_agent_id,
            )
        await _send_to_chat(
            platform, chat_id,
            f"Registered as {new_user_id}. Use /config to review your configuration.",
        )
        return

    if not user_id:
        # Rate limit check for unregistered users
        if _check_rate_limit(platform, platform_user_id):
            await _send_to_chat(
                platform, chat_id,
                "Too many attempts. Please try again later.",
            )
            return
        logger.info(
            "No user mapping for %s:%s — prompting registration",
            platform, platform_user_id,
        )
        await _send_to_chat(
            platform, chat_id,
            "You are not registered. Use /register <invitation_token> to register.",
        )
        return

    # Resolve per-user core agent (read-only lookup, no lock needed).
    user_core_agent = _get_core_agent(_load_data(), user_id)

    if cmd == "new":
        old_sid, new_sid = await _rotate_session(platform, chat_id, platform_user_id, user_id)
        if old_sid:
            _pending_removal.add(old_sid)
            typing_task = asyncio.create_task(_send_typing_loop(platform, chat_id))
            _typing_tasks[old_sid] = typing_task
            await _spawn_to_core(
                identifier=old_sid,
                user_id=user_id,
                session_id=old_sid,
                message=f"<new_session> {new_sid}",
                core_agent_id=user_core_agent,
            )
        else:
            await _send_to_chat(platform, chat_id, "New session started.")
        return

    if cmd == "tokens":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        await _spawn_to_core(
            identifier=session_id, user_id=user_id,
            session_id=session_id, message="<token_info>",
            core_agent_id=user_core_agent,
        )
        return

    if cmd == "agents":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        await _spawn_to_core(
            identifier=session_id, user_id=user_id,
            session_id=session_id, message="<agents_info>",
            core_agent_id=user_core_agent,
        )
        return

    if cmd == "config":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg:
            await _spawn_to_core(
                identifier=session_id, user_id=user_id,
                session_id=session_id,
                message=f"<config_instruct> {arg}",
                core_agent_id=user_core_agent,
            )
        else:
            await _spawn_to_core(
                identifier=session_id, user_id=user_id,
                session_id=session_id, message="<show_config>",
                core_agent_id=user_core_agent,
            )
        return

    if cmd == "model":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg:
            await _spawn_to_core(
                identifier=session_id, user_id=user_id,
                session_id=session_id,
                message=f"<set_model> {arg}",
                core_agent_id=user_core_agent,
            )
        else:
            await _spawn_to_core(
                identifier=session_id, user_id=user_id,
                session_id=session_id, message="<list_models>",
                core_agent_id=user_core_agent,
            )
        return

    if cmd == "link":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg:
            await _spawn_to_core(
                identifier=session_id, user_id=user_id,
                session_id=session_id,
                message=f"<link_agent> {arg}",
                core_agent_id=user_core_agent,
            )
        else:
            await _spawn_to_core(
                identifier=session_id, user_id=user_id,
                session_id=session_id, message="<list_linkable>",
                core_agent_id=user_core_agent,
            )
        return

    if cmd == "unlink":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        await _spawn_to_core(
            identifier=session_id, user_id=user_id,
            session_id=session_id, message="<unlink_agent>",
            core_agent_id=user_core_agent,
        )
        return

    if cmd == "stop":
        session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)
        _typing_tasks[session_id] = asyncio.create_task(_send_typing_loop(platform, chat_id))
        await _spawn_to_core(
            identifier=session_id, user_id=user_id,
            session_id=session_id, message="<stop_session>",
            core_agent_id=user_core_agent,
        )
        return

    if cmd in ("start", "help"):
        await _send_to_chat(
            platform, chat_id,
            "Commands: /new · /stop · /tokens · /agents\n"
            "/config — show config · /config <instruction> — modify config\n"
            "/model — list models · /model <id> — switch model\n"
            "/link — list linkable agents · /link <id> — direct talk\n"
            "/unlink — end direct agent link\n"
            "/register <token> — register with invitation token"
        )
        return

    # --- Normal message ---
    session_id = await _get_or_create_session(platform, chat_id, platform_user_id, user_id)

    # Send typing indicator while processing
    typing_task = asyncio.create_task(_send_typing_loop(platform, chat_id))
    _typing_tasks[session_id] = typing_task

    task_id = await _spawn_to_core(
        identifier=session_id, user_id=user_id,
        session_id=session_id, message=text, files=files,
        core_agent_id=user_core_agent,
    )

    if not task_id:
        typing_task.cancel()
        _typing_tasks.pop(session_id, None)

    # If verbose mode, subscribe to progress events and relay to user
    if task_id and _is_verbose_user(user_id):
        asyncio.create_task(_stream_progress_to_user(task_id, platform, chat_id))


# ---------------------------------------------------------------------------
# Result routing
# ---------------------------------------------------------------------------

async def _route_result(data: dict[str, Any]) -> None:
    """
    Process an inbound delivery from the router.

    Two distinct paths:

    1. **Result delivery** (destination_agent_id is null):
       Response to a task this agent spawned.  Already secured by the
       router's task tree and ACL — no extra verification needed.
       ``identifier`` encodes the routing intent:
         - "dc_*"/"cfg_*" → direct-chat / config from WebUI, signal event
         - <session_id>   → route to platform chat

    2. **Direct invocation** (destination_agent_id is set):
       Another agent proactively sends a message to a user (e.g. reminder).
       Payload follows input_schema: {user_id, session_id, message}.
       ``user_id`` is verified against the session owner before delivery.
    """
    destination = data.get("destination_agent_id")
    payload: dict = data.get("payload") or {}

    # --- Path 2: Direct invocation (agent-initiated message) ---
    if destination is not None:
        await _handle_direct_message(data, payload)
        return

    # --- Path 1: Result delivery (response to spawned task) ---
    identifier: str = data.get("identifier") or ""
    content: str = payload.get("content") or payload.get("error") or ""
    status_code: int = data.get("status_code") or 200

    if identifier.startswith("dc_") or identifier.startswith("cfg_"):
        ev = _dc_events.get(identifier)
        if ev:
            _dc_results[identifier] = content
            ev.set()
        else:
            # No listener (already timed out) — discard to prevent leak.
            logger.debug("Discarding orphaned dc/cfg result for %s", identifier)
        return

    # Cancel typing indicator for this session
    tt = _typing_tasks.pop(identifier, None)
    if tt and not tt.done():
        tt.cancel()

    # Route to platform chat
    details = await _get_session_details(identifier)
    if not details:
        logger.warning("No session details for identifier %s", identifier)
        return

    result_files = payload.get("files")
    await _send_to_chat(details["platform"], details["chat_id"], content, files=result_files)

    # Remove old session if this was a /new result
    if identifier in _pending_removal:
        _pending_removal.discard(identifier)
        await _remove_session_details(identifier)
        logger.info("Session %s removed after /new", identifier)


async def _handle_direct_message(data: dict[str, Any], payload: dict[str, Any]) -> None:
    """
    Handle a direct invocation from another agent to send a message to a user.

    Payload schema: {user_id: str, session_id: str, message: str}

    Verifies that user_id matches the session owner before delivery.
    Reports result back to the router to close the task.
    """
    task_id: str = data.get("task_id") or ""
    parent_task_id: str = data.get("parent_task_id")
    user_id: str = (payload.get("user_id") or "").strip()
    session_id: str = (payload.get("session_id") or "").strip()
    message: str = (payload.get("message") or "").strip()

    if not user_id or not session_id or not message:
        logger.warning("Direct message missing required fields (user_id, session_id, message)")
        await _report_direct_result(task_id, parent_task_id, 400, "Missing required fields: user_id, session_id, message")
        return

    details = await _get_session_details(session_id)
    if not details:
        logger.warning("Direct message: no session details for session_id %s", session_id)
        await _report_direct_result(task_id, parent_task_id, 404, f"Session not found: {session_id}")
        return

    # Verify user_id matches session owner
    session_user_id = details.get("user_id", "")
    if user_id != session_user_id:
        logger.warning(
            "Direct message blocked: user_id mismatch for session %s "
            "(payload=%s, session_owner=%s)",
            session_id, user_id, session_user_id,
        )
        await _report_direct_result(task_id, parent_task_id, 403, "user_id does not match session owner")
        return

    await _send_to_chat(details["platform"], details["chat_id"], message)
    logger.info(
        "Direct message delivered to user %s via %s (session %s)",
        user_id, details["platform"], session_id,
    )
    await _report_direct_result(task_id, parent_task_id, 200, "Message delivered.")


async def _report_direct_result(
    task_id: str,
    parent_task_id: Optional[str],
    status_code: int,
    content: str,
) -> None:
    """Report result of a direct invocation back to the router."""
    if not _http_client or not _agent_id or not task_id:
        return
    body = build_result_request(
        agent_id=_agent_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=status_code,
        output=AgentOutput(content=content),
    )
    try:
        r = await _http_client.post(f"{ROUTER_URL}/route", json=body)
        r.raise_for_status()
    except Exception as exc:
        logger.error("Failed to report direct message result: %s", exc)


async def _get_session_details(session_id: str) -> Optional[dict]:
    """Look up session details by session_id."""
    async with _data_lock:
        raw = SESSIONS_FILE.read_text(encoding="utf-8") if SESSIONS_FILE.exists() else "{}"
        data_store: dict = json.loads(raw)
    return data_store.get("session_details", {}).get(session_id)


# ---------------------------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------------------------

async def _run_telegram() -> None:
    global _tg_app
    if not TELEGRAM_TOKEN:
        logger.info("Telegram token not configured — bot disabled")
        return

    from telegram import BotCommand
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
    from telegram.ext import filters as tg_filters
    from telegram import Update

    async def _tg_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message or update.edited_message
        if not msg or not update.effective_user:
            return
        user = update.effective_user
        platform_user_id = str(user.id)
        chat_id = str(msg.chat_id)
        text = msg.text or msg.caption or ""

        # --- Extract attached files ---
        proxy_files: list[dict[str, Any]] = []
        try:
            tg_file_obj = None
            filename = "file"
            if msg.photo:
                # Take the largest resolution.
                tg_file_obj = await msg.photo[-1].get_file()
                filename = f"photo_{msg.photo[-1].file_unique_id}.jpg"
            elif msg.document:
                tg_file_obj = await msg.document.get_file()
                filename = msg.document.file_name or f"doc_{msg.document.file_unique_id}"

            if tg_file_obj:
                buf = io.BytesIO()
                await tg_file_obj.download_to_memory(buf)
                file_bytes = buf.getvalue()
                pf = _save_file_locally(file_bytes, filename)
                if pf:
                    proxy_files.append(pf)
        except Exception as exc:
            logger.warning("Failed to extract Telegram file: %s", exc)

        asyncio.create_task(
            _handle_incoming(
                "telegram", chat_id, platform_user_id, text,
                files=proxy_files or None,
            )
        )

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    _tg_app = app

    # All text messages, captions, photos, and documents go through the same dispatcher
    app.add_handler(MessageHandler(
        (tg_filters.TEXT | tg_filters.CAPTION | tg_filters.PHOTO | tg_filters.Document.ALL)
        & ~tg_filters.UpdateType.EDITED_MESSAGE,
        _tg_dispatch,
    ))

    try:
        await app.bot.set_my_commands([
            BotCommand("new", "Start a new session"),
            BotCommand("stop", "Stop all active tasks"),
            BotCommand("tokens", "Show token usage"),
            BotCommand("agents", "List available agents"),
            BotCommand("config", "Show or modify user config"),
            BotCommand("model", "List or switch LLM model"),
            BotCommand("link", "Direct talk with an agent"),
            BotCommand("unlink", "End direct agent link"),
            BotCommand("register", "Register with invitation token"),
        ])
    except Exception as e:
        logger.warning("Failed to register Telegram commands: %s", e)

    logger.info("Starting Telegram bot (polling)...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        _tg_app = None


# ---------------------------------------------------------------------------
# Discord bot (Gateway WebSocket)
# ---------------------------------------------------------------------------

async def _run_discord() -> None:
    global _discord_http, _discord_bot_id
    if not DISCORD_TOKEN:
        logger.info("Discord token not configured — bot disabled")
        return

    import websockets  # noqa: PLC0415

    GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
    INTENTS = 37377  # GUILDS + GUILD_MESSAGES + MESSAGE_CONTENT + DIRECT_MESSAGES

    _discord_http = httpx.AsyncClient(timeout=30.0)

    # Fetch bot user id
    try:
        r = await _discord_http.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
        )
        r.raise_for_status()
        _discord_bot_id = r.json().get("id")
        logger.info("Discord bot connected as user %s", _discord_bot_id)
    except Exception as e:
        logger.error("Discord identity check failed: %s", e)

    _seq: Optional[int] = None
    _heartbeat_task: Optional[asyncio.Task] = None

    async def _heartbeat(ws: Any, interval_s: float) -> None:
        nonlocal _seq
        while True:
            try:
                await ws.send(json.dumps({"op": 1, "d": _seq}))
            except Exception:
                break
            await asyncio.sleep(interval_s)

    async def _identify(ws: Any) -> None:
        await ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": DISCORD_TOKEN,
                "intents": INTENTS,
                "properties": {"os": "linux", "browser": "channel_agent", "device": "channel_agent"},
            },
        }))

    async def _on_dc_message(payload: dict) -> None:
        author = payload.get("author") or {}
        if author.get("bot"):
            return
        sender_id = str(author.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        content = payload.get("content") or ""
        if not sender_id or not channel_id:
            return
        # In guilds, only respond if mentioned or DM
        guild_id = payload.get("guild_id")
        if guild_id and _discord_bot_id:
            mentions = [str(m.get("id", "")) for m in (payload.get("mentions") or [])]
            if _discord_bot_id not in mentions and f"<@{_discord_bot_id}>" not in content:
                return
            # Strip mention from content
            content = re.sub(r"<@!?" + re.escape(_discord_bot_id) + r">", "", content).strip()

        # --- Extract Discord attachments ---
        proxy_files: list[dict[str, Any]] = []
        for attachment in payload.get("attachments") or []:
            att_url = attachment.get("url")
            att_filename = attachment.get("filename", "file")
            if not att_url:
                continue
            try:
                async with httpx.AsyncClient(timeout=60.0) as dl_client:
                    dl_resp = await dl_client.get(att_url)
                    dl_resp.raise_for_status()
                    file_bytes = dl_resp.content
                pf = _save_file_locally(file_bytes, att_filename)
                if pf:
                    proxy_files.append(pf)
            except Exception as exc:
                logger.warning("Failed to extract Discord attachment '%s': %s", att_filename, exc)

        asyncio.create_task(
            _handle_incoming(
                "discord", channel_id, sender_id, content,
                files=proxy_files or None,
            )
        )

    while True:
        try:
            logger.info("Connecting to Discord gateway...")
            async with websockets.connect(GATEWAY) as ws:
                _seq = None
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    op = msg.get("op")
                    seq = msg.get("s")
                    if seq is not None:
                        _seq = seq
                    if op == 10:
                        interval_s = msg["d"]["heartbeat_interval"] / 1000
                        if _heartbeat_task:
                            _heartbeat_task.cancel()
                        _heartbeat_task = asyncio.create_task(_heartbeat(ws, interval_s))
                        await _identify(ws)
                    elif op == 0 and msg.get("t") == "READY":
                        logger.info("Discord gateway READY")
                    elif op == 0 and msg.get("t") == "MESSAGE_CREATE":
                        await _on_dc_message(msg.get("d") or {})
                    elif op in (7, 9):
                        logger.info("Discord gateway %s — reconnecting", "RECONNECT" if op == 7 else "INVALID_SESSION")
                        break
        except asyncio.CancelledError:
            break
        except Exception as e:
            if getattr(getattr(e, "rcvd", None), "code", None) == 4004:
                logger.error("Discord authentication failed (4004) — token invalid, not retrying")
                break
            logger.warning("Discord gateway error: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)

    if _heartbeat_task:
        _heartbeat_task.cancel()
    if _discord_http:
        await _discord_http.aclose()
        _discord_http = None


# ---------------------------------------------------------------------------
# Admin WebUI — auth
# ---------------------------------------------------------------------------

_signer = URLSafeTimedSerializer(SESSION_SECRET)
_SESSION_COOKIE = "ca_session"
_SESSION_MAX_AGE = 3600 * 8


def _make_session() -> str:
    return _signer.dumps("ok")


def _check_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        _signer.loads(token, max_age=_SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(ca_session: Optional[str] = Cookie(default=None)) -> None:
    if not _check_session(ca_session):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await _ensure_registered()
    tg_task = asyncio.create_task(_run_telegram())
    dc_task = asyncio.create_task(_run_discord())
    gc_task = asyncio.create_task(_file_gc_loop())
    yield
    tg_task.cancel()
    dc_task.cancel()
    gc_task.cancel()
    try:
        await asyncio.gather(tg_task, dc_task, gc_task, return_exceptions=True)
    except Exception:
        pass
    if _http_client:
        await _http_client.aclose()


app = FastAPI(title="Channel Inbound Agent", lifespan=lifespan)

_static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


# ---------------------------------------------------------------------------
# Router callback — POST /receive
# ---------------------------------------------------------------------------

@app.post("/refresh-info")
async def refresh_info(request: Request) -> JSONResponse:
    """Re-push this agent's AgentInfo to the router."""
    if _auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], _auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    if not _auth_token or not _agent_id:
        return JSONResponse({"status": "error", "detail": "Not connected to router."}, status_code=503)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.put(
                f"{ROUTER_URL}/agent-info",
                headers={"Authorization": f"Bearer {_auth_token}"},
                json={
                    "agent_id": _agent_id,
                    "description": (
                        "Channel inbound agent. Bridges Telegram and Discord to the router. "
                        "Calling this agent sends a direct message to the user on their active "
                        "chat platform (Telegram or Discord). Requires user_id and session_id "
                        "to identify the recipient."
                    ),
                    "input_schema": "user_id: str, session_id: str, message: str",
                    "output_schema": "content: str",
                    "required_input": ["user_id", "session_id", "message"],
                },
            )
            r.raise_for_status()
        return JSONResponse({"status": "refreshed"})
    except Exception as exc:
        logger.warning("Failed to refresh agent info: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=502)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "agent_id": _agent_id or "not initialized"})


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """Router delivers task results here. Must return 202 immediately."""
    # Verify delivery auth from router
    if _auth_token:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], _auth_token):
            return JSONResponse(status_code=403, content={"error": "Forbidden"})

    data = await request.json()
    asyncio.create_task(_route_result(data))
    return JSONResponse(status_code=202, content={"status": "accepted"})


# ---------------------------------------------------------------------------
# File serving — GET /files/{filename}
# ---------------------------------------------------------------------------


@app.get("/files/{filename}")
async def serve_file(filename: str, key: str = "") -> FileResponse:
    """Serve a locally stored file. Requires the correct access key."""
    expected_key = _local_file_keys.get(filename)
    if not expected_key or not secrets.compare_digest(key, expected_key):
        raise HTTPException(status_code=403, detail="Invalid or missing file key.")
    file_path = (_LOCAL_FILES_DIR / filename).resolve()
    if not str(file_path).startswith(str(_LOCAL_FILES_DIR.resolve()) + "/"):
        raise HTTPException(status_code=403, detail="Invalid filename.")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(str(file_path), filename=filename)


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/ui/login")
async def login(request: Request, response: Response) -> dict:
    body = await request.json()
    pw = body.get("password", "")
    if not pw or not _admin_pw.verify(pw):
        raise HTTPException(status_code=403, detail="Invalid password")
    response.set_cookie(_SESSION_COOKIE, _make_session(),
                        max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax")
    return {"status": "ok"}


@app.post("/ui/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(_SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/ui/whoami")
async def whoami(ca_session: Optional[str] = Cookie(default=None)) -> dict:
    return {"authenticated": _check_session(ca_session)}


@app.post("/ui/change-password")
async def change_password(request: Request, ca_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ca_session)
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
    if not _admin_pw.verify(current):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    _admin_pw.change(new_pw)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/ui/status")
async def ui_status(ca_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ca_session)
    router_connected = False
    if _http_client and _agent_id:
        try:
            r = await _http_client.get(f"{ROUTER_URL}/health", timeout=3.0)
            router_connected = r.status_code == 200
        except Exception:
            pass
    return {
        "router_connected": router_connected,
        "agent_id": _agent_id,
        "telegram_running": _tg_app is not None,
        "discord_running": _discord_http is not None,
        "router_url": ROUTER_URL,
        "receive_url": RECEIVE_URL,
    }


# ---------------------------------------------------------------------------
# User mappings
# ---------------------------------------------------------------------------

@app.get("/ui/users")
async def ui_list_users(ca_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
    return {
        "user_mappings": data["user_mappings"],
        "core_agent_map": data.get("core_agent_map", {}),
        "verbose_users": data.get("verbose_users", []),
        "default_core_agent": CORE_AGENT_ID,
    }


@app.post("/ui/users")
async def ui_add_user(
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    body = await request.json()
    platform = str(body.get("platform", "")).strip()
    platform_user_id = str(body.get("platform_user_id", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    if not all([platform, platform_user_id, user_id]):
        raise HTTPException(status_code=400, detail="platform, platform_user_id, user_id required")
    async with _data_lock:
        data = _load_data()
        data["user_mappings"][_user_mapping_key(platform, platform_user_id)] = user_id
        _save_data(data)
    return {"status": "ok"}


@app.delete("/ui/users/{mapping_key:path}")
async def ui_delete_user(
    mapping_key: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
        data["user_mappings"].pop(mapping_key, None)
        _save_data(data)
    return {"status": "ok"}


@app.get("/ui/core-agent-map")
async def ui_get_core_agent_map(
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
    return {
        "core_agent_map": data.get("core_agent_map", {}),
        "default": CORE_AGENT_ID,
    }


@app.put("/ui/core-agent-map/{user_id}")
async def ui_set_core_agent(
    user_id: str,
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    body = await request.json()
    agent_id = str(body.get("agent_id", "")).strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id required")
    async with _data_lock:
        data = _load_data()
        if "core_agent_map" not in data:
            data["core_agent_map"] = {}
        data["core_agent_map"][user_id] = agent_id
        _save_data(data)
    return {"status": "ok", "user_id": user_id, "agent_id": agent_id}


@app.delete("/ui/core-agent-map/{user_id}")
async def ui_delete_core_agent(
    user_id: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Remove per-user override, reverting to the default core agent."""
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
        data.get("core_agent_map", {}).pop(user_id, None)
        _save_data(data)
    return {"status": "ok"}


@app.get("/ui/verbose-users")
async def ui_get_verbose_users(
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
    return {"verbose_users": data.get("verbose_users", [])}


@app.put("/ui/verbose-users/{user_id}")
async def ui_set_verbose(
    user_id: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
        verbose = data.setdefault("verbose_users", [])
        if user_id not in verbose:
            verbose.append(user_id)
        _save_data(data)
    return {"status": "ok"}


@app.delete("/ui/verbose-users/{user_id}")
async def ui_unset_verbose(
    user_id: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
        verbose = data.get("verbose_users", [])
        if user_id in verbose:
            verbose.remove(user_id)
        _save_data(data)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Invitation tokens
# ---------------------------------------------------------------------------

@app.get("/ui/invitation-tokens")
async def ui_list_invitation_tokens(ca_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ca_session)
    return {"tokens": _get_valid_tokens()}


@app.post("/ui/invitation-tokens")
async def ui_create_invitation_token(
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    ttl = int(body.get("ttl", 86400))
    config = body.get("config") or {}
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    token = _create_invitation_token(user_id, ttl, config)
    return {"token": token, "user_id": user_id, "ttl": ttl}


@app.put("/ui/invitation-tokens/{token}/config")
async def ui_update_invitation_token_config(
    token: str,
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Update the pre-configured user config embedded in an invitation token."""
    _require_auth(ca_session)
    body = await request.json()
    config = body.get("config", {})
    td = _load_tokens_data()
    if token not in td["tokens"]:
        raise HTTPException(status_code=404, detail="Token not found")
    td["tokens"][token]["config"] = config
    _save_tokens_data(td)
    return {"status": "ok"}


@app.delete("/ui/invitation-tokens/{token}")
async def ui_delete_invitation_token(
    token: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    if _delete_invitation_token(token):
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Token not found")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@app.get("/ui/sessions")
async def ui_list_sessions(ca_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ca_session)
    async with _data_lock:
        data = _load_data()
    return {"sessions": data["sessions"], "session_details": data["session_details"]}


@app.delete("/ui/sessions/{session_id}")
async def ui_delete_session(
    session_id: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    await _remove_session_details(session_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get("/ui/logs")
async def ui_logs(
    n: int = 100,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    entries = list(_log_ring)[-n:]
    return {"logs": entries}


# ---------------------------------------------------------------------------
# Direct chat (WebUI → user via core agent, result back to WebUI)
# ---------------------------------------------------------------------------

@app.post("/ui/direct-chat")
async def ui_direct_chat(
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    message = str(body.get("message", "")).strip()
    if not user_id or not message:
        raise HTTPException(status_code=400, detail="user_id and message required")

    # Use a special dc_ identifier so the result is returned to the WebUI,
    # not routed to a platform chat.
    dc_id = f"dc_{secrets.token_hex(16)}"
    _dc_events[dc_id] = asyncio.Event()

    # Use a temporary session for the direct chat (so history is maintained)
    session_id = f"admin_{user_id}"
    await _spawn_to_core(
        identifier=dc_id,
        user_id=user_id,
        session_id=session_id,
        message=message,
    )
    return {"dc_id": dc_id}


@app.get("/ui/direct-chat-result/{dc_id}")
async def ui_direct_chat_result(
    dc_id: str,
    timeout: int = 90,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    timeout = min(max(int(timeout), 5), 120)
    if dc_id in _dc_results:
        return {"content": _dc_results.pop(dc_id), "dc_id": dc_id}
    ev = _dc_events.get(dc_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Unknown dc_id")
    try:
        await asyncio.wait_for(ev.wait(), timeout=float(timeout))
    except asyncio.TimeoutError:
        _dc_events.pop(dc_id, None)
        _dc_results.pop(dc_id, None)
        return {"content": None, "dc_id": dc_id, "status": "timeout"}
    _dc_events.pop(dc_id, None)
    return {"content": _dc_results.pop(dc_id, ""), "dc_id": dc_id}


# ---------------------------------------------------------------------------
# User config (sends <user_config> to core agent via SYSTEM session)
# ---------------------------------------------------------------------------

_USER_CONFIG_TEMPLATE = """\
# Name: {user_id}
# Language: English
# Tone preference: friendly, concise
# Background: (describe the user's role, expertise, and context)
# Interests: (list topics the user often asks about)
# Special instructions: (any persistent notes for the assistant)
"""


@app.get("/ui/user-config/{user_id}")
async def ui_get_user_config(
    user_id: str,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    return {
        "user_id": user_id,
        "template": _USER_CONFIG_TEMPLATE.replace("{user_id}", user_id),
    }


@app.post("/ui/user-config")
async def ui_save_user_config(
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    _require_auth(ca_session)
    body = await request.json()
    user_id = str(body.get("user_id", "")).strip()
    content = str(body.get("content", "")).strip()
    if not user_id or not content:
        raise HTTPException(status_code=400, detail="user_id and content required")

    message = f"<user_config> {content}"
    await _spawn_to_core(
        identifier=f"_noreply_ucfg_{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        session_id="SYSTEM",
        message=message,
    )
    logger.info("User config sent for %s", user_id)
    return {"status": "ok", "note": "Config sent to core agent. Result logged only."}


@app.get("/ui/user-json-config/{user_id}")
async def ui_get_user_json_config(
    user_id: str,
    timeout: int = 30,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Fetch per-user JSON config by sending <fetch_user_config> to core agent."""
    _require_auth(ca_session)
    dc_id = f"cfg_fetch_{user_id}_{uuid.uuid4().hex[:8]}"
    _dc_events[dc_id] = asyncio.Event()

    data = _load_data()
    core_agent_id = _get_core_agent(data, user_id)
    await _spawn_to_core(
        identifier=dc_id,
        user_id=user_id,
        session_id="SYSTEM",
        message="<fetch_user_config>",
        core_agent_id=core_agent_id,
    )

    ev = _dc_events.get(dc_id)
    if ev:
        try:
            await asyncio.wait_for(ev.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            _dc_events.pop(dc_id, None)
            _dc_results.pop(dc_id, None)
            return {"user_id": user_id, "config": None, "status": "timeout"}
    _dc_events.pop(dc_id, None)
    raw = _dc_results.pop(dc_id, "{}")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        config = {"_raw": raw}
    return {"user_id": user_id, "config": config}


@app.post("/ui/user-json-config/{user_id}")
async def ui_save_user_json_config(
    user_id: str,
    request: Request,
    timeout: int = 30,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Save per-user JSON config by sending <update_user_config> to core agent."""
    _require_auth(ca_session)
    body = await request.json()
    config_json = json.dumps(body.get("config", {}), ensure_ascii=False)

    dc_id = f"cfg_save_{user_id}_{uuid.uuid4().hex[:8]}"
    _dc_events[dc_id] = asyncio.Event()

    data = _load_data()
    core_agent_id = _get_core_agent(data, user_id)
    await _spawn_to_core(
        identifier=dc_id,
        user_id=user_id,
        session_id="SYSTEM",
        message=f"<update_user_config> {config_json}",
        core_agent_id=core_agent_id,
    )

    ev = _dc_events.get(dc_id)
    if ev:
        try:
            await asyncio.wait_for(ev.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            _dc_events.pop(dc_id, None)
            _dc_results.pop(dc_id, None)
            return {"user_id": user_id, "status": "timeout"}
    _dc_events.pop(dc_id, None)
    raw = _dc_results.pop(dc_id, "{}")
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        config = {"_raw": raw}
    return {"user_id": user_id, "config": config, "status": "ok"}


# ---------------------------------------------------------------------------
# Onboarding info
# ---------------------------------------------------------------------------

@app.get("/ui/onboarding")
async def ui_onboarding(ca_session: Optional[str] = Cookie(default=None)) -> dict:
    _require_auth(ca_session)
    router_reachable = False
    if _http_client:
        try:
            r = await _http_client.get(f"{ROUTER_URL}/health", timeout=3.0)
            router_reachable = r.status_code == 200
        except Exception:
            pass
    return {
        "router_url": ROUTER_URL,
        "agent_id": _agent_id,
        "receive_url": RECEIVE_URL,
        "registered": _agent_id is not None,
        "router_reachable": router_reachable,
    }


@app.post("/ui/onboarding/register")
async def ui_onboarding_register(
    request: Request,
    ca_session: Optional[str] = Cookie(default=None),
) -> dict:
    """Re-register with the router using a new invitation token."""
    _require_auth(ca_session)
    body = await request.json()
    token = str(body.get("invitation_token", "")).strip()
    if not token:
        raise HTTPException(status_code=400, detail="invitation_token required")
    # Temporarily override the env var and re-run onboarding
    global INVITATION_TOKEN
    INVITATION_TOKEN = token
    await _ensure_registered()
    return {
        "status": "ok" if _agent_id else "failed",
        "agent_id": _agent_id,
    }


# ---------------------------------------------------------------------------
# Root — serve the SPA
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(str(_static / "index.html"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
