# Router — Storage, HTTP API, Operability

> Part 3 of the router design. Database schema, ProxyFile storage
> backend, HTTP API surface, observability, configuration, and the
> implementation sequence. See [`protocol.md`](./protocol.md) and
> [`state.md`](./state.md) for the wire protocol and task model.

## 1. Database

Single relational store. `aiosqlite` for single-node; Postgres ≥ 14 for
multi-worker. All migrations managed via Alembic — no ad-hoc DDL.

### 1.1 Core tables

```
users(user_id PK, role, tier, auth_kind, auth_secret_hash,
      created_at, suspended_at)

sessions(session_id PK, user_id FK, opened_at, closed_at,
         metadata JSONB)

agents(agent_id PK, kind, status, capabilities JSONB,
       requires_capabilities JSONB, tags JSONB, agent_info JSONB,
       auth_token_hash, public_key, registered_at, last_seen_at)

acl_rules(rule_id PK, ord, name, caller JSONB, callee JSONB,
          effect, created_at, created_by FK)

tasks(task_id PK, parent_task_id FK, root_task_id, user_id FK,
      session_id FK, agent_id FK, state, status_code,
      idempotency_key UNIQUE(user_id, idempotency_key),
      priority, deadline, created_at, updated_at,
      input JSONB, output JSONB, error JSONB)

task_events(event_id PK, task_id FK, ts, kind, actor_agent_id,
            from_state, to_state, payload JSONB)

files(file_id PK, sha256 UNIQUE, user_id FK, session_id FK,
      task_id FK, byte_size, mime_type, storage_url,
      original_filename, created_at, expires_at)

audit_log(event_id PK, ts, actor_kind, actor_id, event,
          target_kind, target_id, payload JSONB)

invitations(token_hash PK, role, tier, expires_at, used_at,
            used_by FK, created_by FK)
```

### 1.2 Indexes

- `tasks(user_id, state)` — quota counters.
- `tasks(parent_task_id)` — child traversal for cancellation.
- `tasks(state) WHERE state IN ('QUEUED','RUNNING','WAITING_CHILDREN')` —
  partial index for `timeout_sweep`.
- `task_events(task_id, ts)` — audit queries.
- `files(sha256)` — content-address dedup.

### 1.3 Constraints

- `tasks.state` validated by CHECK constraint against the enum.
- `tasks.deadline` enforced as `NULL OR > created_at`.
- All FK with `ON DELETE RESTRICT` (no cascade — preserve audit
  history; soft-delete via flags where needed).

### 1.4 Single-writer SQLite caveat

aiosqlite + WAL is single-writer. Acceptable for the single-node
profile. For Postgres deployments, use `SELECT ... FOR UPDATE` in the
transition function (`state.md` §1.3) to serialise per-task
transitions; row-level locking gives proper concurrency.

## 2. ProxyFile storage backend

The current implementation writes files to a local `PROXYFILE_DIR`
(`router.py:47`). Multi-worker / multi-host deployments break this. The
rewrite defines a pluggable interface:

```python
class FileStore(Protocol):
    async def put(self, sha256: str, src: AsyncIterable[bytes],
                  meta: FileMeta) -> str: ...
    async def open(self, sha256: str) -> AsyncIterator[bytes]: ...
    async def presigned_url(self, sha256: str,
                            ttl_s: int) -> Optional[str]: ...
    async def delete(self, sha256: str) -> None: ...
```

Implementations: `LocalFileStore` (default), `S3FileStore`,
`GCSFileStore`, `R2FileStore`. Selection via config (§5).

### 2.1 Content addressing

Every uploaded file is hashed (sha256) before storage and stored under
its hash. `files.sha256` is unique — duplicate uploads reuse the
existing object. Integrity checks on download verify the hash matches.

### 2.2 ProxyFile protocol values

