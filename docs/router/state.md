# Router — Task State, Users, ACL

> Part 2 of the router design. Covers the task state machine, the
> multi-user model (users, sessions, quotas, RBAC), and the
> capability-based ACL that replaces today's group allowlist. See
> [`protocol.md`](./protocol.md) for wire framing and
> [`storage.md`](./storage.md) for persistence and HTTP API.

## 1. Task state machine

Today's router tracks task state implicitly across columns and timeouts.
The rewrite makes it explicit: a small enum, enforced transitions, one
function that performs every transition.

### 1.1 States

```
                ┌──────────┐
                │  QUEUED  │  task row created, not yet sent to agent
                └────┬─────┘
                     │ dispatch
                     ▼
                ┌──────────┐
                │ RUNNING  │  agent acked the NewTask frame
                └────┬─────┘
                     │ spawn / delegate
                     ▼
            ┌────────────────────┐
            │ WAITING_CHILDREN   │  awaiting subtask Result(s)
            └────┬───────────────┘
                 │ all children resolved
                 ▼
                ┌──────────┐
                │ RUNNING  │  (re-entry; one task may flip
                └────┬─────┘    several times)
                     │
        ┌────────────┴────────────┬─────────────┬───────────────┐
        ▼                         ▼             ▼               ▼
   ┌──────────┐             ┌──────────┐  ┌───────────┐  ┌──────────────┐
   │SUCCEEDED │             │ FAILED   │  │ CANCELLED │  │ TIMED_OUT    │
   └──────────┘             └──────────┘  └───────────┘  └──────────────┘
        (terminal — no transitions out of any of these)
```

`SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` are terminal. Once a
row enters a terminal state, only `updated_at` may change.

### 1.2 Allowed transitions

Encoded as a static table on the router, validated on every transition:

| From               | To                                              |
| ------------------ | ----------------------------------------------- |
| `QUEUED`           | `RUNNING`, `FAILED`, `CANCELLED`, `TIMED_OUT`   |
| `RUNNING`          | `WAITING_CHILDREN`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` |
| `WAITING_CHILDREN` | `RUNNING`, `FAILED`, `CANCELLED`, `TIMED_OUT`   |
| terminal           | _(none)_                                        |

Any other transition is a programming error and raises a typed exception
that fails the task with `internal_error`.

### 1.3 Transition function

A single coroutine `task_transition(task_id, new_state, *, reason, conn)`
is the only code path that mutates `tasks.state`. It:

1. Begins a transaction.
2. Reads the current state with `SELECT ... FOR UPDATE` (Postgres) or
   `BEGIN IMMEDIATE` (aiosqlite).
3. Validates the transition against the allowed table.
4. Updates `state`, `updated_at`, and writes a row to `task_events`
   (audit log, see [`storage.md`](./storage.md)).
5. Commits.
6. Emits an OpenTelemetry span event and a Prometheus counter increment.

No other code may write to `tasks.state` directly. Linted at CI via a
grep rule.

### 1.4 Cancellation propagation

`Cancel` on task `T` causes:

1. `T` transitions to `CANCELLED` (if not already terminal).
2. All descendants of `T` (resolved via `parent_task_id` traversal) are
   transitioned to `CANCELLED` and a `Cancel` frame is forwarded to
   their assigned agents.
3. The agent SDK's handler for the cancelled task receives a
   `CancellationError` from its `await` points (see `sdk.md`).

Cancellation is best-effort: a tight CPU loop in an agent will not be
preempted. Agents that run long-running work must check
`ctx.cancel_token` periodically; the SDK enforces this in
streaming/iterating helpers.

### 1.5 Timeouts

Two layers:

- **Per-frame ack timeout** (default 30s, see `protocol.md` §4.1) —
  in-memory Future, fires `failed/ack_timeout`.
- **Per-task deadline** — optional `deadline` field on `NewTask`. The
  `timeout_sweep` background loop scans for tasks whose deadline has
  passed and transitions them to `TIMED_OUT`, emitting a `Cancel`
  frame to the assigned agent.

Default per-task deadline is provided by deployment config (e.g. 5
minutes for normal priority, 30 minutes for low priority).

## 2. Multi-user model

The current stack treats `user_id` as a string buried in the task
payload. The rewrite promotes it to a first-class top-level field on
every frame and every database row.

### 2.1 Entities

**`User`** — a registered human (or service principal). Identified by a
stable `user_id`. Owns sessions, tasks, files, quotas. Carries a `role`
(see §2.4) and an `auth` record (password hash for human users, API key
for service principals).

**`Session`** — a coherent unit of conversational/task continuity for a
single user. One user may have many concurrent sessions (different
chats, different projects). All tasks spawned within a session share a
`session_id`; agents may use this to scope memory.

**`Task`** — as before, but now `tasks` carries `(user_id, session_id)`
as indexed columns. Cross-user task access is blocked at the query
layer; cross-session is permitted but never default.

**`File`** — ProxyFile records carry `(user_id, session_id, task_id)`.
Default visibility is "owner only," with explicit sharing primitives
(see [`storage.md`](./storage.md)).

### 2.2 Session lifecycle

Sessions are explicitly opened and closed via the router HTTP API
(`POST /v1/sessions`, `DELETE /v1/sessions/{id}`). A session is a
container — closing one terminates any in-flight tasks tagged to it
(transition to `CANCELLED` with reason `session_closed`).

The Tier-0 orchestrator agent typically opens a session per
user-conversation; specialised flows (e.g. a webhook-driven cron
agent) may open ephemeral sessions per fired event.

### 2.3 Quotas and budgets

Every user carries quota counters tracked in Postgres (or a Redis
cache for hot-path checks):

| Counter              | Window       | Default              | Enforced where                     |
| -------------------- | ------------ | -------------------- | ---------------------------------- |
| `tasks_started`      | per day      | 1 000                | router on `NewTask` admit          |
| `tasks_concurrent`   | live         | 10                   | router on `NewTask` admit          |
| `llm_input_tokens`   | per day      | 1 000 000            | LLM service on call                |
| `llm_output_tokens`  | per day      | 250 000              | LLM service on call                |
| `provider_cost_usd`  | per month    | per-tier-default     | LLM service on call                |
| `file_storage_bytes` | live         | 1 GiB                | ProxyFile upload                   |

Quota enforcement happens at the latest possible point so that a
declined task does not consume downstream budget. Exceeding a quota
returns `Error{code:"quota_exceeded", retryable: false}` with a
`retry_after` hint in metadata.

### 2.4 RBAC

Three built-in roles, each a set of permissions. Custom roles are
expressible by composing permission flags.

| Role         | Permissions                                                                             |
| ------------ | --------------------------------------------------------------------------------------- |
| `admin`      | All of `user`, plus: invite agents, manage users, view audit log, edit ACL config.      |
| `user`       | Open sessions, spawn tasks against allowed agents, upload/download own files.           |
| `service`    | Like `user` but no UI access; uses long-lived API key; subject to separate quota tier.  |

Role is enforced at the router HTTP edge (admin endpoints) and at the
WebSocket edge (Hello-time for service principals; sessions for human
users carry a JWT bearing the role).

### 2.5 Per-user agent visibility

Agents may be **gated per user** (e.g. only paid-tier users see the
image-generation agent). The visibility set for a user is the
intersection of:

1. The user's role permissions.
2. The user's tier (free, paid, enterprise — deployment-defined).
3. The agent's `min_tier` and `min_role` declared in its `AgentInfo`.

The intersection is computed at session-open time and re-evaluated on
quota or role changes.

## 3. Capability-based ACL

### 3.1 Why not groups

Today's `inbound_groups` / `outbound_groups` (see `router.py:401,443`)
work but degrade as the agent suite grows: every new agent requires
manual edits to N allowlists, and the schema does not express _why_
agent A may call agent B, only _that_ it may. The rewrite replaces this
with a **capability** model.

### 3.2 Primitives

**Capability** — a string namespace describing a function an agent
provides or requires. Examples:

```
llm.generate.text
llm.generate.image
llm.generate.video
search.web
search.knowledgebase
exec.code.python
files.read.user
files.write.user
memory.read.session
memory.write.session
```

**Tag** — a string label attached to an agent for grouping
(`tier:1`, `team:coding`, `region:eu`). Tags compose with capabilities
to form ACL rules.

**Provides** — declared in `AgentInfo.capabilities`. The agent asserts
it can fulfil any caller asking for that capability.

**Requires** — declared in `AgentInfo.requires_capabilities`. The
agent's handlers may invoke capabilities in this list. Anything not
listed is denied at dispatch time.

**Tier** — an integer (0/1/2 by default). Conventional shorthand
encoded as a tag (`tier:0`, `tier:1`, ...). Tier defaults govern
visibility (see §3.4).

### 3.3 Visibility vs permission

The two are separated:

- **Visibility** controls what a caller can _see_ in
  `available_destinations` (the catalog injected into LLM tool
  schemas). Narrowing visibility reduces context bloat and misrouting.
- **Permission** controls what a caller can _invoke_ at dispatch
  time. The router checks permission on every `NewTask`, regardless
  of visibility. A caller with knowledge of an agent ID it cannot see
  still cannot call it.

Both layers consult the same capability + tag rules; they differ only
in the action they gate.

### 3.4 Default rules

Out-of-the-box, a deployment uses tier-based defaults:

| Caller tier | Sees                                                  | May invoke                                  |
| ----------- | ----------------------------------------------------- | ------------------------------------------- |
| `tier:0`    | All `tier:1` agents the user is gated for             | All visible                                 |
| `tier:1`    | A subset of `tier:2` agents tagged for this team      | All visible                                 |
| `tier:2`    | Peers within its team tag, plus `tier:0` for kickback | All visible                                 |

These are defaults expressed as ACL rules in config — not hard-coded.
A deployment can flatten the model (one tier), introduce a fourth
tier, or write capability-only rules without tiers entirely.

### 3.5 Rule expression

ACL rules are declarative, evaluated in order, first-match wins:

```yaml
acl:
  rules:
    - name: orchestrator-can-call-mains
      caller: { tags: [tier:0] }
      callee: { tags: [tier:1] }
      effect: allow

    - name: coding-team-peers
      caller: { tags: [team:coding, tier:2] }
      callee: { tags: [team:coding, tier:2] }
      effect: allow

    - name: anyone-can-call-llm-bridge
      callee: { capabilities: [llm.generate.text] }
      effect: allow

    - name: deny-direct-storage-write-from-tier-0
      caller: { tags: [tier:0] }
      callee: { capabilities: [files.write.user] }
      effect: deny

    - default: deny
