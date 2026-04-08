"""
config_ui.py — Reusable config.json editor endpoints for agent web UIs.

Provides GET /ui/config (read current config + field descriptions) and
PUT /ui/config (write config).  Reads from data/config.json and
config.example in the agent's directory.

Usage in web_ui.py:
    from config_ui import add_config_routes
    add_config_routes(router, agent_dir, require_auth_fn)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


def add_config_routes(
    router: APIRouter,
    agent_dir: str | Path,
    require_auth: Callable,
    cookie_name: str = "session",
) -> None:
    """Register GET/PUT /ui/config endpoints on the given router.

    Args:
        router: FastAPI APIRouter to attach endpoints to.
        agent_dir: Path to the agent's root directory (contains
            config.example and data/config.json).
        require_auth: Callable that validates the session cookie.
            Called with the raw cookie value; should raise HTTPException
            on failure.
        cookie_name: Name of the session cookie used by this agent.
    """
    agent_dir = Path(agent_dir)
    config_path = agent_dir / "data" / "config.json"
    example_path = agent_dir / "config.example"

    @router.get("/ui/config")
    async def get_config(request: Request) -> dict[str, Any]:
        require_auth(request.cookies.get(cookie_name))
        config: dict[str, Any] = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        example: dict[str, str] = {}
        if example_path.exists():
            try:
                example = json.loads(example_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"config": config, "example": example}

    @router.put("/ui/config")
    async def put_config(request: Request) -> dict[str, str]:
        require_auth(request.cookies.get(cookie_name))
        body = await request.json()
        new_config = body.get("config")
        if not isinstance(new_config, dict):
            raise HTTPException(status_code=400, detail="config must be a JSON object")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(new_config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.rename(config_path)
        return {"status": "ok"}
