"""Alembic env — async-aware migration runner for bp_router.

The DB URL is read from `ROUTER_DB_URL` so deployments don't have to
keep alembic.ini and the runtime config in sync.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from env so we don't keep secrets in alembic.ini
db_url = os.environ.get("ROUTER_DB_URL")
if db_url:
    # Alembic uses sqlalchemy URLs; coerce postgresql:// → postgresql+asyncpg://
    if db_url.startswith("postgresql://"):
        db_url = "postgresql+asyncpg://" + db_url[len("postgresql://"):]
    config.set_main_option("sqlalchemy.url", db_url)

# No SQLAlchemy MetaData in this project (we use raw SQL via asyncpg).
# Alembic still needs target_metadata; pass None and rely on op.execute().
target_metadata = None


def run_migrations_offline() -> None:
    """Generate SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
