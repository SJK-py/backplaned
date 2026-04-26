# Reworked Backplaned — Design Overview

> **Status:** design draft. This document defines the target architecture for the
> next-generation router and agent SDK. It is deliberately opinionated and
> prescriptive; deviations should be explicitly noted and justified.

## 1. Why a rewrite

The current Backplaned stack (see `router.py`, `helper.py`) was built around a
single-user developer workflow and HTTP request/response between router and
agents. It has served well as a proof-of-concept, but several architectural
limits have emerged:

1. **Single-user assumptions.** `user_id` rides inside `payload` rather than as
   a first-class field. Quotas, per-user isolation, and RBAC are bolted on.
2. **Loose typing.** `RouteRequest.payload: dict[str, Any]` and the
   `identifier`/`task_id`/`destination_agent_id`/`status_code` tuple carry
   multiple distinct intents (spawn, delegate, result, progress) over one
   schema. Misrouting is caught in agent LLM logic, not at the boundary.
3. **Transport coupling.** Every agent runs its own FastAPI server exposing
   `/receive`. Agents behind NAT need inbound-reachable endpoints. Each task
   delivery opens a fresh HTTP round trip.
4. **Process-local state.** `_pending_results`, progress queues, and (in the
   WebSocket future) socket registries live in a single Python process. The
   router cannot be horizontally scaled without rework.
5. **Storage coupling.** ProxyFile assumes a local filesystem under the router
   process. Multi-host deployment breaks file serving.
6. **Allowlist-based ACL.** `inbound_groups` / `outbound_groups` work, but do
   not scale as the agent suite grows and do not cleanly express
   capability-based routing.
7. **Thin observability.** No structured trace IDs, no built-in metrics, no
   span propagation across the task tree.

The rewrite addresses all seven, while preserving the proven ideas: central
router as a switchboard, per-agent auth tokens, typed AgentInfo, ProxyFile as
the file abstraction, and the parent/child task tree.

## 2. Target scope

The reworked system targets these workloads:

- **Multi-user deployments**, from small teams to SaaS-style installations.
- **Provider-tailored agent suites** — e.g. a Gemini agent family that exposes
  the full Gemini capability surface (grounding, code execution, image/video
  generation, multimodal I/O) and coexists with Anthropic/OpenAI equivalents.
- **Tiered orchestration trees** (Tier 0 orchestrator → Tier 1 mains → Tier 2
  specialists) with controlled visibility at each layer.

It does **not** target:

- Non-Python agents as first-class peers. (Non-Python agents remain possible
  but must speak the framed WebSocket protocol themselves — the SDK is Python.)
- Fully distributed consensus. There is still a single logical router (which
  may be horizontally scaled behind a shared Postgres + Redis).
- Replacement of the admin/webapp UI in this pass. The UI consumes the router
  HTTP API and is upgraded independently.

## 3. Guiding principles

These are load-bearing and should be treated as hard constraints on the design:

**P1. Typed frames end-to-end.** Every message between router and agents is a
Pydantic-validated discriminated-union frame. `dict[str, Any]` payloads are
permitted only at explicit extension points, never as the primary schema.

**P2. Protocol hides behind the SDK.** Agent authors never write transport code.
They register handlers. Whether the agent is embedded or external, HTTP or
WebSocket, is a deployment flag — not a code change.

**P3. One channel per agent.** WebSocket, full-duplex, multiplexed by
`correlation_id`. Ack semantics and result delivery ride the same socket. HTTP
remains only for ProxyFile transfer and admin/UI endpoints.

**P4. Durable state in storage, ephemeral state in memory.** Task records,
user/session records, quotas, ACL config → Postgres (or aiosqlite for
single-node). Pending ack futures, live socket registry, progress fan-out →
in-memory, replaceable with Redis when scaling out.

**P5. First-class user/session.** Every frame carries `user_id` and
`session_id`. Every task row, every file, every audit entry is keyed by them.

**P6. Capability-based ACL, not hand-maintained allowlists.** Agents declare
capabilities they provide and require. Visibility and permission are
computed, not edited by hand. Tiers are defaults; overrides are explicit,
scoped, and audited.