| `protocol`      | Meaning                                              |
| --------------- | ---------------------------------------------------- |
| `router-proxy`  | Path is a router-served URL keyed by `file_id`       |
| `presigned`     | Path is a backend-direct URL with TTL               |
| `localfile`     | Embedded-agent-only; absolute filesystem path       |
| `http`          | Reserved for cross-router or external HTTP sources  |

`presigned` is preferred when the backend supports it (S3-compatible),
removing the router from the byte path entirely.

### 2.3 Lifecycle

- Files default to TTL = `task.created_at + 7 days` (configurable).
- A garbage-collection loop (analogous to current `gc_proxy_files` in
  `router.py`) deletes expired rows and their backend objects.
- User quota (`file_storage_bytes`, see `state.md` §2.3) enforced at
  upload time.
- `DELETE /v1/sessions/{id}` cascades to file expiry for that session.

## 3. HTTP API surface

WebSocket `/v1/agent` carries all agent-runtime traffic. Everything
else is HTTP. All endpoints are versioned under `/v1/`; breaking
changes ship under `/v2/`.

### 3.1 Public (user-facing)

| Method | Path                                  | Purpose                            |
| ------ | ------------------------------------- | ---------------------------------- |
| POST   | `/v1/auth/login`                      | Issue session JWT                  |
| POST   | `/v1/auth/refresh`                    | Refresh session JWT                |
| POST   | `/v1/sessions`                        | Open a session                     |
| DELETE | `/v1/sessions/{id}`                   | Close a session                    |
| GET    | `/v1/sessions/{id}/tasks`             | List tasks in session              |
| GET    | `/v1/tasks/{id}`                      | Read one task (status + events)    |
| POST   | `/v1/tasks/{id}/cancel`               | Cancel a task                      |
| GET    | `/v1/files/{file_id}`                 | Download (router-proxy) or 302 to presigned |
| POST   | `/v1/files`                           | Upload (multipart or chunked)      |

### 3.2 Agent-facing

| Method | Path                              | Purpose                            |
| ------ | --------------------------------- | ---------------------------------- |
| POST   | `/v1/onboard`                     | Register a new external agent      |
| POST   | `/v1/agent/refresh-token`         | Rotate auth token                  |
| WS     | `/v1/agent`                       | Long-lived agent WebSocket         |

### 3.3 Admin

| Method | Path                              | Purpose                            |
| ------ | --------------------------------- | ---------------------------------- |
| POST   | `/v1/admin/invitations`           | Issue agent invitation             |
| POST   | `/v1/admin/users`                 | Create user                        |
| PATCH  | `/v1/admin/users/{id}`            | Update role/tier/quotas            |
| GET    | `/v1/admin/audit`                 | Query audit log                    |
| GET    | `/v1/admin/acl/rules`             | List ACL rules                     |
| PUT    | `/v1/admin/acl/rules`             | Replace ACL ruleset (validated)    |
| GET    | `/v1/admin/agents`                | List registered agents             |
| POST   | `/v1/admin/agents/{id}/suspend`   | Force-disconnect & disable         |

### 3.4 Health

| Method | Path                  | Purpose                                    |
| ------ | --------------------- | ------------------------------------------ |
| GET    | `/healthz`            | Liveness (process up)                      |
| GET    | `/readyz`             | Readiness (DB reachable, storage writable) |
| GET    | `/metrics`            | Prometheus exposition                      |

## 4. Observability

Three pillars, all on by default. None of these are opt-in.

### 4.1 Tracing

OpenTelemetry. Every WebSocket frame carries `trace_id` + `span_id`
(`protocol.md` §2.1). The router creates a span per dispatch, per ACL
check, per state transition, per DB write. Spans link parent → child
across the task tree via the trace context propagated in `NewTask`.
Exporter is OTLP/HTTP, configured via standard `OTEL_*` env vars.

### 4.2 Logs

