"""bp_router.storage — Pluggable file storage.

See `docs/router/storage.md` §2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bp_router.storage.base import FileMeta, FileStore

if TYPE_CHECKING:
    from bp_router.settings import Settings


def build_file_store(settings: "Settings") -> FileStore:
    """Construct the configured backend.

    Selection driven by `settings.file_store`. New backends are added by
    creating a `FileStore` implementation under `bp_router.storage` and
    extending this factory.
    """
    backend = settings.file_store
    if backend == "local":
        from bp_router.storage.local import LocalFileStore  # noqa: PLC0415

        return LocalFileStore.from_options(settings.file_store_options)
    if backend == "s3":
        from bp_router.storage.s3 import S3FileStore  # noqa: PLC0415

        return S3FileStore.from_options(settings.file_store_options)
    raise ValueError(f"Unsupported file_store backend: {backend!r}")


__all__ = ["FileMeta", "FileStore", "build_file_store"]