**P7. Observability is not optional.** OpenTelemetry spans, structured logs,
Prometheus metrics, and `trace_id` propagation are built-in from day one.

**P8. Embedded is a deployment flag.** Hot-path agents (LLM bridge, format
validators) run in-process via direct function dispatch — no
`httpx.ASGITransport`. The SDK transparently routes to the right dispatcher.

**P9. Backward-incompatible by default.** This is a rewrite, not a migration.
The old router can run side-by-side for cutover, but internal APIs are not
preserved. This buys simplicity; there is no compatibility layer in the
codebase.

## 4. Architecture at a glance

```
                  ┌──────────────────────────────────────────────────┐
                  │                    Router                        │
                  │  ┌─────────┐  ┌──────────┐   ┌────────────────┐  │
                  │  │ WS hub  │  │ Task SM  │   │ Storage layer  │  │
                  │  └────┬────┘  └────┬─────┘   └──────┬─────────┘  │
                  │       │            │                │            │
                  │  ┌────┴────────────┴──────┐  ┌──────┴──────────┐ │
                  │  │ Frame dispatch + ACL   │  │ Postgres + Redis│ │
                  │  └────────────────────────┘  └─────────────────┘ │
                  │  ┌────────────────────────┐  ┌─────────────────┐ │
                  │  │ Embedded agent registry│  │ ProxyFile store │ │
                  │  └────────────────────────┘  │  (S3/R2/GCS/fs) │ │
                  │                              └─────────────────┘ │
                  │  ┌────────────────────────────────────────────┐  │
                  │  │ HTTP API: admin, onboarding, ProxyFile I/O │  │
                  │  └────────────────────────────────────────────┘  │
                  └────┬─────────────────────────────────────────┬───┘
                       │ WebSocket                               │ HTTP
                       │ (frames)                                │ (files, admin)
         ┌─────────────┼─────────────┐                           │
         ▼             ▼             ▼                           ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐               ┌──────────┐
   │ External │  │ External │  │ External │    … etc      │ Webapp   │
   │ agent A  │  │ agent B  │  │ agent C  │               │   UI     │
   └──────────┘  └──────────┘  └──────────┘               └──────────┘

   (Embedded agents live inside the router process; invoked via
    direct function dispatch through the same frame pipeline.)
```

The router is the single point that sees every frame. Every frame is typed,
tagged with a trace ID, validated against the agent's declared interface,
and routed through the capability-based ACL layer. The SDK on each side
handles correlation, acks, reconnection, and heartbeat transparently.

## 5. Key departures from current Backplaned

| Concern                     | Current (`router.py`, `helper.py`)                     | Reworked                                                    |
| --------------------------- | ------------------------------------------------------ | ----------------------------------------------------------- |
| Transport (router ↔ agent)  | HTTP POST both directions (`/route`, `/receive`)       | Single WebSocket per agent, typed frames                    |
| Frame schema                | `RouteRequest` with `payload: dict[str, Any]`          | Discriminated union: `NewTask | Result | Progress | Cancel | Error | Ack` |
| Correlation                 | `identifier` + `task_id`, HTTP 202 ack                 | `correlation_id` + `task_id`, app-level ack frame           |
| User model                  | `user_id` inside payload                               | Top-level field on every frame; indexed in all tables       |
| Sessions                    | Implicit per-task                                      | First-class `session_id` with memory/context boundary       |
| ACL                         | `inbound_groups` / `outbound_groups`                   | Capability + tag + per-user scope, computed                 |
| Embedded dispatch           | `httpx.ASGITransport` (simulated HTTP)                 | Direct async function call via handler registry             |
| Storage (ProxyFile)         | Local filesystem                                       | Pluggable backend (local, S3, R2, GCS) + content hashing    |
| Database                    | Synchronous `sqlite3` on thread pool                   | `aiosqlite` (single-node) or Postgres + Alembic             |
| Task state                  | Implicit across columns                                | Explicit state machine with enforced transitions            |
| Progress                    | SSE endpoint (`/tasks/{id}/progress`)                  | Same WS channel; SSE removed                                |
| Cancellation                | Not supported                                          | `Cancel` frame propagated through task tree                 |
| LLM bridge                  | `llm_agent` via `httpx.ASGITransport`                  | SDK-exposed service (`ctx.llm.generate(...)`)               |
| Observability               | `print` / stdlib `logging`                             | Structured JSON logs + OpenTelemetry + Prometheus           |
| Config                      | `os.environ.get(...)` scattered                        | Pydantic Settings, validated at startup                     |
| Auth                        | Opaque bearer token (persistent)                       | Signed short-lived token + refresh, with agent keypair (optional) |

