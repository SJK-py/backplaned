"""
agents/memory_agent/memory_store.py — Self-contained memory store.

Each user gets an isolated LanceDB database at ``{db_base}/{user_id}/``
with a single ``memories`` table inside.  This mirrors kb_agent's per-user
isolation pattern and avoids cross-user data leakage.

The two-pass algorithm mirrors mem0's core logic:

  Pass 1 (Fact Extraction):
    Send conversation text to the LLM to extract discrete, atomic facts.

  Pass 2 (Memory Consolidation):
    For each new fact, vector-search for similar existing memories, then ask
    the LLM to classify each as ADD / UPDATE / DELETE / NONE.  Execute the
    resulting operations against the user's LanceDB table.

Search uses LanceDB hybrid mode (vector + BM25 full-text) for best recall.

LLM calls are made via an injectable callable so the agent layer can route
them through llm_agent (with per-user model control).  Embedding calls are
made directly for latency reasons.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import lancedb
import pyarrow as pa
from openai import OpenAI

logger = logging.getLogger(__name__)

# Type alias: (system_prompt, user_message) -> raw LLM text
LLMCallable = Callable[[str, str], str]

# ---------------------------------------------------------------------------
# LLM prompt templates
# ---------------------------------------------------------------------------

FACT_EXTRACTION_PROMPT = """You extract facts about a user from their conversation with an AI agent.
The user's identifier is "{user_id}".

Rules:
1. Every fact MUST be a self-contained sentence with a clear subject.
2. Merge related details into one fact. Do not split them.
3. Read both user and AI agent messages for context, but only record facts about the user.
4. Preserve names, dates, places, technologies, and relationships.
5. Capture changes explicitly (e.g. "switched from X to Y", "no longer does X").
6. If the user's real name is mentioned, lead with it. Otherwise use their identifier.
7. Today's date is {today}. Convert relative dates (e.g. "yesterday", "next week") to absolute dates.
8. If nothing worth remembering is found, return an empty list.
9. Detect the language of the user's messages and record facts in the same language.

Return ONLY: {{"facts": ["...", "..."]}}

Examples:

Conversation:
[alice] I just moved to Tokyo last month from Seoul where I lived 5 years.
[AI agent] How are you finding it?
[alice] Loving the food. I started a new job at LINE as a frontend engineer.
Output: {{"facts": ["Alice recently moved from Seoul to Tokyo and is enjoying the food scene", "Alice started a new job at LINE as a frontend engineer"]}}

Conversation:
[bob99] We're rewriting our backend in Rust, was using Go before.
[AI agent] What framework are you using?
[bob99] Axum. Our team is 4 engineers at Acme Corp.
Output: {{"facts": ["bob99 works at Acme Corp in a 4-person engineering team that is rewriting their backend from Go to Rust using Axum"]}}

Conversation:
[charlie] I used to love Python but TypeScript won me over for web dev.
[AI agent] What changed your mind?
[charlie] Next.js productivity. I still use Python for ML projects though.
Output: {{"facts": ["Charlie switched from Python to TypeScript for web development, favoring Next.js, but still uses Python for ML"]}}

Conversation:
[user42] What's the capital of France?
[AI agent] The capital of France is Paris. Paris is located in northern France on the Seine River. It has a population of about 2.1 million in the city proper and over 12 million in the metropolitan area. Paris is known for landmarks like the Eiffel Tower, the Louvre Museum, and Notre-Dame Cathedral. It has been the capital since the 10th century and serves as the country's political, economic, and cultural center.
[user42] Thanks!
Output: {{"facts": []}}

Conversation:
[dana] Meeting with David from marketing tomorrow at 2pm about Q3 budget.
[AI agent] Need help preparing?
[dana] No thanks. BTW I've been on keto for 3 months, lost 5kg so far.
Output: {{"facts": ["Dana has a meeting with David from marketing about Q3 budget on {tomorrow}", "Dana has been on a keto diet for 3 months and has lost 5kg"]}}
"""

MEMORY_UPDATE_PROMPT = """You are a smart memory manager which controls the memory of a system.
You can perform four operations: (1) ADD into memory, (2) UPDATE memory, (3) DELETE from memory, and (4) NONE (no change).

