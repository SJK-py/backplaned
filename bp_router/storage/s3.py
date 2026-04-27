"""bp_router.storage.s3 — S3-compatible FileStore (skeleton).

Works against AWS S3, R2, MinIO, etc. Uses aioboto3 (deferred import).

Required `file_store_options`:
    bucket:           str
    region_name:      str | null
    endpoint_url:     str | null   (R2/MinIO)
    access_key_id:    str | null   (else AWS default chain)
    secret_access_key:str | null
    prefix:           str          (key prefix; default "")
"""

from __future__ import annotations

from typing import AsyncIterable, AsyncIterator, Optional

from bp_router.storage.base import FileMeta, FileStore


class S3FileStore(FileStore):
    backend_name = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        region_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        prefix: str = "",
    ) -> None:
        self.bucket = bucket
        self.region_name = region_name
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.prefix = prefix.rstrip("/")

    @classmethod
    def from_options(cls, options: dict) -> "S3FileStore":
        return cls(
            bucket=options["bucket"],
            region_name=options.get("region_name"),
            endpoint_url=options.get("endpoint_url"),
            access_key_id=options.get("access_key_id"),
            secret_access_key=options.get("secret_access_key"),
            prefix=options.get("prefix", ""),
        )

    # ------------------------------------------------------------------

    def _key(self, sha256: str) -> str:
        sub = f"{sha256[:2]}/{sha256[2:4]}/{sha256}"
        return f"{self.prefix}/{sub}" if self.prefix else sub

    async def put(
        self, sha256: str, src: AsyncIterable[bytes], meta: FileMeta
    ) -> str:
        # Implementation note: use multipart upload via aioboto3, validate
        # sha256 against streamed content, then upload. Returns
        # "s3://<bucket>/<key>".
        raise NotImplementedError

    async def open(self, sha256: str) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def presigned_url(self, sha256: str, *, ttl_s: int) -> Optional[str]:
        # `generate_presigned_url(ClientMethod="get_object", ...)` with
        # ExpiresIn=ttl_s.
        raise NotImplementedError

    async def delete(self, sha256: str) -> None:
        raise NotImplementedError

    async def exists(self, sha256: str) -> bool:
        raise NotImplementedError
