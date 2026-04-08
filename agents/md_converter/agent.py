"""
agents/md_converter/agent.py — Document-to-Markdown converter via MarkItDown.

Lightweight embedded agent that converts PDF, DOCX, PPTX, XLSX, HTML,
images, and other formats to Markdown using Microsoft's MarkItDown library.

Optional OCR plugin (markitdown-ocr) uses an OpenAI-compatible VLM endpoint
for image and scanned PDF extraction.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_DIR = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from helper import (
    AgentInfo,
    AgentOutput,
    ProxyFile,
    ProxyFileManager,
    build_result_request,
)

logger = logging.getLogger("md_converter")

# ---------------------------------------------------------------------------
# Configuration — read directly from config.json (hot-reloadable)
# ---------------------------------------------------------------------------

_CONFIG_PATH = _AGENT_DIR / "data" / "config.json"
_OUR_AGENT_ID = "md_converter"


def _load_config() -> dict[str, Any]:
    """Read config.json from disk (re-read on every call for hot-reload)."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cfg(key: str, default: str = "") -> str:
    """Get a config value as string, with fallback default.

    Empty-string values in config.json are treated as unset so that code
    defaults take effect.
    """
    val = str(_load_config().get(key, default))
    return val if val else default


ROUTER_URL: str = os.environ.get("ROUTER_URL", "http://localhost:8000")
OUTPUT_DIR: str = str(_AGENT_DIR / "data" / "output")
MAX_CONTENT_LENGTH: int = 30000
PREVIEW_LENGTH: int = 2000


def _refresh_config() -> None:
    """Re-read config.json and update module-level variables."""
    global OUTPUT_DIR, MAX_CONTENT_LENGTH, PREVIEW_LENGTH
    cfg = _load_config()
    _si = lambda v, d: d if v is None or v == "" else int(v)
    OUTPUT_DIR = str(cfg.get("OUTPUT_DIR") or str(_AGENT_DIR / "data" / "output"))
    MAX_CONTENT_LENGTH = _si(cfg.get("MAX_CONTENT_LENGTH"), 30000)
    PREVIEW_LENGTH = _si(cfg.get("PREVIEW_LENGTH"), 2000)


_refresh_config()  # Initial load

_OUTPUT_MAX_AGE_SECS: int = 3600  # 1 hour


_last_cleanup: float = 0.0
_CLEANUP_INTERVAL_SECS: float = 300.0  # Run at most once every 5 minutes


def _cleanup_output_dir() -> None:
    """Remove output files older than _OUTPUT_MAX_AGE_SECS (throttled)."""
    import time
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL_SECS:
        return
    _last_cleanup = now
    out_dir = Path(OUTPUT_DIR)
    if not out_dir.is_dir():
        return
    threshold = now - _OUTPUT_MAX_AGE_SECS
    for f in out_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < threshold:
            try:
                f.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# AgentInfo
# ---------------------------------------------------------------------------

AGENT_INFO = AgentInfo(
    agent_id=_OUR_AGENT_ID,
    description=(
        "Document-to-Markdown converter. Pass a file via the file argument. "
        "Supports PDF, DOCX, PPTX, XLSX, HTML, images (with OCR). "
        "Returns markdown as text. Set output_file=true to get a .md file "
        "attachment instead (useful for large documents or downstream storage)."
    ),
    input_schema="file: ProxyFile, output_file: Optional[bool]",
    output_schema="content: str, files: Optional[List[ProxyFile]]",
    required_input=["file"],
)

# ---------------------------------------------------------------------------
# MarkItDown singleton (lazy-loaded)
# ---------------------------------------------------------------------------

_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        kwargs: dict[str, Any] = {}

        ocr_enabled = _cfg("OCR_ENABLED", "true").lower() in ("true", "1", "yes")
        ocr_base_url = _cfg("OCR_BASE_URL")
        ocr_model = _cfg("OCR_MODEL")
        if ocr_enabled and ocr_base_url and ocr_model:
            try:
                from openai import OpenAI

                kwargs["enable_plugins"] = True
                kwargs["llm_client"] = OpenAI(
                    base_url=ocr_base_url,
                    api_key=_cfg("OCR_API_KEY") or "placeholder",
                )
                kwargs["llm_model"] = ocr_model
                if _cfg("OCR_NO_PROMPT", "false").lower() in ("true", "1", "yes"):
                    kwargs["llm_prompt"] = ""
                elif _cfg("OCR_PROMPT"):
                    kwargs["llm_prompt"] = _cfg("OCR_PROMPT")
                logger.info(
                    "OCR plugin enabled: model=%s base_url=%s",
                    ocr_model, ocr_base_url,
                )
            except ImportError:
                logger.warning(
                    "OCR requested but 'openai' package not available. "
                    "OCR will be disabled."
                )
        else:
            if ocr_enabled:
                logger.info(
                    "OCR enabled but OCR_BASE_URL or OCR_MODEL not configured. "
                    "OCR will be disabled."
                )

        _converter = MarkItDown(**kwargs)
    return _converter


