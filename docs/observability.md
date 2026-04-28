# Observability — Traces, Logs, Metrics

> Conventions for the three observability pillars across router and SDK.
> Companion to [`router/storage.md §4`](./router/storage.md#4-observability),
> which introduced the topic. This document is prescriptive: span names,
> required attributes, log fields, metric names. Deviations break
> dashboards and alerts.

## 1. Principles

**O1. On by default.** No agent or operator should have to opt in.
Disabling is possible (`OTEL_SDK_DISABLED=true`) but not the default.

**O2. Trace-id everywhere.** Every WebSocket frame, every HTTP
request, every log line, every metric label set carries the
originating trace context. Correlation is non-negotiable.

**O3. Cardinality budgets.** No metric label may carry unbounded
values (`task_id`, `user_id`, free-form strings). Logs and traces
are the place for high-cardinality data, not metrics.

**O4. Privacy by default.** No prompt content, no PII, no file
content, no auth tokens in any pillar. Hash or omit. Operators can
opt into prompt logging at higher levels for development.

**O5. Same conventions, both sides.** Router and SDK emit the same
span names, the same attribute keys, the same log fields. An agent's
spans nest under the router's spans automatically through OTel
context propagation.

## 2. Tracing

### 2.1 Propagation

Every `NewTask` frame carries `trace_id` and `span_id` (`protocol.md`
§2.1). The router and SDK attach OTel context using these values:

- **Root.** A user-initiated session opens a root span at the
  router's HTTP edge (login or session-open endpoint). Its
  `trace_id` is propagated to the orchestrator's first `NewTask`.
- **Inheritance.** Each `NewTask` becomes a child span of its
  parent. The frame carries the parent span's id; the receiver
  starts a new span with the same `trace_id` and a fresh `span_id`.
- **Async wait.** When a parent task transitions to
  `WAITING_CHILDREN`, its span stays open. Children's spans are
  siblings under the parent. The parent's span closes only when the
  parent task reaches a terminal state.

### 2.2 Span names

```
router.session.open
router.session.close
router.task.dispatch          # router admits a NewTask
router.task.transition        # state-machine transition
router.acl.evaluate
router.frame.send
router.frame.recv
router.db.query
router.storage.put
router.storage.get
router.llm.call               # router-side LLM service span
sdk.handler                   # span around user handler invocation
sdk.peer.spawn
sdk.peer.delegate
sdk.llm.call
sdk.files.fetch
sdk.files.put
```

Names use `dot.separated.lowercase`. The first segment identifies
the emitter (`router` / `sdk`); the rest is hierarchical action.

### 2.3 Required attributes

Every span carries:

| Attribute              | Type   | Notes                                       |
| ---------------------- | ------ | ------------------------------------------- |
| `service.name`         | string | `"router"` or `"sdk"`                       |
| `service.instance.id`  | string | Worker / agent process id                   |
| `deployment.env`       | string | `"prod"`, `"staging"`, etc.                 |
| `protocol.version`     | string | Frame protocol version                      |

Spans for in-task work add:

| Attribute           | Type   | Notes                                       |
| ------------------- | ------ | ------------------------------------------- |
| `bp.task_id`        | string | Current task                                |
| `bp.parent_task_id` | string | If any                                      |
| `bp.user_id`        | string | First-class user                            |
| `bp.session_id`     | string |                                             |
| `bp.agent_id`       | string | Agent owning the span                       |
| `bp.frame.type`     | string | For frame.send / frame.recv                 |
| `bp.acl.rule_name`  | string | For acl.evaluate                            |
| `bp.acl.effect`     | string | `"allow"` or `"deny"`                       |
| `bp.state.from`     | string | For task.transition                         |
| `bp.state.to`       | string |                                             |
| `bp.llm.model`      | string | For llm.call (alias, not raw provider id)   |
| `bp.llm.tokens.in`  | int    | For llm.call                                |
| `bp.llm.tokens.out` | int    | For llm.call                                |

The `bp.` prefix avoids clashes with the OpenTelemetry semantic
conventions namespace.

### 2.4 Span events vs. child spans

- Use **child spans** for operations with measurable duration
  (`db.query`, `llm.call`, `peer.spawn`).
- Use **span events** for instantaneous occurrences inside a span
  (`frame_acked`, `cache_hit`, `quota_check_passed`).

Don't create child spans for trivially short operations; they bloat
the trace UI.

### 2.5 Sampling

- **Errors:** always sampled. A 5xx span pulls the entire trace.
- **Tail-based sampling** is recommended in production: sample
  100% locally, ship to a collector that retains errors and high-
  latency traces and downsamples the rest.
- **Default head sampler** for low-traffic deployments: 10%.

## 3. Structured logs

### 3.1 Format

JSON, one object per line, stdout. No multi-line logs. No
`print()` calls anywhere — CI lints for it. Standard library
`logging` is configured with a JSON formatter at process startup.

### 3.2 Required fields

```jsonc
{
  "ts": "2026-04-26T12:34:56.123456Z",
  "level": "INFO",
  "logger": "router.dispatch",
  "trace_id": "...",
  "span_id": "...",
  "event": "task_dispatched",
  "service": "router",
  "service.instance.id": "router-7c4a",
  "deployment.env": "prod"
}
```

Plus contextually-bound fields where applicable: `bp.task_id`,
`bp.user_id`, `bp.session_id`, `bp.agent_id`. The SDK's `ctx.log`
is pre-bound with these.

### 3.3 `event` field

Lowercase, snake_case, action-like:

```
session_opened           session_closed
task_admitted            task_dispatched
task_transitioned        task_completed
frame_received           frame_sent           frame_dropped
acl_decision             quota_exceeded
agent_connected          agent_disconnected   agent_resumed
storage_uploaded         storage_deleted
llm_call_start           llm_call_finished
handler_error            transport_error
```

Free-form messages are permitted in the `message` field for human
readers, but the canonical signal is `event` plus structured fields.

### 3.4 Levels

| Level    | Use                                                      |
| -------- | -------------------------------------------------------- |
| DEBUG    | Verbose diagnostics; off in production.                  |
| INFO     | Normal lifecycle events (connect, dispatch, complete).   |
| WARNING  | Recoverable anomalies (retry, fallback, slow path).      |
| ERROR    | Task failures, transport failures, validation rejects.   |
| CRITICAL | Process-level emergencies (DB unreachable, OOM).         |

### 3.5 Privacy

By default, the SDK and router log:

- Prompts: only token counts and a 16-char truncated SHA-256.
- LLM responses: only token counts and finish reason.
- File contents: never. Filenames and SHA-256 only.
- Auth tokens / API keys: never. Even on errors.

A development-only flag (`BP_LOG_PROMPTS=1`) enables full prompt
logging. Refused in production by a startup check that rejects the
flag if `deployment.env=prod`.

## 4. Metrics

### 4.1 Naming

Prometheus exposition. Names follow `<service>_<subject>_<unit>`,
lowercased:

```
router_frames_total{direction, type, agent_id}                          counter
router_frame_size_bytes{direction, type}                                histogram
router_task_state_transitions_total{from, to}                           counter
router_task_duration_seconds{terminal_state}                            histogram
router_task_active_count{state}                                         gauge
router_acl_decisions_total{decision, effect, rule_name}                 counter
router_quota_exceeded_total{counter, user_tier}                         counter
router_ws_connected_agents_count                                        gauge
router_ws_disconnects_total{reason}                                     counter
router_db_query_duration_seconds{query}                                 histogram
router_storage_bytes_total{backend, op}                                 counter
router_storage_op_duration_seconds{backend, op}                         histogram
router_llm_calls_total{model, provider, status}                         counter
router_llm_tokens_total{model, direction}                               counter
router_llm_cost_microusd_total{model}                                   counter

sdk_handler_duration_seconds{agent_id, status}                          histogram
sdk_pending_acks_count{agent_id}                                        gauge
sdk_pending_results_count{agent_id}                                     gauge
sdk_reconnects_total{agent_id, reason}                                  counter
```

### 4.2 Cardinality rules

Permitted as labels: `direction`, `type`, `state`, `effect`,
`rule_name`, `reason`, `model` (alias, not raw), `provider`,
`status`, `backend`, `op`, `agent_id` (bounded set), `user_tier`
(bounded set).

**Forbidden** as labels: `task_id`, `user_id`, `session_id`,
`trace_id`, `correlation_id`, raw error strings, free-form prompts,
file paths.

The Pydantic Settings layer enforces an allowlist of label keys per
metric at registration time; new labels require code review.

### 4.3 Histogram buckets

- Latencies: default OTel buckets (`5ms, 10ms, 25ms, 50ms, 100ms,
  250ms, 500ms, 1s, 2.5s, 5s, 10s, 30s`).
- Sizes: `1KB, 4KB, 16KB, 64KB, 256KB, 1MB`.

Don't tune buckets per-metric without a strong reason — uniform
buckets make cross-metric comparison easier.

## 5. Dashboards and SLOs

The repo ships a `dashboards/` directory of Grafana JSON definitions,
keyed off the metric names above. Defaults:

**Router health (one row per panel):**

- `router_ws_connected_agents_count` over time
- p50 / p95 / p99 of `router_task_duration_seconds` by terminal_state
- Rate of `router_task_state_transitions_total{to="failed"}`
- Rate of `router_acl_decisions_total{effect="deny"}`
- Top 10 `(rule_name, agent_id)` pairs in deny logs

**Per-user / per-tier:**

- Task admit rate by `user_tier`
- Quota exhaustion rate by `counter` and `user_tier`
- LLM cost burn (`router_llm_cost_microusd_total`)

**SLOs (deployment-local; documented as defaults):**

- 99% of `task.dispatch` admits within 100 ms (router-side).
- 99% of frame acks within 1 s.
- < 0.1% of tasks reach `failed` due to `transport_error`.
- < 1% of agent reconnects within a 5-minute window per agent_id.

## 6. Local development

A single `docker-compose.observability.yml` brings up:

- Jaeger (OTel collector + UI)
- Prometheus (scrapes router `/metrics`)
- Grafana (pre-loaded dashboards)
- Loki (optional, for log search)

Pointed at by `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` and
`PROMETHEUS_SCRAPE=true`. Default in the `docker/` directory.

## 7. Anti-patterns

- **Logging the same thing as a span event and a log line.** Pick
  one — span events for in-trace context, logs for everything else.
- **Adding a metric for "this is what I want to debug today."**
  Metrics are budget; reach for a span event or log first.
- **Including `task_id` in a metric label.** Always wrong. Use a
  log line or a span event.
- **Free-form error strings as labels.** `error="connection refused on agent_id=foo"`
  blows up cardinality. Bucket errors into a small enum
  (`reason="transport_error" | "ack_timeout" | ...`).
- **One span per await.** Spans should map to operations a human
  cares about, not to every coroutine.

## 8. Testing observability

- **Unit:** `assert_emitted_event(log, "task_dispatched", task_id=...)`
  helper in the test harness.
- **Integration:** the `TestRouter` (`sdk/services.md` §7) collects
  emitted spans/logs/metrics into a typed buffer; tests assert on
  shape, not exact values.
- **CI smoke:** the `dashboards/` JSON is validated against the
  metric registry on every PR — a panel referencing a non-existent
  metric fails CI.