Guidelines:

1. **ADD**: New information not present in memory. Use a new sequential ID.
2. **UPDATE**: Same topic but different/more detailed info — rewrite the text, keep the same ID. If two facts convey the same thing, keep the one with more information.
3. **DELETE**: New fact contradicts an existing memory.
4. **NONE**: Information already present, no change needed.

Example — ADD:
  Old Memory: [{{"id": "0", "text": "User is a software engineer"}}]
  New facts: ["Name is John"]
  Result: [{{"id": "0", "text": "User is a software engineer", "event": "NONE"}}, {{"id": "1", "text": "Name is John", "event": "ADD"}}]

Example — UPDATE:
  Old Memory: [{{"id": "0", "text": "Likes to play cricket"}}]
  New facts: ["Loves to play cricket with friends"]
  Result: [{{"id": "0", "text": "Loves to play cricket with friends", "event": "UPDATE", "old_memory": "Likes to play cricket"}}]

Example — DELETE:
  Old Memory: [{{"id": "0", "text": "Loves cheese pizza"}}]
  New facts: ["Dislikes cheese pizza"]
  Result: [{{"id": "0", "text": "Loves cheese pizza", "event": "DELETE"}}]

Example — NONE:
  Old Memory: [{{"id": "0", "text": "Name is John"}}]
  New facts: ["Name is John"]
  Result: [{{"id": "0", "text": "Name is John", "event": "NONE"}}]

---

{memory_section}

The new retrieved facts are:
```
{new_facts}
```

