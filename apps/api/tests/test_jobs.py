"""Jobs: nomes, rate limit por janela fixa e DLQ — tudo sem tocar Postgres."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from procrastinate.exceptions import JobAborted
from procrastinate.jobs import Job

from app.core import jobs as jobs_core
from app.core.observability import current_trace_id, inject_trace_context
from app.jobs import app as jobs_app
from app.jobs.app import defer_job, register_task
from app.jobs.tasks.email import send_email


def _job(job_id: int = 99, attempts: int = 0) -> Job:
    return Job(
        id=job_id,
        status="doing",
        queue="test",
        lock=None,
        queueing_lock=None,
        task_name="test.boom",
        task_kwargs={},
        attempts=attempts,
    )


def test_register_task_rejects_invalid_queue_and_action() -> None:
    for bad in ("Email", "email.send", "email send", ""):
        with pytest.raises(ValueError):
            register_task(bad, "send")
    for bad in ("Send", "send.x", "send x", ""):
        with pytest.raises(ValueError):
            register_task("email", bad)


async def test_register_task_names_queue_and_action() -> None:
    @register_task("test", "echo", max_retries=0)
    async def echo(ctx: Any, value: str) -> str:
        return value

    assert echo.name == "test.echo"
    assert echo.queue == "test"


def test_rate_limit_sql_uses_fixed_epoch_window_and_bind_params() -> None:
    sql = jobs_core.RATE_LIMIT_SQL
    assert "floor(extract(epoch from now()) / :window) * :window" in sql
    assert "to_timestamp" in sql
    assert "ON CONFLICT (kind, key, window_start)" in sql
    for bind in (":kind", ":key", ":window"):
        assert bind in sql


class _RecordingSession:
    """Duck-typed AsyncSession suficiente para `async with ... begin()`."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.params: dict[str, Any] | None = None

    async def execute(self, _sql: Any, params: dict[str, Any]) -> _RecordingSession:
        self.params = params
        return self

    def mappings(self) -> _RecordingSession:
        return self

    def first(self) -> dict[str, Any] | None:
        return self.row

    def begin(self) -> _RecordingSession:
        return self

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_record_dead_letter_inserts_payload_and_returns_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RecordingSession({"id": 7})
    monkeypatch.setattr(jobs_core, "AsyncSessionLocal", lambda: session)

    dead_letter_id = await jobs_core.record_dead_letter(
        kind="email.send",
        key="user:1",
        payload={"to": "a@b.com"},
        error="RuntimeError: boom",
        original_job_id=12,
    )

    assert dead_letter_id == 7
    assert session.params is not None
    assert session.params["kind"] == "email.send"
    assert session.params["key"] == "user:1"
    assert session.params["original_job_id"] == 12
    assert '"to": "a@b.com"' in session.params["payload"]


async def test_check_job_rate_limit_raises_above_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RecordingSession({"count": 3})
    monkeypatch.setattr(jobs_core, "AsyncSessionLocal", lambda: session)

    assert await jobs_core.check_job_rate_limit("email.send", "user:1", 3, 60) == 3

    with pytest.raises(jobs_core.JobRateLimitExceeded):
        await jobs_core.check_job_rate_limit("email.send", "user:1", 2, 60)


async def test_check_job_rate_limit_validates_window() -> None:
    with pytest.raises(ValueError):
        # Chega no guard antes de abrir sessão (não toca Postgres).
        await jobs_core.check_job_rate_limit("email.send", "k", 1, 0)


async def test_register_task_records_dead_letter_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(jobs_app, "record_dead_letter", _record)

    @register_task("test", "boom", max_retries=0)
    async def boom(ctx: Any, value: str) -> None:
        raise RuntimeError("falhou")

    ctx = SimpleNamespace(job=_job())
    with pytest.raises(RuntimeError, match="falhou"):
        await boom(ctx, value="x")

    assert calls[0]["kind"] == "test.boom"
    assert calls[0]["original_job_id"] == 99
    assert calls[0]["payload"]["kwargs"] == {"value": "x"}
    assert calls[0]["payload"]["attempts"] == 0


async def test_register_task_skips_dead_letter_while_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Com retry pendente a falha não vira dead letter (uma linha por job)."""
    calls: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(jobs_app, "record_dead_letter", _record)

    @register_task("test", "boom-retry", max_retries=3)
    async def boom(ctx: Any, value: str) -> None:
        raise RuntimeError("falhou")

    ctx = SimpleNamespace(job=_job(attempts=0))
    with pytest.raises(RuntimeError, match="falhou"):
        await boom(ctx, value="x")

    assert calls == []


async def test_register_task_records_dead_letter_on_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(jobs_app, "record_dead_letter", _record)

    @register_task("test", "boom-last", max_retries=3)
    async def boom(ctx: Any, value: str) -> None:
        raise RuntimeError("falhou")

    ctx = SimpleNamespace(job=_job(attempts=3))
    with pytest.raises(RuntimeError, match="falhou"):
        await boom(ctx, value="x")

    assert len(calls) == 1
    assert calls[0]["payload"]["attempts"] == 3


async def test_register_task_ignores_aborted_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(jobs_app, "record_dead_letter", _record)

    @register_task("test", "boom-abort", max_retries=0)
    async def boom(ctx: Any, value: str) -> None:
        raise JobAborted

    ctx = SimpleNamespace(job=_job())
    with pytest.raises(JobAborted):
        await boom(ctx, value="x")

    assert calls == []


async def test_defer_job_skips_rate_limit_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _check(*_args: Any, **_kwargs: Any) -> int:
        nonlocal called
        called = True
        return 1

    async def _defer(**_kwargs: Any) -> int:
        return 42

    monkeypatch.setattr(jobs_app, "check_job_rate_limit", _check)
    monkeypatch.setattr(send_email, "defer_async", _defer)

    assert await defer_job(send_email, to="a@b.com", subject="hi") == 42
    assert called is False


def test_import_paths_auto_discovers_task_modules() -> None:
    assert "app.jobs.tasks.email" in jobs_app.app.import_paths
    assert all(mod.startswith("app.jobs.tasks.") for mod in jobs_app.app.import_paths)
    assert "app.jobs.tasks.__init__" not in jobs_app.app.import_paths


async def test_register_task_propagates_and_strips_trace_context_when_not_declared() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    captured_trace_id: str | None = None

    @register_task("test", "trace-strip", max_retries=0)
    async def simple_task(ctx: Any, value: str) -> str:
        nonlocal captured_trace_id
        del ctx
        captured_trace_id = current_trace_id()
        return value

    with tracer.start_as_current_span("http.request") as parent_span:
        expected_trace_id = format(parent_span.get_span_context().trace_id, "032x")
        payload = inject_trace_context({"value": "ok"})

    ctx = SimpleNamespace(job=_job())
    result = await simple_task(ctx, **payload)

    assert result == "ok"
    assert captured_trace_id == expected_trace_id
