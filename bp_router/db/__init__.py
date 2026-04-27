"""bp_router.db — Postgres + Redis access layer.

Postgres via asyncpg, Redis via redis.asyncio. Migrations managed by
Alembic (`bp_router/db/migrations/`).

Schema is defined by Alembic migrations; `models.py` contains row
dataclasses, `queries.py` contains the query helpers. The `WHERE
user_id = ?` scoping invariant for user-owned rows is enforced via
the `Scope` helper in `queries.py`.
"""