# ---------------------------------------------------------------------------
# File resolution helper
# ---------------------------------------------------------------------------


_md_pfm = ProxyFileManager(
    inbox_dir=Path(__file__).resolve().parent / "data" / "inbox",
    router_url=ROUTER_URL,
)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


async def _run(data: dict[str, Any]) -> dict[str, Any]:
    """Process an inbound routing payload."""
    task_id: str = data.get("task_id", "")
    parent_task_id: Optional[str] = data.get("parent_task_id")
    raw_payload: dict[str, Any] = data.get("payload", {})

    # Extract ProxyFile
    file_raw = raw_payload.get("file") or raw_payload.get("file_path")  # file preferred, file_path for backward compat
    if not file_raw:
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=400,
            output=AgentOutput(content="Error: payload.file (ProxyFile) is required"),
        )

    # Fetch file to local inbox (handles any protocol: router-proxy, localfile, http)
    try:
        local_path = await _md_pfm.fetch(file_raw, task_id)
        filename = Path(local_path).name or "document"
    except Exception as exc:
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=502,
            output=AgentOutput(content=f"Error resolving file: {exc}"),
        )

    # Convert using local file path directly.
    # Run in thread executor to avoid blocking the router's event loop
    # (MarkItDown.convert is synchronous and OCR can be slow).
    import asyncio as _aio
    from functools import partial as _partial

    try:
        converter = _get_converter()
        loop = _aio.get_running_loop()
        result = await loop.run_in_executor(None, _partial(converter.convert, local_path))
        markdown = result.text_content or ""
    except Exception as exc:
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=500,
            output=AgentOutput(content=f"Conversion error: {exc}"),
        )

    file_size = Path(local_path).stat().st_size
    logger.info("Converted '%s' (%d bytes) -> %d chars markdown", filename, file_size, len(markdown))

    # Determine if output should be returned as file
    force_file = raw_payload.get("output_file", False)
    if isinstance(force_file, str):
        force_file = force_file.lower() in ("true", "1", "yes")

    as_file = force_file or len(markdown) > MAX_CONTENT_LENGTH

    if as_file:
        # Write markdown to output file and return as localfile ProxyFile
        out_dir = Path(OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        md_filename = Path(filename).stem + ".md"
        out_path = out_dir / md_filename
        if out_path.exists():
            import uuid as _uuid
            md_filename = f"{Path(filename).stem}_{_uuid.uuid4().hex[:6]}.md"
            out_path = out_dir / md_filename
        out_path.write_text(markdown, encoding="utf-8")

        # Build preview content
        preview = markdown[:PREVIEW_LENGTH]
        if len(markdown) > PREVIEW_LENGTH:
            preview += f"\n\n[... truncated — full content in attached file ({len(markdown)} chars)]"

        pf_dict = _md_pfm.resolve(str(out_path.resolve()))
        return build_result_request(
            agent_id=_OUR_AGENT_ID,
            task_id=task_id,
            parent_task_id=parent_task_id,
            status_code=200,
            output=AgentOutput(
                content=preview,
                files=[ProxyFile(**pf_dict)],
            ),
        )

    return build_result_request(
        agent_id=_OUR_AGENT_ID,
        task_id=task_id,
        parent_task_id=parent_task_id,
        status_code=200,
        output=AgentOutput(content=markdown),
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="MD Converter Agent")


@app.post("/receive")
async def receive(request: Request) -> JSONResponse:
    """Called by the router via in-process ASGI transport."""
    _refresh_config()
    _cleanup_output_dir()
    data = await request.json()
    try:
        result = await _run(data)
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        logger.exception("Unhandled error in md_converter")
        task_id = data.get("task_id", "")
        parent_task_id = data.get("parent_task_id")
        return JSONResponse(
            status_code=200,
            content=build_result_request(
                agent_id=_OUR_AGENT_ID,
                task_id=task_id,
                parent_task_id=parent_task_id,
                status_code=500,
                output=AgentOutput(content=f"MD converter error: {exc}"),
            ),
        )
