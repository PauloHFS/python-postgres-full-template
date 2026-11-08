"""OpenTelemetry + Sentry opcional (AD-011).

Ativado quando `OTEL_EXPORTER_OTLP_ENDPOINT` ou `SENTRY_DSN` estão setados.
Sem env, é no-op — não custa nada.

Decisão: vendor-agnostic no app. Quem roteia OTLP pro vendor (Grafana Tempo,
Honeycomb, Datadog, etc.) é o operador.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.metrics import Meter
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.core.config import Settings, settings

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def build_resource(config: Settings | None = None) -> Resource:
    """Resource com a identidade do serviço (app, versão e ambiente)."""
    cfg = config if config is not None else settings
    return Resource.create(
        {
            SERVICE_NAME: cfg.app_name,
            "service.version": cfg.app_version,
            "deployment.environment": cfg.app_env,
            "service.namespace": "lean",
        }
    )


def build_sampler(config: Settings | None = None) -> Sampler:
    """Mapeia `OTEL_TRACES_SAMPLER` para o sampler do SDK.

    Valores aceitos: "always_on", "always_off", "traceidratio" e
    "parentbased_traceidratio". Ausente → parentbased_traceidratio.
    """
    cfg = config if config is not None else settings
    name = (cfg.otel_traces_sampler or "").lower().replace("-", "_")
    ratio = cfg.otel_sample_ratio

    if name == "always_on":
        return ALWAYS_ON
    if name == "always_off":
        return ALWAYS_OFF
    if name == "traceidratio":
        return TraceIdRatioBased(ratio)
    return ParentBased(TraceIdRatioBased(ratio))


def setup_tracing() -> TracerProvider | None:
    """Configura propagator e provider (API ou worker). Retorna None se desligado.

    O propagator W3C é sempre instalado, mesmo sem exporter, para que o
    contexto de entrada (`traceparent`) alimente logs e spans manuais.
    """
    global _tracer_provider  # noqa: PLW0603 (singleton do provider do processo)

    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )

    if _tracer_provider is not None:
        return _tracer_provider
    if not settings.otel_exporter_otlp_endpoint:
        return None

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=build_resource(settings),
        sampler=build_sampler(settings),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    _tracer_provider = provider
    return provider


def setup_metrics() -> MeterProvider | None:
    """Configura MeterProvider OTLP para API ou worker. Retorna None se desligado."""
    global _meter_provider  # noqa: PLW0603

    if _meter_provider is not None:
        return _meter_provider
    if not settings.otel_exporter_otlp_endpoint:
        return None

    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    exporter = OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000)
    provider = MeterProvider(
        resource=build_resource(settings),
        metric_readers=[reader],
    )
    metrics.set_meter_provider(provider)
    _meter_provider = provider
    return provider


def shutdown_tracing() -> None:
    """Flush + shutdown dos providers (evita perder spans e métricas no container)."""
    global _tracer_provider, _meter_provider  # noqa: PLW0603
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
    if _meter_provider is not None:
        _meter_provider.shutdown()
        _meter_provider = None


def get_meter(name: str = "lean.meter") -> Meter:
    """Retorna um Meter globalmente registrado ou no-op."""
    return metrics.get_meter(name)


def setup_observability(app: FastAPI) -> TracerProvider | None:
    """Inicializa tracing e métricas se `OTEL_EXPORTER_OTLP_ENDPOINT` estiver setado."""
    provider = setup_tracing()
    if provider is None:
        return None

    setup_metrics()

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    LoggingInstrumentor().instrument(set_logging_format=False)

    from app.db.session import engine

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)

    from app.core.logging import get_logger

    get_logger("otel").info(
        "observabilidade inicializada (tracing + métricas)",
        endpoint=settings.otel_exporter_otlp_endpoint,
        sampler=settings.otel_traces_sampler,
        ratio=settings.otel_sample_ratio,
    )
    return provider


def setup_sentry() -> None:
    """Inicializa Sentry se DSN setado."""
    if not settings.sentry_dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        release=f"{settings.app_name}@{settings.app_version}",
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
        ],
        # Profiling: custa CPU, deixa off por padrão
        profiles_sample_rate=0.0,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )


def inject_trace_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Anexa o contexto W3C corrente ao payload da fila (para o worker)."""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return {**payload, "trace_context": carrier}


def extract_trace_context(payload: Mapping[str, Any]) -> Context:
    """Extrai o contexto W3C do payload da fila (jobs antigos → contexto vazio)."""
    carrier = payload.get("trace_context")
    if not isinstance(carrier, dict):
        return Context()
    return TraceContextTextMapPropagator().extract(cast("dict[str, str]", carrier))


def current_trace_id() -> str | None:
    """hex do trace corrente, ou None quando não há span válido."""
    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else None


# Instrumentação de Jobs (métricas RED para background workers)
_jobs_meter = metrics.get_meter("lean.jobs")
_job_duration_histogram = _jobs_meter.create_histogram(
    name="jobs.execution.duration",
    unit="s",
    description="Duração da execução de jobs em segundos",
)
_job_outcomes_counter = _jobs_meter.create_counter(
    name="jobs.outcomes.total",
    unit="1",
    description="Total de execuções de jobs por status (success, retry, dead_letter, aborted)",
)


def record_job_metric(task_name: str, status: str, duration_s: float | None = None) -> None:
    """Registra métricas de execução de jobs no OpenTelemetry."""
    attributes = {"task": task_name, "status": status}
    _job_outcomes_counter.add(1, attributes)
    if duration_s is not None:
        _job_duration_histogram.record(duration_s, {"task": task_name, "status": status})


class JobTimer:
    """Helper contextual para medir duração e status de job."""

    def __init__(self, task_name: str) -> None:
        self.task_name = task_name
        self.start_time = 0.0
        self.status: str | None = None

    def __enter__(self) -> JobTimer:
        self.start_time = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        duration_s = max(0.0, time.perf_counter() - self.start_time)
        if self.status is not None:
            status = self.status
        elif exc_type is None:
            status = "success"
        elif exc_type.__name__ == "JobAborted":
            status = "aborted"
        else:
            status = "failure"
        record_job_metric(self.task_name, status, duration_s)
