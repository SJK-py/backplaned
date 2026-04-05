"""
kb_agent/db.py — Per-user LanceDB knowledge base with chunking and hybrid search.

Each user gets a separate LanceDB table under ``data/lancedb/{user_id}/``.
Documents are chunked using hierarchical recursive splitting, embedded via
an OpenAI-compatible embedding API, and stored with metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Optional

import lancedb
from openai import OpenAI

logger = logging.getLogger("kb_agent.db")


def _esc(value: str) -> str:
    """Escape single quotes for LanceDB SQL filter strings."""
    return value.replace("'", "''")


def _esc_like(value: str) -> str:
    """Escape single quotes and LIKE wildcards for LanceDB LIKE filters."""
    return _esc(value).replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# Chunking (ported from reference bot-database.py)
# ---------------------------------------------------------------------------

DIV_HIERARCHY = ["header", "2newline", "newline", "word", "char"]
DIV_MAP = {
    "header": "\n#",
    "2newline": "\n\n",
    "newline": "\n",
    "word": " ",
    "char": "",
}


def get_splits(
    text: str, div_name: str, chunk_max: int,
) -> list[str]:
    """Recursively split text at hierarchy level until chunks fit."""
    if not text:
        return []
    if len(text) <= chunk_max:
        return [text]

    idx = DIV_HIERARCHY.index(div_name) if div_name in DIV_HIERARCHY else len(DIV_HIERARCHY) - 1

    if idx == len(DIV_HIERARCHY) - 1:
        return [text[i:i + chunk_max] for i in range(0, len(text), chunk_max)]

    sep = DIV_MAP[div_name]
    parts = text.split(sep)
    if len(parts) > 1:
        parts = [parts[0]] + [sep + p for p in parts[1:]]

    final: list[str] = []
    next_div = DIV_HIERARCHY[idx + 1]
    for p in parts:
        if len(p) <= chunk_max:
            final.append(p)
        else:
            final.extend(get_splits(p, next_div, chunk_max))
    return final


def chunk_text(
    text: str,
    chunk_max: int = 2000,
    chunk_min: int = 1000,
    chunk_overlap: int = 100,
    start_div: str = "header",
) -> list[str]:
    """Split text into overlapping chunks using hierarchical splitting."""
    base_splits = get_splits(text, start_div, chunk_max)
    chunks: list[str] = []
    current = ""

    for split in base_splits:
        if len(current) + len(split) <= chunk_max:
            current += split
        else:
            if len(current) >= chunk_min:
                chunks.append(current.strip())
            if chunk_overlap > 0 and len(current) > chunk_overlap:
                current = current[-chunk_overlap:] + split
            else:
                current = split

    if current:
        if len(current) >= chunk_min or not chunks:
            chunks.append(current.strip())
        elif len(chunks[-1]) + len(current) <= chunk_max:
            # Tail too short for its own chunk — merge into the last one.
            chunks[-1] = chunks[-1] + "\n" + current.strip()
        else:
            # Merging would exceed max — keep tail as its own small chunk.
            chunks.append(current.strip())

    return chunks


# ---------------------------------------------------------------------------
# KnowledgeDB — per-user LanceDB wrapper
# ---------------------------------------------------------------------------


class KnowledgeDB:
    """Per-user knowledge base backed by LanceDB with hybrid search."""

    def __init__(
        self,
        base_dir: str | Path,
        embed_base_url: str,
        embed_api_key: str,
        embed_model: str,
        embed_timeout: float,
        vector_dim: int,
        chunk_max: int = 2000,
        chunk_min: int = 1000,
        chunk_overlap: int = 100,
    ) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._embed_base_url = embed_base_url
        self._embed_api_key = embed_api_key
        self._embed_model = embed_model
        self._embed_timeout = embed_timeout
        self._vector_dim = vector_dim
        self._chunk_max = chunk_max
        self._chunk_min = chunk_min
        self._chunk_overlap = chunk_overlap
        self._user_locks: dict[str, asyncio.Lock] = {}

    def _user_db_path(self, user_id: str) -> str:
        p = (self._base / user_id).resolve()
        base_resolved = str(self._base.resolve())
        if not str(p).startswith(base_resolved + "/"):
            raise ValueError("Invalid user_id: path traversal blocked")
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def _get_table(self, user_id: str):
        """Get or create the user's LanceDB table."""
        db = lancedb.connect(self._user_db_path(user_id))
        try:
            return db.open_table("documents")
        except Exception:
            import pyarrow as pa
            schema = pa.schema([
                pa.field("chunk_id", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._vector_dim)),
                pa.field("text", pa.string()),
                pa.field("title", pa.string()),
                pa.field("collection", pa.string()),
                pa.field("date", pa.string()),
                pa.field("description", pa.string()),
                pa.field("tags", pa.string()),  # JSON array string
            ])
            return db.create_table("documents", schema=schema)

    def _get_embed_client(self) -> OpenAI:
        return OpenAI(
            base_url=self._embed_base_url,
            api_key=self._embed_api_key,
            timeout=self._embed_timeout,
        )

    def _embed(self, text: str) -> list[float]:
        """Synchronous embedding call."""
        client = self._get_embed_client()
        res = client.embeddings.create(input=text, model=self._embed_model)
        return res.data[0].embedding

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    async def store_document(
        self,
        user_id: str,
        text: str,
        title: str,
        collection: str = "default",
        description: str = "",
        date: str = "",
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Chunk, embed, and store a markdown document."""
        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            return await self._store_document_locked(
                user_id, text, title, collection, description, date, tags,
            )

    async def _store_document_locked(
        self,
        user_id: str,
        text: str,
        title: str,
        collection: str,
        description: str,
        date: str,
        tags: Optional[list[str]],
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        table = self._get_table(user_id)

        # Deduplicate title (under lock to prevent races)
        final_title = title
        try:
            df = table.search().select(["title"]).limit(10000).to_pandas()
            existing = set(df["title"].unique()) if not df.empty else set()
            counter = 1
            while final_title in existing:
                final_title = f"{title}_{counter}"
                counter += 1
        except Exception:
            pass

        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tags_json = json.dumps(tags or [])

        chunks = chunk_text(
            text, self._chunk_max, self._chunk_min, self._chunk_overlap,
        )
        if not chunks:
            return {"status": "error", "detail": "Document produced no chunks"}

        # Embed in thread pool
        records = []
        for c_text in chunks:
            vector = await loop.run_in_executor(None, partial(self._embed, c_text))
            records.append({
                "chunk_id": uuid.uuid4().hex[:12],
                "vector": vector,
                "text": c_text,
                "title": final_title,
                "collection": collection,
                "date": date,
                "description": description,
                "tags": tags_json,
            })

        table.add(records)

        # Rebuild FTS index
        try:
            table.create_fts_index("text", replace=True)
        except Exception as exc:
            logger.warning("FTS index rebuild: %s", exc)

        logger.info("Stored '%s' for user %s (%d chunks)", final_title, user_id, len(chunks))
        return {
            "status": "stored",
            "title": final_title,
            "collection": collection,
            "chunks": len(chunks),
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        user_id: str,
        query: str,
        count: int = 5,
        collection: Optional[str] = None,
        title: Optional[str] = None,
        tag: Optional[str] = None,
        date_before: Optional[str] = None,
        date_after: Optional[str] = None,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Search the user's knowledge base. mode: hybrid | vector | fts."""
        loop = asyncio.get_running_loop()
        table = self._get_table(user_id)

        # Only compute embedding when needed (skip for pure full-text search).
        query_vector = None
        if mode != "fts":
            query_vector = await loop.run_in_executor(None, partial(self._embed, query))

        try:
            if mode == "hybrid":
                search_op = table.search(query_type="hybrid").vector(query_vector).text(query).limit(count)
            elif mode == "fts":
                search_op = table.search(query_type="fts").text(query).limit(count)
            else:
                search_op = table.search(query_vector).limit(count)

            filters: list[str] = []
            if collection:
                filters.append(f"`collection` = '{_esc(collection)}'")
            if title:
                filters.append(f"`title` = '{_esc(title)}'")
            if date_before:
                filters.append(f"`date` < '{_esc(date_before)}'")
            if date_after:
                filters.append(f"`date` > '{_esc(date_after)}'")
            if tag:
                filters.append(f"`tags` LIKE '%{_esc_like(tag)}%'")

            if filters:
                search_op = search_op.where(" AND ".join(filters))

            df = search_op.to_pandas()
        except Exception as exc:
            logger.warning("Search failed for user %s: %s", user_id, exc)
            return []

        if df.empty:
            return []

        score_col = "_relevance_score" if "_relevance_score" in df.columns else ("_score" if "_score" in df.columns else None)

        results = []
        for _, row in df.iterrows():
            r: dict[str, Any] = {
                "title": row.get("title", ""),
                "collection": row.get("collection", ""),
                "text": row.get("text", ""),
                "date": row.get("date", ""),
                "description": row.get("description", ""),
            }
            if score_col:
                r["score"] = float(row.get(score_col, 0))
            try:
                r["tags"] = json.loads(row.get("tags", "[]"))
            except Exception:
                r["tags"] = []
            results.append(r)

        return results

    # ------------------------------------------------------------------
    # List / Remove / Modify
    # ------------------------------------------------------------------

    async def list_documents(self, user_id: str) -> list[dict[str, Any]]:
        """List all unique documents for a user."""
        table = self._get_table(user_id)
        try:
            df = table.search().select([
                "title", "collection", "date", "description", "tags",
            ]).limit(100000).to_pandas()
        except Exception:
            return []

        if df.empty:
            return []

        unique = df.drop_duplicates(subset=["title"])
        docs = []
        for _, row in unique.iterrows():
            try:
                tags = json.loads(row.get("tags", "[]"))
            except Exception:
                tags = []
            docs.append({
                "title": row.get("title", ""),
                "collection": row.get("collection", ""),
                "date": row.get("date", ""),
                "description": row.get("description", ""),
                "tags": tags,
            })
        return docs

    async def remove_document(self, user_id: str, title: str) -> bool:
        """Remove all chunks of a document by title."""
        table = self._get_table(user_id)
        try:
            table.delete(f"`title` = '{_esc(title)}'")
            table.create_fts_index("text", replace=True)
            logger.info("Removed '%s' for user %s", title, user_id)
            return True
        except Exception as exc:
            logger.warning("Remove failed for '%s': %s", title, exc)
            return False

    async def modify_metadata(
        self,
        user_id: str,
        title: str,
        collection: Optional[str] = None,
        description: Optional[str] = None,
        date: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> bool:
        """Update metadata for all chunks of a document."""
        table = self._get_table(user_id)
        try:
            updates: dict[str, str] = {}
            if collection is not None:
                updates["collection"] = collection
            if description is not None:
                updates["description"] = description
            if date is not None:
                updates["date"] = date
            if tags is not None:
                updates["tags"] = json.dumps(tags)
            if not updates:
                return False
            table.update(where=f"`title` = '{_esc(title)}'", values=updates)
            return True
        except Exception as exc:
            logger.warning("Modify failed for '%s': %s", title, exc)
            return False

    def list_user_ids(self) -> list[str]:
        """Return all user_ids that have a knowledge base."""
        result = []
        if not self._base.exists():
            return result
        for d in self._base.iterdir():
            if d.is_dir() and (d / "documents.lance").exists():
                result.append(d.name)
        return result
