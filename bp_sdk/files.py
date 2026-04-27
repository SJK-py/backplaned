"""bp_sdk.files — Per-task ProxyFileManager.

Evolution of helper.py:290-628. Same in/out model; tighter API:
fetch returns Path, put is explicit, no path-scanning heuristics.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterable, Optional, Union

import httpx

from bp_protocol.types import ProxyFile

if TYPE_CHECKING:
    from bp_sdk.context import TaskContext

logger = logging.getLogger(__name__)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class ProxyFileManager:
    """Per-task file lifecycle.

    `fetch`/`fetch_all` resolve incoming ProxyFiles to local paths.
    `put` uploads bytes/files to the router's storage backend and
    returns a ProxyFile reference suitable for inclusion in
    AgentOutput.files.
    """

    def __init__(
        self,
        ctx: "TaskContext",
        *,
        inbox_dir: Path,
        router_url: str,
        embedded: bool,
        auth_token: Optional[str] = None,
    ) -> None:
        self._ctx = ctx
        self._inbox_dir = inbox_dir
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        self._router_url = router_url.rstrip("/")
        self._embedded = embedded
        self._auth_token = auth_token
        self._registry: dict[Path, ProxyFile] = {}

    def _get_auth_token(self) -> Optional[str]:
        return self._auth_token

    # ------------------------------------------------------------------
    # Inbound: ProxyFile → local Path
    # ------------------------------------------------------------------

    async def fetch(self, pf: ProxyFile) -> Path:
        """Resolve a ProxyFile to a local Path.

        - localfile: returned as-is (no copy).
        - presigned / http: streamed to the inbox.
        - router-proxy: GET {router_url}{pf.path} with bearer auth.

        Caches by sha256 when known so repeat fetches inside a task
        don't re-download identical bytes.
        """
        if pf.protocol == "localfile":
            local = Path(pf.path)
            self._registry[local] = pf
            return local

        # Cached?
        if pf.sha256:
            for local, registered in self._registry.items():
                if registered.sha256 == pf.sha256 and local.exists():
                    return local

        filename = pf.original_filename or Path(pf.path.split("?")[0]).name or "file"
        dest = self._inbox_dir / filename
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}")

        if pf.protocol == "router-proxy":
            url = f"{self._router_url}{pf.path}"
            headers = {}
            token = self._get_auth_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif pf.protocol == "presigned" or pf.protocol == "http":
            url = pf.path
            headers = {}
        else:
            raise ValueError(f"unsupported ProxyFile protocol: {pf.protocol!r}")

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)

        if pf.sha256:
            actual = await asyncio.to_thread(_file_sha256, dest)
            if actual != pf.sha256:
                dest.unlink(missing_ok=True)
                raise ValueError(
                    f"sha256 mismatch on fetch: claimed {pf.sha256}, actual {actual}"
                )

        self._registry[dest] = pf
        return dest

    async def fetch_all(self, files: list[ProxyFile]) -> list[Path]:
        return [await self.fetch(f) for f in files]

    # ------------------------------------------------------------------
    # Outbound: bytes/Path → ProxyFile
    # ------------------------------------------------------------------

    async def put(
        self,
        src: Union[Path, bytes, AsyncIterable[bytes]],
        *,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> ProxyFile:
        """Upload to router storage and return a ProxyFile reference.

        For embedded agents, returns a `localfile` reference if `src`
        is already a Path on a shared filesystem — bytes are not copied.
        """
        if self._embedded and isinstance(src, Path):
            # Shared filesystem with the router process; no copy needed.
            sha = await asyncio.to_thread(_file_sha256, src)
            stat = src.stat()
            pf = ProxyFile(
                path=str(src.resolve()),
                protocol="localfile",
                sha256=sha,
                byte_size=stat.st_size,
                mime_type=mime_type or mimetypes.guess_type(str(src))[0],
                original_filename=filename or src.name,
            )
            self._registry[src] = pf
            return pf

        # External agent (or embedded with bytes/iterator): POST /v1/files.
        upload_path, sha, size, resolved_filename, mime = await self._materialise(
            src, filename, mime_type
        )

        token = self._get_auth_token()
        url = f"{self._router_url}/v1/files"
        params: dict[str, str] = {}
        if self._ctx.session_id:
            params["session_id"] = self._ctx.session_id
        if self._ctx.task_id and self._ctx.task_id != "<spawn>":
            params["task_id"] = self._ctx.task_id

        async with httpx.AsyncClient(timeout=300.0) as client:
            with upload_path.open("rb") as fh:
                files_field = {
                    "file": (resolved_filename, fh, mime or "application/octet-stream")
                }
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = await client.post(
                    url, files=files_field, params=params, headers=headers
                )
                resp.raise_for_status()
                data = resp.json()

        pf = ProxyFile(
            path=data["path"],
            protocol="router-proxy",
            sha256=data.get("sha256", sha),
            byte_size=int(data.get("byte_size", size)),
            mime_type=mime,
            original_filename=resolved_filename,
        )
        self._registry[upload_path] = pf
        return pf

    async def _materialise(
        self,
        src: Union[Path, bytes, AsyncIterable[bytes]],
        filename: Optional[str],
        mime_type: Optional[str],
    ) -> tuple[Path, str, int, str, Optional[str]]:
        """Normalise `src` to (path_on_disk, sha256, size, filename, mime).

        For Path, returns the path unchanged after hashing. For bytes /
        iterable[bytes], writes to a unique inbox path first.
        """
        if isinstance(src, Path):
            sha = await asyncio.to_thread(_file_sha256, src)
            size = src.stat().st_size
            return (
                src,
                sha,
                size,
                filename or src.name,
                mime_type or mimetypes.guess_type(str(src))[0],
            )

        # bytes or iterable
        out = self._inbox_dir / f"upload_{uuid.uuid4().hex}.bin"
        h = hashlib.sha256()
        size = 0

        if isinstance(src, bytes):
            h.update(src)
            size = len(src)
            out.write_bytes(src)
        else:
            async for chunk in src:
                h.update(chunk)
                size += len(chunk)
                with out.open("ab") as fh:
                    fh.write(chunk)

        return (
            out,
            h.hexdigest(),
            size,
            filename or out.name,
            mime_type,
        )

    # ------------------------------------------------------------------
    # Lookup / cleanup
    # ------------------------------------------------------------------

    def resolve(self, local_path: Union[Path, str]) -> Optional[ProxyFile]:
        return self._registry.get(Path(local_path))

    def list(self) -> dict[Path, ProxyFile]:
        return dict(self._registry)

    async def cleanup(self) -> None:
        """Delete the inbox directory. Called by the dispatcher when the
        task terminates."""
        try:
            await asyncio.to_thread(shutil.rmtree, self._inbox_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            logger.debug("inbox cleanup failed", exc_info=True)
