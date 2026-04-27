"""bp_router.observability.metrics — Prometheus metric registry.

See `docs/design/observability.md` §4 for the canonical metric set.
The registry is a module-level singleton so any subsystem can import
and increment without dependency injection.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


# Module-level registry. Tests reset by replacing with a fresh
# CollectorRegistry and re-creating the metric handles.
REGISTRY = CollectorRegistry()


# ---------------------------------------------------------------------------
# Frame / WS
# ---------------------------------------------------------------------------

frames_total = Counter(
    "router_frames_total",
    "WebSocket frames sent or received, by direction/type/agent.",
    ["direction", "type", "agent_id"],
    registry=REGISTRY,
)
frame_size_bytes = Histogram(
    "router_frame_size_bytes",
    "WebSocket frame size in bytes.",
    ["direction", "type"],
    buckets=(1024, 4096, 16384, 65536, 262144, 1_048_576),
    registry=REGISTRY,
)
ws_connected_agents = Gauge(
    "router_ws_connected_agents_count",
    "Currently connected agent sockets.",
    registry=REGISTRY,
)
ws_disconnects_total = Counter(
    "router_ws_disconnects_total",
    "Agent socket disconnects, by reason.",
    ["reason"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

task_state_transitions_total = Counter(
    "router_task_state_transitions_total",
    "Task state transitions.",
    ["from", "to"],
    registry=REGISTRY,
)
task_duration_seconds = Histogram(
    "router_task_duration_seconds",
    "Task duration from creation to terminal state.",
    ["terminal_state"],
    registry=REGISTRY,
)
task_active_count = Gauge(
    "router_task_active_count",
    "Tasks currently in a non-terminal state.",
    ["state"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# ACL / quotas
# ---------------------------------------------------------------------------

acl_decisions_total = Counter(
    "router_acl_decisions_total",
    "ACL evaluation outcomes.",
    ["decision", "effect", "rule_name"],
    registry=REGISTRY,
)
quota_exceeded_total = Counter(
    "router_quota_exceeded_total",
    "Quota check denials.",
    ["counter", "user_tier"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# DB / storage
# ---------------------------------------------------------------------------

db_query_duration_seconds = Histogram(
    "router_db_query_duration_seconds",
    "Time spent on a single DB query.",
    ["query"],
    registry=REGISTRY,
)
storage_bytes_total = Counter(
    "router_storage_bytes_total",
    "Bytes uploaded or downloaded by backend and operation.",
    ["backend", "op"],
    registry=REGISTRY,
)
storage_op_duration_seconds = Histogram(
    "router_storage_op_duration_seconds",
    "Time spent on a single storage operation.",
    ["backend", "op"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "router_llm_calls_total",
    "LLM calls by model alias, provider, and status.",
    ["model", "provider", "status"],
    registry=REGISTRY,
)
llm_tokens_total = Counter(
    "router_llm_tokens_total",
    "LLM tokens consumed.",
    ["model", "direction"],
    registry=REGISTRY,
)
llm_cost_microusd_total = Counter(
    "router_llm_cost_microusd_total",
    "LLM cost in micro-USD.",
    ["model"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Setup / exposition
# ---------------------------------------------------------------------------


def configure_metrics() -> None:
    """No-op for now; the registry is populated at import time. Reserved
    for future setup (default labels, label allowlist enforcement)."""
    return None


def render_exposition() -> bytes:
    """Render the Prometheus text exposition for the /metrics endpoint."""
    return generate_latest(REGISTRY)