Return ONLY valid JSON in this exact structure:
{{
    "memory": [
        {{
            "id": "<ID>",
            "text": "<Content>",
            "event": "ADD|UPDATE|DELETE|NONE",
            "old_memory": "<old text, only if UPDATE>"
        }}
    ]
}}
"""

_TABLE_NAME = "memories"


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """Per-user memory store backed by LanceDB with hybrid search.

    Each user gets an isolated LanceDB database at ``{db_base}/{user_id}/``.
    """

    def __init__(
        self,
        *,
        db_base: str | Path,
        llm_fn: LLMCallable,
        embed_base_url: str,
        embed_api_key: str,
        embed_model: str,
        embedding_dims: int = 768,
    ) -> None:
        self._db_base = Path(db_base).resolve()
        self._db_base.mkdir(parents=True, exist_ok=True)
        self._embedding_dims = embedding_dims

        # Injectable LLM callable (routed through llm_agent)
        self._llm_fn = llm_fn

        # Embedding client (direct for latency)
        self._embedder = OpenAI(base_url=embed_base_url, api_key=embed_api_key)
        self._embed_model = embed_model

    # ------------------------------------------------------------------
    # Per-user LanceDB helpers
    # ------------------------------------------------------------------

    def _user_db_path(self, user_id: str) -> str:
        """Return the resolved path for a user's LanceDB database."""
        p = (self._db_base / user_id).resolve()
        if not str(p).startswith(str(self._db_base) + "/"):
            raise ValueError("Invalid user_id: path traversal blocked")
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def _get_table(self, user_id: str) -> lancedb.table.Table:
        """Get or create the user's memories table."""
        db = lancedb.connect(self._user_db_path(user_id))
        try:
            return db.open_table(_TABLE_NAME)
        except Exception:
            return db.create_table(_TABLE_NAME, schema=self._make_schema())

    def _make_schema(self) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("created_at", pa.float64()),
                pa.field("updated_at", pa.float64()),
                pa.field("vector", pa.list_(pa.float32(), self._embedding_dims)),
            ]
        )

    @staticmethod
    def _rebuild_fts_index(table: lancedb.table.Table) -> None:
        """Rebuild the BM25 full-text search index on the text column."""
        try:
            table.create_fts_index("text", replace=True)
        except Exception as exc:
            logger.warning("FTS index rebuild: %s", exc)

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for a batch of texts."""
        resp = self._embedder.embeddings.create(input=texts, model=self._embed_model)
        return [d.embedding for d in resp.data]

    def _embed_one(self, text: str) -> list[float]:
        return self._embed([text])[0]

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    def _llm_call(self, system: str, user: str) -> str:
        """Call LLM via the injected callable."""
        return self._llm_fn(system, user)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Robustly parse JSON from LLM output."""
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        logger.warning("Failed to parse LLM JSON output: %s", text[:200])
        return {}

    # ------------------------------------------------------------------
    # Pass 1 — Fact extraction
    # ------------------------------------------------------------------

    def _extract_facts(self, content: str, user_id: str) -> list[str]:
        """Use the LLM to extract atomic facts from conversation text."""
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        system = FACT_EXTRACTION_PROMPT.format(
            today=today, tomorrow=tomorrow, user_id=user_id,
        )
        raw = self._llm_call(system, content)
        parsed = self._parse_json(raw)

        facts = parsed.get("facts", [])
        result: list[str] = []
        for f in facts:
            if isinstance(f, str) and f.strip():
                result.append(f.strip())
            elif isinstance(f, dict) and "fact" in f:
                result.append(str(f["fact"]).strip())
        return result

    # ------------------------------------------------------------------
    # Pass 2 — Memory consolidation (dedup / update / delete)
    # ------------------------------------------------------------------

    def _find_similar(
        self, text: str, table: lancedb.table.Table, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Vector search for existing memories similar to text."""
        try:
            if table.count_rows() == 0:
                return []
        except Exception:
            return []

        vec = self._embed_one(text)
        try:
            return table.search(vec).limit(limit).to_list()
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return []

    def _consolidate(
        self, facts: list[str], table: lancedb.table.Table
    ) -> list[dict[str, Any]]:
        """Compare new facts against existing memories via LLM."""
        # Gather candidate existing memories for all facts
        candidates: dict[str, dict[str, Any]] = {}  # id -> record
        for fact in facts:
            for row in self._find_similar(fact, table, limit=5):
                rid = row["id"]
                if rid not in candidates:
                    candidates[rid] = row

        # Build old memory list with sequential integer IDs
        # (prevents LLM from hallucinating UUIDs)
        idx_to_uuid: dict[str, str] = {}
        old_memory_list: list[dict[str, str]] = []
        for i, (uid, row) in enumerate(candidates.items()):
            idx = str(i)
            idx_to_uuid[idx] = uid
            old_memory_list.append({"id": idx, "text": row["text"]})

        # Build prompt
        if old_memory_list:
            memory_section = (
                "Below is the current content of memory:\n```\n"
                + json.dumps(old_memory_list, ensure_ascii=False)
                + "\n```"
            )
        else:
            memory_section = "Current memory is empty."

        system = MEMORY_UPDATE_PROMPT.format(
            memory_section=memory_section,
            new_facts=json.dumps(facts, ensure_ascii=False),
        )
        raw = self._llm_call(system, "Analyze and return the memory operations.")
        parsed = self._parse_json(raw)
        operations = parsed.get("memory", [])

        # Map sequential IDs back to UUIDs
        for op in operations:
            op_id = str(op.get("id", ""))
            if op_id in idx_to_uuid:
                op["_uuid"] = idx_to_uuid[op_id]
            else:
                op["_uuid"] = None  # new memory, will get a UUID on ADD
        return operations

    # ------------------------------------------------------------------
    # Execute consolidation operations
    # ------------------------------------------------------------------

    def _execute_operations(
        self, operations: list[dict[str, Any]], table: lancedb.table.Table
    ) -> dict[str, int]:
        """Apply ADD/UPDATE/DELETE/NONE operations to the user's table."""
        stats = {"add": 0, "update": 0, "delete": 0, "none": 0}
        now = time.time()
        mutated = False

        for op in operations:
            event = str(op.get("event", "NONE")).upper()
            text = str(op.get("text", "")).strip()
            existing_uuid = op.get("_uuid")

            if event == "ADD" and text:
                vec = self._embed_one(text)
                new_id = uuid.uuid4().hex
                table.add(
                    [
                        {
                            "id": new_id,
                            "text": text,
                            "created_at": now,
                            "updated_at": now,
                            "vector": vec,
                        }
                    ]
                )
                stats["add"] += 1
                mutated = True
                logger.debug("ADD memory: %s", text[:80])

            elif event == "UPDATE" and text and existing_uuid:
                vec = self._embed_one(text)
                try:
                    table.update(
                        where=f"id = '{existing_uuid}'",
                        values={
                            "text": text,
                            "updated_at": now,
                            "vector": vec,
                        },
                    )
                    stats["update"] += 1
                    mutated = True
                    logger.debug(
                        "UPDATE memory %s: %s -> %s",
                        existing_uuid,
                        op.get("old_memory", "?")[:40],
                        text[:40],
                    )
                except Exception as exc:
                    logger.warning("UPDATE failed for %s: %s", existing_uuid, exc)

            elif event == "DELETE" and existing_uuid:
                try:
                    table.delete(f"id = '{existing_uuid}'")
                    stats["delete"] += 1
                    mutated = True
                    logger.debug("DELETE memory %s", existing_uuid)
                except Exception as exc:
                    logger.warning("DELETE failed for %s: %s", existing_uuid, exc)

            else:
                stats["none"] += 1

        # Rebuild FTS index when data changed
        if mutated:
            self._rebuild_fts_index(table)

        return stats

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, content: str, *, user_id: str) -> dict[str, Any]:
        """Ingest content: extract facts, consolidate with existing memories, store.

        Returns a summary dict with operation counts.
        """
        facts = self._extract_facts(content, user_id)
        if not facts:
            logger.info("No facts extracted from content for user %s", user_id)
            return {"facts_extracted": 0, "operations": {"add": 0, "update": 0, "delete": 0, "none": 0}}

        logger.info("Extracted %d facts for user %s: %s", len(facts), user_id, facts)
        table = self._get_table(user_id)
        operations = self._consolidate(facts, table)
        stats = self._execute_operations(operations, table)
        logger.info("Memory consolidation for user %s: %s", user_id, stats)
        return {"facts_extracted": len(facts), "operations": stats}

    def search(self, query: str, *, user_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """Hybrid search (vector + BM25) over memories for a user.

        Returns a list of dicts with 'id', 'memory', 'created_at',
        'updated_at', and 'score'.
        """
        table = self._get_table(user_id)
        try:
            if table.count_rows() == 0:
                return []
        except Exception:
            return []

        vec = self._embed_one(query)
        try:
            results = (
                table.search(query_type="hybrid")
                .vector(vec)
                .text(query)
                .limit(limit)
                .to_list()
            )
        except Exception:
            # FTS index may not exist yet — fall back to vector-only search
            logger.debug("Hybrid search unavailable, falling back to vector search")
            try:
                results = table.search(vec).limit(limit).to_list()
            except Exception as exc:
                logger.error("Search failed: %s", exc)
                return []

        # Determine score column (LanceDB uses different names)
        score_col = None
        if results:
            first = results[0]
            for col in ("_relevance_score", "_score", "_distance"):
                if col in first:
                    score_col = col
                    break

        out: list[dict[str, Any]] = []
        for r in results:
            score = 0.0
            if score_col:
                raw = float(r.get(score_col, 0.0))
                # _distance is lower-is-better; _relevance_score is higher-is-better
                score = (1.0 - raw) if score_col == "_distance" else raw
            out.append(
                {
                    "id": r["id"],
                    "memory": r["text"],
                    "created_at": r.get("created_at"),
                    "updated_at": r.get("updated_at"),
                    "score": score,
                }
            )
        return out