## 6. What stays the same

- **Central router as switchboard.** No peer-to-peer agent discovery;
  everything flows through the router.
- **Per-agent auth tokens.** The mechanism differs (shorter TTL, optional
  signing), but the concept is unchanged.
- **`AgentInfo`** (`helper.py:153-187`) as the registration record, now with
  additional typed fields (capabilities, accepts/produces schema, tier).
- **`ProxyFile`** (`helper.py:125-144`) as the file reference abstraction.
  Protocols are extended; the role is unchanged.
- **`LLMData`** (`helper.py:190-202`) as the high-level LLM prompt container.
  `LLMCall` (`helper.py:205-225`) remains for low-level provider-native calls.
- **Parent/child task tree.** Tasks still have `parent_task_id` and spawn/
  delegate semantics.
- **Invitation-token onboarding** (`helper.py:1222-1283`). The handshake moves
  onto the WebSocket channel after the initial onboard HTTP POST.

## 7. Migration approach

This is a green-field repository. There is **no in-place migration** of a
running Backplaned instance. A deployment that wants to adopt the reworked
stack cuts over:

1. Stand up the new router against an empty Postgres (or fresh aiosqlite DB).
2. Issue fresh invitations; external agents re-onboard as SDK-based clients.
3. Existing ProxyFiles are not migrated; old tasks are considered archived.
4. The admin UI points at the new router; history starts fresh.

For any agent logic that must be carried forward (prompts, provider configs,
tool definitions), extract it and re-implement inside the new SDK's handler
shape — the surface is small and porting is mechanical.

The expected build sequence is documented in
[`router.md §Implementation sequencing`](./router.md#implementation-sequencing).

## 8. Non-goals

- **A plugin marketplace.** The agent SDK is opinionated and Python-only.
- **Byzantine fault tolerance.** The router trusts authenticated agents.
- **Multi-region active-active.** One logical router per deployment, optionally
  horizontally scaled within one region via Redis/Postgres.
- **Automatic schema evolution of `payload` dicts.** Frame-level schemas are
  versioned and backward-incompatible changes require a new frame type.
- **Zero-downtime SDK upgrades.** Agents run with a pinned SDK version; the
  router rejects frames with an incompatible protocol version.

## 9. Document map

Router (split for readability and resilience while drafting):

- [`router/protocol.md`](./router/protocol.md) — WebSocket frame envelope,
  connection lifecycle, heartbeat, correlation model.
- [`router/state.md`](./router/state.md) — Task state machine, multi-user
  model (users, sessions, quotas, RBAC), capability-based ACL.
- [`router/storage.md`](./router/storage.md) — Database schema, ProxyFile
  storage backend, HTTP API surface, observability, configuration,
  implementation sequencing.

SDK:

- [`sdk/core.md`](./sdk/core.md) — Agent surface, `TaskContext`, transport
  abstraction, frame dispatch, peer calls, lifecycle, errors.
- [`sdk/services.md`](./sdk/services.md) — LLM service, file handling,
  progress, cancellation, tool builders, embedded vs. external
  deployment, testing, worked Gemini-agent example.

Planned follow-ups:

- _(planned)_ `acl.md` — Deep-dive on capability and tier model.
- _(planned)_ `observability.md` — Span / log / metric conventions.
- _(planned)_ `security.md` — Threat model, token lifecycle, secrets
  handling.
