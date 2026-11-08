"""Testes de tracing/observabilidade (sem rede e sem banco)."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision

from app.core.config import settings
from app.core.logging import add_trace_context
from app.core.observability import (
    JobTimer,
    build_resource,
    build_sampler,
    current_trace_id,
    extract_trace_context,
    inject_trace_context,
    record_job_metric,
    setup_metrics,
)


def _tracer_with_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_build_resource_identity() -> None:
    resource = build_resource(settings)

    assert resource.attributes["service.name"] == settings.app_name
    assert resource.attributes["service.version"] == settings.app_version
    assert resource.attributes["deployment.environment"] == settings.app_env
    assert resource.attributes["service.namespace"] == "lean"


def test_build_sampler_always_off_drops() -> None:
    sampler = build_sampler(settings.model_copy(update={"otel_traces_sampler": "always_off"}))

    assert sampler.should_sample(Context(), 0x1234, "test").decision is Decision.DROP


def test_build_sampler_traceidratio_respects_ratio() -> None:
    off = build_sampler(
        settings.model_copy(
            update={"otel_traces_sampler": "traceidratio", "otel_sample_ratio": 0.0}
        )
    )
    on = build_sampler(
        settings.model_copy(
            update={"otel_traces_sampler": "traceidratio", "otel_sample_ratio": 1.0}
        )
    )

    assert off.should_sample(Context(), 0x1234, "test").decision is Decision.DROP
    assert on.should_sample(Context(), 0x1234, "test").decision is Decision.RECORD_AND_SAMPLE


def test_inject_extract_roundtrip() -> None:
    tracer, exporter = _tracer_with_exporter()

    with tracer.start_as_current_span("parent") as span:
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        payload = inject_trace_context({"to": "a@b.com"})

    assert payload["to"] == "a@b.com"
    extracted = trace.get_current_span(extract_trace_context(payload))
    assert format(extracted.get_span_context().trace_id, "032x") == expected_trace_id
    assert len(exporter.get_finished_spans()) == 1


def test_current_trace_id_none_outside_span() -> None:
    assert current_trace_id() is None


def test_current_trace_id_inside_span() -> None:
    tracer, _ = _tracer_with_exporter()

    with tracer.start_as_current_span("current") as span:
        expected = format(span.get_span_context().trace_id, "032x")
        assert current_trace_id() == expected


def test_add_trace_context_without_span() -> None:
    event = add_trace_context(None, "info", {})

    assert event == {"trace_id": None, "span_id": None}


def test_add_trace_context_with_span() -> None:
    tracer, _ = _tracer_with_exporter()

    with tracer.start_as_current_span("log") as span:
        span_context = span.get_span_context()
        event = add_trace_context(None, "info", {})

    assert event["trace_id"] == format(span_context.trace_id, "032x")
    assert event["span_id"] == format(span_context.span_id, "016x")


def test_inject_trace_context_without_existing_key() -> None:
    assert inject_trace_context({"to": "a@b.com"})["to"] == "a@b.com"


def test_extract_trace_context_missing_returns_none() -> None:
    context = extract_trace_context({"to": "a@b.com"})

    assert not trace.get_current_span(context).get_span_context().is_valid


def test_setup_metrics_disabled_without_endpoint() -> None:
    assert setup_metrics() is None


def test_job_timer_and_metrics_recording() -> None:
    # Não deve levantar exceções mesmo com meter padrão
    record_job_metric("email.send", "success", duration_s=0.05)
    record_job_metric("email.send", "retry")

    with JobTimer("test.job"):
        pass
