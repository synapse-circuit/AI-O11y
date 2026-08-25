"""OpenTelemetry and Grafana Agent Observability wiring.

Traces and metrics go out over standard OTLP to whatever endpoint
OTEL_EXPORTER_OTLP_ENDPOINT points at (the Grafana Cloud OTLP gateway).
Generation and tool-execution records go out through the agento11y
Client, which reads its own AGENTO11Y_* env vars.

Both are optional at import time: if the OTLP env vars are missing,
tracing/metrics fall back to in-process no-op providers so the app still
runs locally without a Grafana Cloud connection.
"""

import os

from agento11y import Client
from dotenv import load_dotenv
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

load_dotenv()

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "ai-o11y-agent-demo")


def configure_tracing() -> trace.Tracer:
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    trace.set_tracer_provider(provider)
    return trace.get_tracer(SERVICE_NAME)


def configure_metrics() -> None:
    resource = Resource.create({"service.name": SERVICE_NAME})
    readers = []

    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))

    provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(provider)


def get_agento11y_client() -> Client:
    """Create the agento11y client. Reads AGENTO11Y_* env vars automatically."""
    return Client()