```

Rules are persisted in the `acl_rules` table; admin endpoints validate
and reload them without restart.

### 3.6 Per-task scoped grants

Strict tiers can become a problem when a Tier-1 agent legitimately
needs a Tier-2 specialist not in its visibility set. The router
supports **scoped grants**: when spawning, an agent (or its parent)
may attach a one-shot ACL extension to the new task:

```jsonc
{
  "type": "NewTask",
  "...": "...",
  "acl_grants": [
    { "callee_agent_id": "test_writer", "expires": "for_task_tree" }
  ]
}
```

Grants are subject to the granter's own permission set (you cannot
grant what you cannot invoke). They are recorded in `task_events` and
visible in the audit log. `expires` may be `for_task` (just this
task), `for_task_tree` (the task and its descendants), or
`for_session` (until the session closes).

### 3.7 Discovery

Agents discover peers via:

1. **`available_destinations` in `Welcome`** — compact catalog (id +
   one-line description + tags) for everything currently visible.
2. **`describe_agent(id)` SDK call** — fetches full `AgentInfo`
   including input schema and documentation, on demand. Used by the
   LLM tool-call layer to keep prompts compact.
3. **`find_agents(capability)` SDK call** — returns the ranked list
   of visible agents that provide a capability. Powers
   capability-based delegation without hard-coding agent IDs.

All three call paths re-validate visibility against the current ACL,
so a revoked capability stops working immediately.

### 3.8 Telemetry-driven tuning

Every routing decision (allow/deny, dispatch latency, retry-after-
miss patterns) is logged with structured fields. Recommended
dashboards (see `observability.md` _planned_):

- ACL deny rate by (caller, callee) — surfaces missing rules.
- Misroute pattern: tasks that fail then succeed on retry to a
  different agent — surfaces poor visibility curation.
- Capability gaps: `find_agents(X)` calls that return empty.

These signals feed back into ACL config maintenance; the rules table
is meant to evolve from data, not stay pinned at install time.
