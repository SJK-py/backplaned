"""bp_router.storage.base — FileStore protocol.

All implementations are content-addressed by sha256: the same content
under different filenames stores once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterable, AsyncIterator, Optional, Protocol


@dataclass(frozen=True)
class FileMeta:
    sha256: str
    byte_size: int
    mime_type: Optional[str] = None
    original_filename: Optional[str] = None


class FileStore(Protocol):
    """Pluggable backend for ProxyFile bytes.

    Implementations: `LocalFileStore`, `S3FileStore`. Selection happens
    in `bp_router.storage.build_file_store` based on `settings.file_store`.

    The `sha256` argument is the canonical identifier. Implementations
    SHOULD verify that uploaded content matches the claimed hash.
    """

    backend_name: str
    """Short string used in metric labels (`local`, `s3`, ...)."""

    async def put(
        self,
        sha256: str,
        src: AsyncIterable[bytes],
        meta: FileMeta,
    ) -> str:
        """Stream content under the hash. Returns a backend-internal URL.

        The URL is opaque to callers and is stored in `files.storage_url`.
        Subsequent `open` / `presigned_url` / `delete` calls take the URL
        OR the sha256 — implementations may use either.
        """
        ...

    async def open(self, sha256: str) -> AsyncIterator[bytes]:
        """Stream the bytes of a stored object."""
        ...

    async def presigned_url(
        self, sha256: str, *, ttl_s: int
    ) -> Optional[str]:
        """Backend-direct URL with TTL, or None if unsupported.

        Returning a URL lets callers redirect clients straight to the
        backend, taking the router out of the byte path. Local
        filesystem returns None; S3/GCS/R2 return signed URLs.
        """
        ...

    async def delete(self, sha256: str) -> None:
        """Best-effort delete. Idempotent — missing object is not an error."""
        ...

    async def exists(self, sha256: str) -> bool:
        ...
