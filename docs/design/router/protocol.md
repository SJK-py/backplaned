# Router — Wire Protocol

> Part 1 of the router design. Covers transport, frame schema, connection
> lifecycle, and correlation. See [`state.md`](./state.md) for the task state
> machine and ACL, and [`storage.md`](./storage.md) for persistence and the
> HTTP API.

## 1. Transport summary

| Channel              | Transport            | Purpose                                                   |
| -------------------- | -------------------- | --------------------------------------------------------- |
| Agent ↔ router       | **WebSocket** (TLS)  | All task delivery, results, progress, control             |
| ProxyFile transfer   | HTTP/1.1 (TLS)       | Bulk file upload/download (presigned URLs where possible) |
| Admin / onboarding   | HTTP/1.1 (TLS)       | Invitation issuance, user mgmt, agent onboarding handshake |
| Webapp UI            | HTTP + WebSocket     | UI consumes the same agent-side WebSocket protocol        |

Rationale recap (see [`overview.md`](../overview.md) §3): one full-duplex
socket per agent multiplexed by `correlation_id` removes per-task TLS/TCP
overhead, removes the need for agents to expose inbound listeners, and
unifies progress streaming with task delivery. Files stay on HTTP because
HTTP semantics (`Range`, streaming, presigned URLs, caching) are a
better fit for bulk bytes than WebSocket framing.

## 2. Frame envelope

Every frame on the WebSocket is a UTF-8 JSON object validated against a
discriminated union on `type`. Frames are individually self-describing —
no implicit state on the receiver beyond the correlation map.

### 2.1 Common header

All frames carry these fields:

| Field            | Type        | Required | Notes                                                          |
| ---------------- | ----------- | -------- | -------------------------------------------------------------- |
| `type`           | enum string | yes      | Discriminator. See §2.2.                                       |
| `protocol_version` | string    | yes      | `"1"` for this spec. Mismatch ⇒ socket closed with code 1002.  |
| `correlation_id` | string      | yes      | UUIDv7 (lex-sortable). Used for ack and result matching.       |
| `trace_id`       | string      | yes      | OpenTelemetry trace id. Propagated unchanged across the tree.  |
| `span_id`        | string      | yes      | Per-frame span id.                                             |
| `timestamp`      | RFC3339     | yes      | UTC, microsecond precision.                                    |
| `agent_id`       | string      | yes      | Sender's agent_id.                                             |

Per-frame additions are described in §2.2.

### 2.2 Frame types

```
                ┌─────────────────────────────────────────────┐
                │  agent ↔ router frame types                 │
                ├─────────────────────────────────────────────┤
                │  Hello       — auth handshake (first frame) │
                │  Welcome     — handshake response           │
                │  NewTask     — spawn / delegate             │
                │  Result      — terminal task outcome        │
                │  Progress    — interim event                │
                │  Cancel      — abort a task                 │
                │  Error       — protocol-level failure       │
                │  Ack         — receipt acknowledgement      │
                │  Ping/Pong   — heartbeat                    │
                └─────────────────────────────────────────────┘
```

**`Hello`** — first frame on a new socket, agent → router.

```jsonc
{
  "type": "Hello",
  "agent_id": "...",
  "auth_token": "...",          // short-lived JWT or signed bearer
  "sdk_version": "1.0.0",
  "agent_info": { ... },        // AgentInfo payload (see sdk.md)
  "resume_token": "..."         // optional: re-attach in-flight tasks
  // ...common header...
}
```

**`Welcome`** — router → agent, sent only after successful `Hello`.

```jsonc
{
  "type": "Welcome",
  "session_id": "...",          // server-issued, valid until disconnect
  "available_destinations": { ... },   // ACL-filtered tools (compact form)
  "capabilities": [ ... ],      // capability strings the agent provides
  "heartbeat_interval_ms": 20000,
  "max_payload_bytes": 1048576
}
```

**`NewTask`** — agent → router (or router → agent on dispatch). Both
spawn (new task tree) and delegate (preserve task_id) use this frame; the
distinction is `task_id == null` vs. set.

```jsonc
{
  "type": "NewTask",
  "task_id": null,                          // null = spawn, str = delegate
  "parent_task_id": "...",                  // null at root
  "destination_agent_id": "gemini_main",
  "user_id": "...",                         // first-class, see overview §P5
  "session_id": "...",
  "priority": "normal",                     // "low"|"normal"|"high"
  "deadline": "2026-04-26T12:34:56Z",       // optional hard deadline
  "payload": {                              // typed per-destination schema
    "llmdata": { ... },
    "files": [ ProxyFile, ... ],
    "handoff_note": "..."
  }
}
```