Structured JSON to stdout. Every log line carries
`{ts, level, trace_id, span_id, user_id?, session_id?, task_id?,
agent_id?, event, ...}`. No `print()` calls anywhere; the linter
forbids them. Log levels are honoured per-module via standard
`logging` config.

### 4.3 Metrics

Prometheus, exposed at `/metrics`. The minimum metric set:

- `router_frames_total{direction, type, agent_id}` (counter)
- `router_task_state_transitions_total{from, to}` (counter)
- `router_task_duration_seconds{terminal_state}` (histogram)
- `router_acl_decisions_total{effect, rule_name}` (counter)
- `router_quota_exceeded_total{counter, user_tier}` (counter)
- `router_ws_connected_agents` (gauge)
- `router_db_query_duration_seconds{query}` (histogram)
- `router_storage_bytes_total{backend}` (counter)

## 5. Configuration

Single `Settings` object using Pydantic Settings. Validated at startup;
typos and missing required values fail fast. No scattered
`os.environ.get(...)`.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTER_")

    db_url: str                                  # postgres://... or sqlite+aiosqlite:///...
    redis_url: Optional[str] = None              # required for multi-worker
    file_store: Literal["local", "s3", "gcs", "r2"] = "local"
    file_store_options: dict[str, Any] = {}
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    public_url: str                              # external base URL
    jwt_secret: SecretStr
    auth_token_ttl_s: int = 86_400
    heartbeat_interval_ms: int = 20_000
    max_payload_bytes: int = 1_048_576
    per_socket_outbox_max: int = 256
    default_task_deadline_s: int = 300
    file_default_ttl_s: int = 604_800
    otel_endpoint: Optional[str] = None
    log_level: str = "INFO"
```

A second `AclConfig` model loads `acl.yaml` (rules + tier defaults)
and is hot-reloadable via `PUT /v1/admin/acl/rules`.

## 6. Concurrency model

- Single asyncio event loop per worker.
- DB calls via `aiosqlite` or `asyncpg` — never block the loop.
- CPU-bound work (image resize, hashing) offloaded to a
  `concurrent.futures.ThreadPoolExecutor` via `asyncio.to_thread`.
- One Postgres connection pool per worker; one Redis pool per worker.
- Per-socket send tasks isolate slow consumers from each other.

For multi-worker deployments, an external load balancer terminates
TLS and sticky-routes WebSocket connections by `agent_id` (consistent
hashing). All shared state lives in Postgres + Redis; workers are
otherwise stateless.

## 7. Implementation sequencing

The recommended build order (each step deliverable in isolation):

1. **Schema + migrations.** Postgres / aiosqlite + Alembic skeleton.
   Stand up `users`, `sessions`, `agents`, `tasks`, `task_events`,
   `files`, `acl_rules`, `audit_log`, `invitations`.
2. **Frame models + transition function.** Pydantic discriminated
   union, single `task_transition()`. Unit-tested in isolation.
3. **WebSocket hub.** `/v1/agent` endpoint, Hello/Welcome handshake,
   in-memory socket registry, heartbeat. Echo-only initially.
4. **Embedded agent dispatch.** Direct-call registry; smoke test with
   a noop embedded agent.
5. **External dispatch.** Real send/recv loops, ack correlation,
   disconnect cleanup.
6. **ACL evaluator.** Capability + tag rules, scoped grants, deny-by-
   default.
7. **HTTP API.** Onboarding, sessions, tasks, files, admin.
8. **ProxyFile + LocalFileStore.** Content-addressed local backend.
   S3/GCS land later behind the same interface.
9. **Observability.** OTel + Prometheus + structured logs from step 1
   onward; this item is a verification milestone, not greenfield work.
10. **First real agent (Gemini).** Drives end-to-end validation and
    pressure-tests the SDK (see [`sdk.md`](../sdk.md)).

Steps 1–4 are the foundation; nothing else can be tested without them.
Steps 5–9 are independently shippable. Step 10 is the first
deployment milestone.
