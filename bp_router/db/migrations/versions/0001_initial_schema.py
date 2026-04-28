"""initial schema

Creates the full set of tables defined in
docs/router/storage.md §1.1: users, sessions, agents, tasks,
task_events, files, acl_rules, audit_log, invitations,
auth_refresh_tokens.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE users (
            user_id            text PRIMARY KEY,
            role               text NOT NULL CHECK (role IN ('admin','user','service')),
            user_tier          text NOT NULL DEFAULT 'free',
            auth_kind          text NOT NULL CHECK (auth_kind IN ('password','oidc','api_key')),
            auth_secret_hash   text,
            email              text UNIQUE,
            created_at         timestamptz NOT NULL DEFAULT now(),
            suspended_at       timestamptz
        )
    """)
    op.execute("CREATE INDEX users_role_idx ON users(role)")
    op.execute("CREATE INDEX users_tier_idx ON users(user_tier)")

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE sessions (
            session_id   text PRIMARY KEY,
            user_id      text NOT NULL REFERENCES users(user_id),
            opened_at    timestamptz NOT NULL DEFAULT now(),
            closed_at    timestamptz,
            metadata     jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX sessions_user_idx ON sessions(user_id, opened_at DESC)")

    # ------------------------------------------------------------------
    # agents
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE agents (
            agent_id              text PRIMARY KEY,
            kind                  text NOT NULL CHECK (kind IN ('external','embedded')),
            status                text NOT NULL CHECK (status IN ('active','suspended','pending')),
            capabilities          jsonb NOT NULL DEFAULT '[]'::jsonb,
            requires_capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
            tags                  jsonb NOT NULL DEFAULT '[]'::jsonb,
            agent_info            jsonb NOT NULL DEFAULT '{}'::jsonb,
            auth_token_hash       text,
            public_key            text,
            registered_at         timestamptz NOT NULL DEFAULT now(),
            last_seen_at          timestamptz
        )
    """)
    op.execute("CREATE INDEX agents_status_idx ON agents(status)")
    op.execute("CREATE INDEX agents_capabilities_idx ON agents USING gin (capabilities)")
    op.execute("CREATE INDEX agents_tags_idx ON agents USING gin (tags)")

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE tasks (
            task_id          text PRIMARY KEY,
            parent_task_id   text REFERENCES tasks(task_id),
            root_task_id     text NOT NULL,
            user_id          text NOT NULL REFERENCES users(user_id),
            session_id       text NOT NULL REFERENCES sessions(session_id),
            agent_id         text NOT NULL REFERENCES agents(agent_id),
            state            text NOT NULL CHECK (state IN (
                'QUEUED','RUNNING','WAITING_CHILDREN',
                'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'
            )),
            status_code      int,
            idempotency_key  text,
            priority         text NOT NULL DEFAULT 'normal',
            deadline         timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            input            jsonb NOT NULL DEFAULT '{}'::jsonb,
            output           jsonb,
            error            jsonb,
            CONSTRAINT tasks_idempotency_unique UNIQUE (user_id, idempotency_key),
            CONSTRAINT tasks_deadline_after_create CHECK (deadline IS NULL OR deadline > created_at)
        )
    """)
    op.execute("CREATE INDEX tasks_user_state_idx ON tasks(user_id, state)")
    op.execute("CREATE INDEX tasks_session_idx ON tasks(session_id, created_at DESC)")
    op.execute("CREATE INDEX tasks_parent_idx ON tasks(parent_task_id)")
    op.execute("""
        CREATE INDEX tasks_active_idx ON tasks(state)
        WHERE state IN ('QUEUED','RUNNING','WAITING_CHILDREN')
    """)

    # ------------------------------------------------------------------
    # task_events
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE task_events (
            event_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_id          text NOT NULL REFERENCES tasks(task_id),
            ts               timestamptz NOT NULL DEFAULT now(),
            kind             text NOT NULL,
            actor_agent_id   text,
            from_state       text,
            to_state         text,
            payload          jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX task_events_task_ts_idx ON task_events(task_id, ts)")

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE files (
            file_id            text PRIMARY KEY,
            sha256             text NOT NULL,
            user_id            text NOT NULL REFERENCES users(user_id),
            session_id         text REFERENCES sessions(session_id),
            task_id            text REFERENCES tasks(task_id),
            byte_size          bigint NOT NULL,
            mime_type          text,
            storage_url        text NOT NULL,
            original_filename  text,
            created_at         timestamptz NOT NULL DEFAULT now(),
            expires_at         timestamptz
        )
    """)
    op.execute("CREATE UNIQUE INDEX files_user_sha_idx ON files(user_id, sha256)")
    op.execute("CREATE INDEX files_expires_idx ON files(expires_at) WHERE expires_at IS NOT NULL")

    # ------------------------------------------------------------------
    # acl_rules
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE acl_rules (
            rule_id             text PRIMARY KEY,
            ord                 int  NOT NULL,
            name                text NOT NULL UNIQUE,
            description         text,
            caller              jsonb NOT NULL DEFAULT '{}'::jsonb,
            callee              jsonb NOT NULL DEFAULT '{}'::jsonb,
            effect              text NOT NULL CHECK (effect IN ('allow','deny')),
            scope               jsonb NOT NULL DEFAULT '{"visibility":true,"permission":true}'::jsonb,
            deny_as_not_found   boolean NOT NULL DEFAULT false,
            created_at          timestamptz NOT NULL DEFAULT now(),
            created_by          text REFERENCES users(user_id)
        )
    """)
    op.execute("CREATE INDEX acl_rules_ord_idx ON acl_rules(ord)")

    # ------------------------------------------------------------------
    # audit_log (hash-chained, append-only)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE audit_log (
            event_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ts           timestamptz NOT NULL DEFAULT now(),
            actor_kind   text NOT NULL,
            actor_id     text,
            event        text NOT NULL,
            target_kind  text,
            target_id    text,
            payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
            prev_hash    text,
            self_hash    text NOT NULL
        )
    """)
    op.execute("CREATE INDEX audit_log_ts_idx ON audit_log(ts DESC)")
    op.execute("CREATE INDEX audit_log_event_idx ON audit_log(event, ts DESC)")

    # ------------------------------------------------------------------
    # invitations
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE invitations (
            token_hash    text PRIMARY KEY,
            role          text NOT NULL,
            user_tier     text,
            expires_at    timestamptz NOT NULL,
            used_at       timestamptz,
            used_by       text REFERENCES users(user_id),
            created_by    text NOT NULL REFERENCES users(user_id)
        )
    """)

    # ------------------------------------------------------------------
    # auth_refresh_tokens
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE auth_refresh_tokens (
            token_hash    text PRIMARY KEY,
            user_id       text NOT NULL REFERENCES users(user_id),
            issued_at     timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL,
            used_at       timestamptz,
            replaced_by   text
        )
    """)
    op.execute("CREATE INDEX auth_refresh_user_idx ON auth_refresh_tokens(user_id)")


def downgrade() -> None:
    for table in (
        "auth_refresh_tokens",
        "invitations",
        "audit_log",
        "acl_rules",
        "files",
        "task_events",
        "tasks",
        "agents",
        "sessions",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
