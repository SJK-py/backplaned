"""
kb_agent/web_ui.py — Admin and user web UI for the knowledge base agent.

User auth: users log in with user_id + password (configured by admin).
Admin auth: admin password from .env.
"""

from __future__ import annotations

import json
import secrets as _secrets
import sys as _sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Cookie, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from helper import PasswordFile, hash_password, verify_password, is_password_hashed
from config_ui import add_config_routes


def build_web_router() -> APIRouter:
    router = APIRouter()

    COOKIE = "kb_session"
    MAX_AGE = 3600 * 8
    _signer_cache: list = []
    _admin_pw_cache: list[PasswordFile] = []

    def _cfg():
        from agent import agent_config
        return agent_config

    def _db():
        from agent import kb_db
        return kb_db

    def _get_admin_pw() -> PasswordFile:
        if not _admin_pw_cache:
            _admin_pw_cache.append(
                PasswordFile(Path(_cfg().data_dir) / "admin_password.json", _cfg().admin_password)
            )
        return _admin_pw_cache[0]

    def _get_signer():
        if not _signer_cache:
            _signer_cache.append(
                URLSafeTimedSerializer(_cfg().session_secret or _secrets.token_hex(32))
            )
        return _signer_cache[0]

    def _users_file() -> Path:
        return Path(_cfg().data_dir) / "users.json"

    def _load_users() -> dict[str, Any]:
        p = _users_file()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
        return {}

    def _save_users(users: dict[str, Any]) -> None:
        _users_file().write_text(json.dumps(users, indent=2))

    def _migrate_user_passwords(users: dict[str, Any]) -> bool:
        """Hash any plaintext passwords in-place. Returns True if any were migrated."""
        changed = False
        for uid, u in users.items():
            pw = u.get("password", "")
            if pw and not is_password_hashed(pw):
                u["password"] = hash_password(pw)
                changed = True
        return changed

    def _make_token(identity: str, role: str = "user") -> str:
        return _get_signer().dumps({"identity": identity, "role": role})

    def _verify(token: str) -> Optional[dict]:
        try:
            return _get_signer().loads(token, max_age=MAX_AGE)
        except (BadSignature, SignatureExpired):
            return None

    def _get_session(kb_session: Optional[str]) -> dict:
        if not kb_session:
            raise HTTPException(status_code=401, detail="Not authenticated")
        data = _verify(kb_session)
        if not data:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return data

    def _require_admin(kb_session: Optional[str]) -> None:
        data = _get_session(kb_session)
        if data.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")

    # -- Auth --

    @router.post("/ui/login")
    async def login(request: Request, response: Response) -> dict:
        body = await request.json()
        user_id = body.get("user_id", "").strip()
        password = body.get("password", "").strip()

        # Admin login
        if user_id == "admin" and _get_admin_pw().verify(password):
            token = _make_token("admin", "admin")
            response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax")
            return {"status": "ok", "role": "admin"}

        # User login
        users = _load_users()
        user = users.get(user_id)
        if user and verify_password(password, user.get("password", "")):
            # Auto-migrate plaintext password to hash on successful login
            if not is_password_hashed(user.get("password", "")):
                user["password"] = hash_password(password)
                _save_users(users)
            token = _make_token(user_id, "user")
            response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax")
            return {"status": "ok", "role": "user", "user_id": user_id}

        raise HTTPException(status_code=403, detail="Invalid credentials")

    @router.post("/ui/logout")
    async def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE)
        return {"status": "ok"}

    @router.get("/ui/whoami")
    async def whoami(kb_session: Optional[str] = Cookie(default=None)) -> dict:
        if not kb_session:
            return {"authenticated": False}
        data = _verify(kb_session)
        if not data:
            return {"authenticated": False}
        return {"authenticated": True, "identity": data.get("identity"), "role": data.get("role")}

    # -- Status --

    @router.get("/ui/status")
    async def status(kb_session: Optional[str] = Cookie(default=None)) -> dict:
        session = _get_session(kb_session)
        from agent import router_client
        return {
            "agent_id": _cfg().agent_id,
            "router_url": _cfg().router_url,
            "router_connected": router_client is not None,
            "embed_model": _cfg().embed_model,
            "vector_dim": _cfg().vector_dim,
            "user_count": len(_db().list_user_ids()),
            "role": session.get("role"),
        }

    # -- Documents (user-scoped) --

    def _resolve_user_id(session: dict) -> str:
        if session["role"] == "admin":
            return session.get("identity", "admin")
        return session["identity"]

    @router.get("/ui/documents")
    async def list_docs(
        user_id: Optional[str] = None,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        session = _get_session(kb_session)
        uid = user_id if session["role"] == "admin" and user_id else _resolve_user_id(session)
        docs = await _db().list_documents(uid)
        return {"user_id": uid, "documents": docs}

    @router.post("/ui/search")
    async def search_docs(
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        session = _get_session(kb_session)
        body = await request.json()
        uid = body.get("user_id") if session["role"] == "admin" else _resolve_user_id(session)
        results = await _db().search(
            user_id=uid,
            query=body.get("query", ""),
            count=body.get("count", 5),
            collection=body.get("collection"),
            title=body.get("title"),
            tag=body.get("tag"),
            mode=body.get("mode", "hybrid"),
        )
        return {"user_id": uid, "results": results}

    @router.delete("/ui/documents/{title:path}")
    async def delete_doc(
        title: str,
        user_id: Optional[str] = None,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        session = _get_session(kb_session)
        uid = user_id if session["role"] == "admin" and user_id else _resolve_user_id(session)
        removed = await _db().remove_document(uid, title)
        if not removed:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"status": "ok"}

    @router.put("/ui/documents/{title:path}/metadata")
    async def update_metadata(
        title: str,
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        session = _get_session(kb_session)
        body = await request.json()
        uid = body.get("user_id") if session["role"] == "admin" else _resolve_user_id(session)
        tags = [t.strip() for t in body.get("tags", "").split(",") if t.strip()] if body.get("tags") else None
        ok = await _db().modify_metadata(
            user_id=uid, title=title,
            collection=body.get("collection"),
            description=body.get("description"),
            date=body.get("date"),
            tags=tags,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Update failed")
        return {"status": "ok"}

    @router.post("/ui/upload")
    async def upload_md(
        file: UploadFile = File(...),
        collection: str = "default",
        title: Optional[str] = None,
        description: str = "",
        tags: str = "",
        user_id: Optional[str] = None,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
        session = _get_session(kb_session)
        uid = user_id if session["role"] == "admin" and user_id else _resolve_user_id(session)
        raw = await file.read()
        if len(raw) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"File exceeds maximum upload size ({_MAX_UPLOAD_BYTES // (1024*1024)} MB)")
        content = raw.decode("utf-8", errors="replace")
        doc_title = title or Path(file.filename or "document").stem
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        result = await _db().store_document(
            user_id=uid, text=content, title=doc_title,
            collection=collection, description=description, tags=tag_list,
        )
        return result

    # -- Convert (user) — sends file to md_converter via router --

    @router.post("/ui/convert")
    async def convert_file(
        file: UploadFile = File(...),
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        """Convert a file to markdown via md_converter agent."""
        session = _get_session(kb_session)
        from agent import router_client, _spawn_and_wait, agent_config
        from helper import ProxyFileManager
        if not router_client:
            raise HTTPException(status_code=503, detail="Not connected to router")

        # Save uploaded file to temp (use original filename for proper naming)
        import tempfile
        original_name = file.filename or "document"
        suffix = Path(original_name).suffix
        content = await file.read()
        ws_dir = Path(_cfg().data_dir) / "workspaces"
        ws_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=str(ws_dir)) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            # Use ProxyFileManager to resolve the file path into a proper
            # ProxyFile dict (http with serve-key for external agents,
            # localfile for embedded).  The router's _ingest_payload_files
            # will convert it to router-proxy for the destination agent.
            cfg = _cfg()
            pfm = ProxyFileManager(
                inbox_dir=ws_dir / "inbox",
                router_url=cfg.router_url,
                agent_endpoint_url=cfg.agent_endpoint_url or f"http://localhost:{cfg.agent_port}",
            )
            resolved_file = pfm.resolve(tmp_path)
            if resolved_file:
                resolved_file["original_filename"] = original_name
            else:
                resolved_file = {"path": tmp_path, "protocol": "localfile", "key": None, "original_filename": original_name}
            result_data = await _spawn_and_wait(
                cfg.md_converter_id,
                {"file": resolved_file},
                timeout=300.0,
            )
            payload = result_data.get("payload", {})
            sc = result_data.get("status_code", 200)
            md_content = payload.get("content", "")
            if sc and sc >= 400:
                return {"status": "error", "detail": md_content}
            return {"status": "ok", "markdown": md_content}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    # -- Store text directly (for converted content) --

    @router.post("/ui/store-text")
    async def store_text(
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        session = _get_session(kb_session)
        uid = _resolve_user_id(session)
        body = await request.json()
        content = body.get("content", "")
        title = body.get("title", "document")
        collection = body.get("collection", "default")
        tags_str = body.get("tags", "")
        tag_list = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None
        if not content:
            raise HTTPException(status_code=400, detail="No content to store")
        result = await _db().store_document(
            user_id=uid, text=content, title=title,
            collection=collection, tags=tag_list,
        )
        return result

    # -- Admin: User Management --

    @router.get("/ui/admin/users")
    async def admin_list_users(kb_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_admin(kb_session)
        users = _load_users()
        return {"users": [{"user_id": k, **{mk: mv for mk, mv in v.items() if mk != "password"}} for k, v in users.items()]}

    @router.post("/ui/admin/users")
    async def admin_create_user(
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_admin(kb_session)
        body = await request.json()
        uid = body.get("user_id", "").strip()
        pw = body.get("password", "").strip()
        if not uid or not pw:
            raise HTTPException(status_code=400, detail="user_id and password required")
        users = _load_users()
        users[uid] = {"password": hash_password(pw), "model_id": body.get("model_id", "")}
        _save_users(users)
        return {"status": "ok"}

    @router.put("/ui/admin/users/{uid}")
    async def admin_update_user(
        uid: str,
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_admin(kb_session)
        body = await request.json()
        users = _load_users()
        if uid not in users:
            raise HTTPException(status_code=404, detail="User not found")
        if "model_id" in body:
            users[uid]["model_id"] = body["model_id"] or ""
        _save_users(users)
        return {"status": "ok"}

    @router.delete("/ui/admin/users/{uid}")
    async def admin_delete_user(
        uid: str,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_admin(kb_session)
        users = _load_users()
        users.pop(uid, None)
        _save_users(users)
        return {"status": "ok"}

    @router.post("/ui/admin/users/{uid}/reset-password")
    async def admin_reset_user_password(
        uid: str,
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        _require_admin(kb_session)
        body = await request.json()
        new_pw = body.get("new_password", "").strip()
        if not new_pw or len(new_pw) < 4:
            raise HTTPException(status_code=400, detail="New password must be at least 4 characters")
        users = _load_users()
        if uid not in users:
            raise HTTPException(status_code=404, detail="User not found")
        users[uid]["password"] = hash_password(new_pw)
        _save_users(users)
        return {"status": "ok"}

    # -- Change password (admin or user) --

    @router.post("/ui/change-password")
    async def change_password(
        request: Request,
        kb_session: Optional[str] = Cookie(default=None),
    ) -> dict:
        session = _get_session(kb_session)
        body = await request.json()
        current = body.get("current_password", "")
        new_pw = body.get("new_password", "")
        if not new_pw or len(new_pw) < 4:
            raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

        if session.get("role") == "admin":
            apw = _get_admin_pw()
            if not apw.verify(current):
                raise HTTPException(status_code=403, detail="Current password is incorrect")
            apw.change(new_pw)
        else:
            uid = session["identity"]
            users = _load_users()
            user = users.get(uid)
            if not user or not verify_password(current, user.get("password", "")):
                raise HTTPException(status_code=403, detail="Current password is incorrect")
            user["password"] = hash_password(new_pw)
            _save_users(users)
        return {"status": "ok"}

    # -- Onboarding --

    @router.get("/ui/onboarding")
    async def onboarding(kb_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_admin(kb_session)
        from agent import router_client, available_destinations
        return {
            "router_url": _cfg().router_url,
            "agent_id": _cfg().agent_id,
            "registered": router_client is not None,
            "available_destinations": list(available_destinations.keys()),
        }

    @router.post("/ui/onboarding/register")
    async def register(request: Request, kb_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_admin(kb_session)
        body = await request.json()
        token = body.get("invitation_token", "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="invitation_token required")
        cfg = _cfg()
        from helper import RouterClient, onboard as do_onboard
        import agent as agent_mod
        endpoint_url = cfg.agent_endpoint_url or f"http://localhost:{cfg.agent_port}"
        try:
            resp = await do_onboard(
                router_url=cfg.router_url,
                invitation_token=token,
                endpoint_url=f"{endpoint_url}/receive",
                agent_info=agent_mod.agent_info,
            )
            cfg.agent_auth_token = resp.auth_token
            cfg.agent_id = resp.agent_id
            agent_mod.available_destinations = resp.available_destinations
            creds_path = Path(cfg.data_dir) / "credentials.json"
            creds_path.write_text(json.dumps({"agent_id": resp.agent_id, "auth_token": resp.auth_token}))
            if agent_mod.router_client:
                await agent_mod.router_client.aclose()
            agent_mod.router_client = RouterClient(
                router_url=cfg.router_url, agent_id=resp.agent_id, auth_token=resp.auth_token,
            )
            return {"status": "ok", "agent_id": resp.agent_id}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @router.post("/ui/refresh-info")
    async def ui_refresh_info(kb_session: Optional[str] = Cookie(default=None)) -> dict:
        _require_admin(kb_session)
        import httpx
        cfg = _cfg()
        try:
            _headers = {"Authorization": f"Bearer {cfg.agent_auth_token}"} if cfg.agent_auth_token else {}
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.post(f"http://localhost:{cfg.agent_port}/refresh-info", headers=_headers)
            if r.status_code < 300:
                return r.json()
            return {"status": "error", "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # -- Root --

    @router.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(Path(__file__).parent / "static" / "index.html"))

    add_config_routes(router, Path(__file__).resolve().parent, _require_admin, cookie_name="kb_session")

    return router
