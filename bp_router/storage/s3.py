"""bp_router.storage.s3 — S3-compatible FileStore.

Works against AWS S3, Cloudflare R2, MinIO, etc. via aioboto3.

Required `file_store_options`:
    bucket:           str
Optional:
    region_name:      str
    endpoint_url:     str   (R2/MinIO)
    access_key_id:    str
    secret_access_key:str
    prefix:           str   (key prefix; default "")
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, AsyncIterable, AsyncIterator, Optional

from bp_router.storage.base import FileMeta, FileStore

logger = logging.getLogger(__name__)


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
        self._session: Optional[Any] = None

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

    def _session_factory(self):  # type: ignore[no-untyped-def]
        if self._session is None:
            try:
                import aioboto3  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "aioboto3 not installed; "
                    "`pip install backplaned[storage-s3]`"
                ) from exc
            self._session = aioboto3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region_name,
            )
        return self._session

    def _client(self):  # type: ignore[no-untyped-def]
        session = self._session_factory()
        return session.client("s3", endpoint_url=self.endpoint_url)

    # ------------------------------------------------------------------
    # put
    # ------------------------------------------------------------------

    async def put(
        self,
        sha256: str,
        src: AsyncIterable[bytes],
        meta: FileMeta,
    ) -> str:
        """Upload to s3://bucket/key with sha256 verification.

        For now: buffers into memory and PUTs in one shot. Multipart
        upload (for large files) is a TODO.
        """
        h = hashlib.sha256()
        size = 0
        chunks: list[bytes] = []
        async for chunk in src:
            h.update(chunk)
            size += len(chunk)
            chunks.append(chunk)

        actual = h.hexdigest()
        if actual != sha256:
            raise ValueError(
                f"sha256 mismatch on s3 put: claimed {sha256}, actual {actual}"
            )
        if meta.byte_size > 0 and size != meta.byte_size:
            raise ValueError(
                f"size mismatch on s3 put: claimed {meta.byte_size}, actual {size}"
            )

        body = b"".join(chunks)
        key = self._key(sha256)
        async with self._client() as s3:
            put_kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": key,
                "Body": body,
            }
            if meta.mime_type:
                put_kwargs["ContentType"] = meta.mime_type
            await s3.put_object(**put_kwargs)
        return f"s3://{self.bucket}/{key}"

    # ------------------------------------------------------------------
    # open
    # ------------------------------------------------------------------

    async def open(self, sha256: str) -> AsyncIterator[bytes]:
        key = self._key(sha256)

        async def _gen() -> AsyncIterator[bytes]:
            async with self._client() as s3:
                resp = await s3.get_object(Bucket=self.bucket, Key=key)
                async for chunk in resp["Body"].iter_chunks(chunk_size=64 * 1024):
                    yield chunk

        return _gen()

    # ------------------------------------------------------------------
    # presigned_url
    # ------------------------------------------------------------------

    async def presigned_url(self, sha256: str, *, ttl_s: int) -> Optional[str]:
        key = self._key(sha256)
        async with self._client() as s3:
            url = await s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=ttl_s,
            )
        return url

    # ------------------------------------------------------------------
    # delete / exists
    # ------------------------------------------------------------------

    async def delete(self, sha256: str) -> None:
        key = self._key(sha256)
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001
            logger.debug("s3 delete failed (may be missing)", exc_info=True)

    async def exists(self, sha256: str) -> bool:
        key = self._key(sha256)
        try:
            async with self._client() as s3:
                await s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False
