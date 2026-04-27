"""bp_router.observability.tracing — OpenTelemetry setup."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bp_router.settings import Settings

logger = logging.getLogger(__name__)


def configure_tracing(settings: "Settings") -> None:
    """Initialise the OTel tracer provider.

    Disabled when `settings.otel_endpoint` is None. When enabled,
    exports to OTLP/HTTP at the configured endpoint with `service.name
    = settings.otel_service_name`. Tail-based sampling is recommended;
    here we ship 100% locally and rely on a downstream collector.
    """
    if not settings.otel_endpoint:
        logger.info("tracing_disabled", extra={"event": "tracing_disabled"})
        return

    # Implementation note: lazy import opentelemetry to keep startup fast
    # when tracing is disabled.
    from opentelemetry import trace  # noqa: PLC0415
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
    )
    trace.set_tracer_provider(provider)
    logger.info(
        "tracing_enabled",
        extra={"event": "tracing_enabled", "endpoint": settings.otel_endpoint},
    )