**`Result`** — terminal outcome of a task (success, error, timeout,
cancelled). One per task, ever.

```jsonc
{
  "type": "Result",
  "task_id": "...",
  "parent_task_id": "...",
  "status": "succeeded",         // succeeded|failed|cancelled|timed_out
  "status_code": 200,            // numeric, mirrors HTTP semantics
  "output": {                    // AgentOutput-shaped
    "content": "...",
    "files": [ ProxyFile, ... ]
  },
  "error": null                  // populated when status != succeeded
}
```

**`Progress`** — interim event during long-running tasks. Replaces the
current SSE channel.

```jsonc
{
  "type": "Progress",
  "task_id": "...",
  "event": "thinking",           // thinking|tool_call|tool_result|chunk|status
  "content": "...",              // free-form, may stream tokens
  "metadata": { ... }            // event-specific (tool name, token index, etc.)
}
```

**`Cancel`** — request abort of an in-flight task. Propagates to all
descendants. Recipient responds with a `Result` of `status="cancelled"`.

```jsonc
{
  "type": "Cancel",
  "task_id": "...",
  "reason": "user_aborted"       // short, machine-readable
}
```

**`Error`** — protocol-level failure (validation, auth, ACL deny). Not
used for task-level failures (which use `Result` with non-2xx
`status_code`).

```jsonc
{
  "type": "Error",
  "code": "acl_denied",          // see §6 for the catalog
  "message": "...",
  "ref_correlation_id": "...",   // the offending frame, if applicable
  "retryable": false
}
```

**`Ack`** — receipt acknowledgement. Sent by the receiver of a
`NewTask`, `Result`, or `Cancel` frame, with `correlation_id` matching
the original. See §5 for semantics.

```jsonc
{
  "type": "Ack",
  "ref_correlation_id": "...",
  "accepted": true,
  "reason": null                 // populated when accepted=false
}
```

**`Ping` / `Pong`** — heartbeat. Either side may send `Ping`; receiver
must reply with `Pong` carrying the same `correlation_id`. See §4.4.

### 2.3 Validation rules

- All frames are validated against Pydantic models at the router edge
  before any business logic runs. Invalid frames are responded to with
  `Error{code:"frame_invalid"}` and the offending frame is dropped.
- `payload` for `NewTask` is validated against the destination agent's
  declared `accepts` schema (see `sdk.md`). Schema mismatch ⇒
  `Error{code:"schema_mismatch"}` with the validation report; the frame
  does **not** create a task row.
- `protocol_version` mismatch ⇒ socket closed with WebSocket close
  code 1002 and a final `Error{code:"protocol_version"}` frame.
- Frame size limit: `max_payload_bytes` (default 1 MiB) advertised in
  `Welcome`. Larger frames close the socket with code 1009.

## 3. Connection lifecycle

```
   agent                                router
     │                                    │
     ├── HTTP POST /onboard ─────────────►│   (one-time, invitation token)
     │◄── OnboardResponse ────────────────┤   (auth_token, agent_id, …)
     │                                    │
     ├── WS UPGRADE /v1/agent ───────────►│
     │◄── 101 Switching Protocols ────────┤
     │                                    │
     ├── Hello ──────────────────────────►│
     │                                    │   verify token, register socket
     │◄── Welcome ────────────────────────┤
     │                                    │
     │  ╔════════════════ steady state ═══╗
     │  ║                                  ║
     │  ║  NewTask, Result, Progress,      ║
     │  ║  Cancel, Ack, Ping/Pong          ║
     │  ║                                  ║
     │  ╚══════════════════════════════════╝
     │                                    │
     │  (disconnect — see §4.5)           │
     ▼                                    ▼
```

### 3.1 Onboarding

External agents perform a one-time HTTP POST to `/v1/onboard` with an
invitation token (issued by an admin). The router responds with
`{agent_id, auth_token, ...}`. This handshake stays on HTTP because:

- It happens once per agent, not per session.
- It needs human-mediated invitation flow.
- Failure modes (token expired, already used) are simpler to surface
  in HTTP semantics.

The `auth_token` is short-lived (default 24h) and refreshed automatically
by the SDK via a `/v1/agent/refresh-token` endpoint.

### 3.2 Connect / Hello

The agent opens a WebSocket to `/v1/agent` and immediately sends `Hello`
with its `auth_token`. The router:

1. Validates the token (signature + expiry + DB lookup).
2. Verifies the `agent_id` is in the expected state (registered, not
   suspended).
3. If a previous socket for this `agent_id` is still mapped, that socket
   is closed with code 4001 ("superseded") before the new one is
   registered. Only one live socket per `agent_id`.
