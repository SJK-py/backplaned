"""bp_router — Reworked Backplaned router.

Stack: FastAPI (asyncio), asyncpg (Postgres), Redis (ephemeral state),
WebSockets (agent transport), HTTP (files + admin).

See `docs/overview.md` for architectural rationale and
`docs/router/` for the full design.
"""

__version__ = "0.1.0"
