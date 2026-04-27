"""bp_sdk._metrics — Tiny Prometheus wrapper for ctx.metric().

Internal — agents should use `ctx.metric(...)` instead of importing this.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

_metrics: dict[str, Any] = {}


def record(name: str, value: float, labels: dict[str, str]) -> None:
    """Record a metric. Auto-creates a Counter on first use of `name`.

    For more control, use prometheus_client directly. This is the
    convenience path.
    """
    metric = _metrics.get(name)
    if metric is None:
        metric = Counter(name, name, labelnames=list(labels.keys()), registry=REGISTRY)
        _metrics[name] = metric
    if labels:
        metric.labels(**labels).inc(value)
    else:
        metric.inc(value)


def gauge(name: str, value: float, labels: dict[str, str]) -> None:
    metric = _metrics.get(name)
    if metric is None:
        metric = Gauge(name, name, labelnames=list(labels.keys()), registry=REGISTRY)
        _metrics[name] = metric
    if labels:
        metric.labels(**labels).set(value)
    else:
        metric.set(value)


def observe(name: str, value: float, labels: dict[str, str]) -> None:
    metric = _metrics.get(name)
    if metric is None:
        metric = Histogram(name, name, labelnames=list(labels.keys()), registry=REGISTRY)
        _metrics[name] = metric
    if labels:
        metric.labels(**labels).observe(value)
    else:
        metric.observe(value)
