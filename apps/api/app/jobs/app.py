"""Procrastinate App — entrypoint do sistema de filas.

Singleton do `procrastinate.App`. As tasks são declaradas nos módulos de
`app.jobs.tasks.*` com `register_task(...)`, que padroniza o nome
(`<queue>.<action>`) e envolve a task com a DLQ (`dead_letters`).

Notas:
- O worker do Procrastinate **exige um connector async** (`PsycopgConnector`);
  connectors sync (psycopg2/SQLAlchemy) servem só para deferir em contexto sync.
  Por isso usamos `PsycopgConnector` com a DSN libpq (`database_url_procrastinate`).
- As tasks são descobertas automaticamente no worker a partir do pacote
  `app.jobs.tasks`; cada módulo `.py` dentro dele é listado em `import_paths`.
  Quem for deferir importa a task diretamente, o que também registra o decorador.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import os
import pkgutil
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import procrastinate
from opentelemetry import trace
from procrastinate.exceptions import JobAborted
from procrastinate.jobs import Job
from procrastinate.retry import RetryStrategy
from procrastinate.tasks import Task

from app.core.config import settings
from app.core.jobs import check_job_rate_limit, record_dead_letter
from app.core.logging import get_logger
from app.core.observability import (
    JobTimer,
    extract_trace_context,
    inject_trace_context,
)

log = get_logger("jobs")

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(
        conninfo=settings.database_url_procrastinate,
    ),
    import_paths=[],
    worker_defaults={
        "concurrency": 4,
        "name": f"procrastinate-worker-{socket.gethostname()}-{os.getpid()}",
    },
)

# Queue/action viram `<queue>.<action>`: só minúsculas, dígitos, `_` e `-`.
_TASK_PART_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_task_part(value: str, field: str) -> None:
    if not _TASK_PART_RE.fullmatch(value):
        raise ValueError(f"{field} inválido: {value!r} (use minúsculas, sem ponto e sem espaço)")


def _context_job(ctx: Any) -> Job | None:
    """`ctx.job` do Procrastinate, quando o contexto está disponível."""
    job = getattr(ctx, "job", None)
    return job if isinstance(job, Job) else None


def _json_safe(value: Any) -> Any:
    """Normaliza kwargs para JSONB (datas/objetos viram string)."""
    return json.loads(json.dumps(value, default=str))


def _is_last_attempt(strategy: RetryStrategy | None, exc: Exception, job: Job | None) -> bool:
    """True quando o Procrastinate não tenta de novo (falha definitiva)."""
    if strategy is None or job is None:
        return True
    return strategy.get_retry_decision(exception=exc, job=job) is None


async def _record_failure(
    name: str,
    exc: Exception,
    job: Job | None,
    kwargs: dict[str, Any],
) -> None:
    """Grava a falha na DLQ; nunca mascara a exceção original."""
    job_id = job.id if job is not None else None
    try:
        await record_dead_letter(
            kind=name,
            key=str(job_id or ""),
            payload={
                "task": name,
                "kwargs": _json_safe(kwargs),
                "attempts": job.attempts if job is not None else 0,
            },
            error=f"{type(exc).__name__}: {exc}",
            original_job_id=job_id,
        )
    except Exception:  # noqa: BLE001 — não mascara a exceção original
        log.exception("falha ao gravar dead letter", kind=name)


def register_task(
    queue: str,
    action: str,
    *,
    max_retries: int = 3,
    pass_context: bool = True,
    **task_kwargs: Any,
) -> Callable[[Callable[..., Awaitable[Any]]], Task[..., Any, Any]]:
    """Registra uma task com nome `<queue>.<action>` e DLQ automática.

    `max_retries` é quantas vezes o Procrastinate tenta de novo (0 = uma
    execução só, 3 = até 4 execuções). A falha só vira dead letter na última
    tentativa — a mesma decisão de retry do Procrastinate —, então a DLQ tem
    uma linha por job que desistiu, e não uma por tentativa.
    """
    _validate_task_part(queue, "queue")
    _validate_task_part(action, "action")
    name = f"{queue}.{action}"
    strategy = RetryStrategy(max_attempts=max_retries) if max_retries > 0 else None

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Task[..., Any, Any]:
        params = inspect.signature(fn).parameters
        accepts_trace_context = "trace_context" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

        @functools.wraps(fn)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            # Propaga span e métrica de execução do job
            parent_ctx = extract_trace_context(kwargs)
            call_kwargs = (
                kwargs
                if accepts_trace_context
                else {k: v for k, v in kwargs.items() if k != "trace_context"}
            )
            tracer = trace.get_tracer("lean.worker")
            with (
                tracer.start_as_current_span(name, context=parent_ctx),
                JobTimer(name) as timer,
            ):
                try:
                    return await fn(*args, **call_kwargs)
                except JobAborted:
                    # Job abortado não é falha: o worker marca como aborted.
                    timer.status = "aborted"
                    raise
                except Exception as exc:
                    job = _context_job(args[0]) if pass_context and args else None
                    if _is_last_attempt(strategy, exc, job):
                        timer.status = "dead_letter"
                        await _record_failure(name, exc, job, call_kwargs)
                    else:
                        timer.status = "retry"
                    raise

        return app.task(
            queue=queue,
            name=name,
            retry=strategy if strategy is not None else False,
            pass_context=pass_context,
            **task_kwargs,
        )(wrapped)

    return decorator


async def defer_job(
    task: Task[..., Any, Any],
    *,
    rate_limit_kind: str | None = None,
    rate_limit_key: str = "",
    rate_limit_limit: int | None = None,
    rate_limit_window_s: int = 60,
    **kwargs: Any,
) -> int:
    """Enfileira a task, aplicando o rate limit por kind/chave se configurado.

    Injeta automaticamente o contexto OTel W3C no payload se houver span ativo.
    """
    if rate_limit_kind is not None and rate_limit_limit is not None:
        await check_job_rate_limit(
            rate_limit_kind,
            rate_limit_key,
            rate_limit_limit,
            rate_limit_window_s,
        )

    if "trace_context" not in kwargs:
        injected = inject_trace_context(kwargs)
        task_payload = injected if injected.get("trace_context") else kwargs
    else:
        task_payload = kwargs
    return await task.defer_async(**task_payload)


def _discover_task_modules(package: str = "app.jobs.tasks") -> list[str]:
    """Lista todos os módulos `.py` dentro do pacote de tasks.

    A descoberta acontece no final deste módulo — depois que `register_task`
    já foi definido — para evitar import circular com `app.jobs.tasks.*`.
    """
    try:
        package_mod = importlib.import_module(package)
    except Exception:  # noqa: BLE001 — fallback seguro se o pacote não existir
        log.warning("não foi possível descobrir módulos de tasks", package=package)
        return []

    path = getattr(package_mod, "__path__", None)
    if path is None:
        return []

    prefix = f"{package}."
    return [
        f"{prefix}{name}"
        for _, name, is_pkg in pkgutil.iter_modules(path)
        if not is_pkg and name != "__init__"
    ]


app.import_paths = _discover_task_modules()


def get_app() -> procrastinate.App:
    return app
