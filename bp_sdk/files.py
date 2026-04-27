"""bp_sdk.files — Per-task ProxyFileManager.

Evolution of helper.py:290-628. Same in/out model; tighter API:
fetch returns Path, put is explicit, no path-scanning heuristics.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterable, Optional, Union

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
    ) -> None:
        self._ctx = ctx
        self._inbox_dir = inbox_dir
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        self._router_url = router_url.rstrip("/")
        self._embedded = embedded
        self._registry: dict[Path, ProxyFile] = {}

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def fetch(self, pf: ProxyFile) -> Path:
        """Resolve a ProxyFile to a local Path. Caches by sha256."""
        raise NotImplementedError

    async def fetch_all(self, files: list[ProxyFile]) -> list[Path]:
        return [await self.fetch(f) for f in files]

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def put(
        self,
        src: Union[Path, bytes, AsyncIterable[bytes]],
        *,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> ProxyFile:
        """Upload to router storage and return a ProxyFile reference.

        For embedded agents on a shared filesystem, returns a
        `localfile` reference — bytes are not copied. For external
        agents, streams to `POST /v1/files` (or a presigned URL when
        the router advertises one).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Lookup / cleanup
    # ------------------------------------------------------------------

    def resolve(self, local_path: Union[Path, str]) -> Optional[ProxyFile]:
        return self._registry.get(Path(local_path))

    def list(self) -> dict[Path, ProxyFile]:
        return dict(self._registry)

    async def cleanup(self) -> None:
        """Delete inbox files. Called by the dispatcher on task completion."""
        raise NotImplementedError