4. Replies with `Welcome` carrying the agent's ACL-filtered destinations
   and runtime parameters.

### 3.3 Resume semantics (optional)

If the agent supplies `resume_token` in `Hello`, and the token matches a
recently-disconnected session whose in-flight tasks were not yet failed,
the router re-attaches the socket and **does not** fail those tasks. The
resume window is short (default 30 s) and configurable per deployment.
This is opt-in — the simple path is "drop = fail in-flight, reconnect
fresh."

### 3.4 Heartbeat

After `Welcome`, both sides start a heartbeat timer. The router sends a
`Ping` every `heartbeat_interval_ms` of socket idle time; the agent
responds with `Pong` echoing the `correlation_id`. Two missed pings ⇒
the router closes the socket with code 4002 ("heartbeat_timeout") and
the disconnect path runs (§3.5). Agents may also initiate `Ping`.

### 3.5 Disconnect

On any disconnect (clean close, error, heartbeat timeout, supersede):

1. Remove the socket from the in-memory `agent_id → WebSocket` registry.
2. If resume window applies (§3.3), park the entry in a "pending
   resume" structure with TTL.
3. Otherwise, fail every in-flight task currently assigned to this
   `agent_id` with `status="failed"`, `status_code=503`, error
   `agent_disconnected`. Propagate result frames to parents.
4. Emit a `disconnect` audit event with the close code and reason.

## 4. Correlation model

Two distinct correlation needs, handled separately:

**Frame-level acks** — confirms that a peer received and accepted (or
rejected) a specific frame. Carried by the `Ack` frame, matched by
`correlation_id`. Lives entirely in process memory.

**Task-level lifecycle** — confirms eventual completion of a task.
Carried by the `Result` frame, matched by `task_id`. Persisted in the
`tasks` table; survives router restarts via the durable state machine
(see [`state.md`](./state.md)).

### 4.1 Frame-level ack flow

```
   sender                              receiver
     │                                    │
     ├── NewTask{correlation_id=X} ──────►│
     │                              (validate, enqueue work)
     │◄── Ack{ref_correlation_id=X} ──────┤
     │   (Future for X resolves)
```

The sender registers a Future keyed by `correlation_id` before sending,
awaits with a configurable timeout (default 30 s, mirroring the current
HTTP behaviour), and resolves on `Ack`. Timeout ⇒ the Future is rejected
with `ack_timeout` and the task that prompted the frame is failed via
the same path used for transport errors today.

The router does not need a second pending-ack system on top of SQLite —
the existing task table + `timeout_sweep` already carry task-level
correlation. The frame ack is in-memory only.

### 4.2 Task-level lifecycle flow

```
   parent agent                  router                   child agent
        │                          │                          │
        ├── NewTask(t=null) ──────►│                          │
        │◄── Ack ──────────────────┤   (task_id assigned)     │
        │                          ├── NewTask(t=T1) ────────►│
        │                          │◄── Ack ──────────────────┤
        │                          │                          │
        │                          │     … work happens …     │
        │                          │                          │
        │                          │◄── Progress(t=T1) ───────┤
        │◄── Progress(t=T1) ───────┤                          │
        │                          │                          │
        │                          │◄── Result(t=T1) ─────────┤
        │                          ├── Ack ──────────────────►│
        │◄── Result(t=T1) ─────────┤                          │
        ├── Ack ──────────────────►│                          │
```

The router fans Progress frames out to subscribers (the parent and any
UI listeners). Result is delivered exactly once to the parent agent;
the router persists it before fan-out so a router crash mid-fan-out
does not lose the result.

### 4.3 Idempotency

`NewTask` from agents may carry an optional `idempotency_key` (string,
unique per agent within a 24h window). The router deduplicates: a
second `NewTask` with the same key returns the existing
`{task_id}` rather than creating a new task. Safe retries on flaky
networks. The key is not visible to the destination agent.

### 4.4 Ordering guarantees

Per-socket: WebSocket guarantees in-order delivery within a single
connection. The router preserves that ordering when forwarding to the
destination socket (no reordering, no fan-in interleaving across
sources).

Across sockets: no ordering guarantee. Two parents writing to the same
child see their `NewTask` frames interleaved in unspecified order.
Agents must not assume cross-source ordering.

### 4.5 Backpressure

The router maintains per-socket send queues bounded by
`per_socket_outbox_max` (default 256 frames). When full:

- For **`Progress`** frames: drop oldest (best-effort delivery).
- For **`NewTask` / `Result`**: apply backpressure to the producer by
  awaiting queue space, with a deadline. On deadline, the originating
  task is failed with `backpressure_timeout`.

Bound the queue, drop or coalesce on overflow — never let one slow
peer bloat router memory.
