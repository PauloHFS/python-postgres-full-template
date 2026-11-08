"""Coletor periódico de métricas de filas e dead letters do Postgres.

Métricas expostas no OpenTelemetry:
- `jobs.queue.size`: contagem de jobs por queue_name e status ('todo', 'doing')
- `jobs.queue.oldest_age`: idade em segundos do job 'todo' mais antigo por queue_name
- `jobs.dead_letters.size`: contagem de registros em dead_letters por status ('pending', 'resolved', 'discarded')
"""

from __future__ import annotations

import asyncio

from opentelemetry import metrics
from sqlalchemy import text

from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal

log = get_logger("jobs.metrics")

QUEUE_DEPTH_SQL = """
    SELECT queue_name, status, count(*) AS count
    FROM procrastinate_jobs
    WHERE status IN ('todo', 'doing')
    GROUP BY queue_name, status;
"""

OLDEST_JOB_AGE_SQL = """
    SELECT queue_name, extract(epoch FROM (now() - min(scheduled_at)))::double precision AS age_seconds
    FROM procrastinate_jobs
    WHERE status = 'todo' AND (scheduled_at IS NULL OR scheduled_at <= now())
    GROUP BY queue_name;
"""

DEAD_LETTERS_SQL = """
    SELECT status, count(*) AS count
    FROM dead_letters
    GROUP BY status;
"""

_meter = metrics.get_meter("lean.jobs.queue")

queue_size_gauge = _meter.create_gauge(
    name="jobs.queue.size",
    unit="1",
    description="Tamanho atual da fila de jobs por queue_name e status",
)

queue_oldest_age_gauge = _meter.create_gauge(
    name="jobs.queue.oldest_age",
    unit="s",
    description="Idade em segundos do job pendente (todo) mais antigo na fila",
)

dead_letters_gauge = _meter.create_gauge(
    name="jobs.dead_letters.size",
    unit="1",
    description="Contagem de dead letters por status",
)

_seen_queues: set[str] = {"email"}
_QUEUE_STATUSES: tuple[str, ...] = ("todo", "doing")
_DEAD_LETTER_STATUSES: tuple[str, ...] = ("pending", "retried", "discarded")


async def collect_queue_metrics_once() -> None:
    """Executa uma rodada de leitura e registro de métricas de fila e dead letters."""
    try:
        async with AsyncSessionLocal() as session:
            # 1. Tamanho da fila (reseta para 0 filas que esvaziaram)
            depth_rows = (await session.execute(text(QUEUE_DEPTH_SQL))).mappings().all()
            depth_map: dict[tuple[str, str], int] = {}
            for row in depth_rows:
                queue_name = str(row["queue_name"])
                status = str(row["status"])
                _seen_queues.add(queue_name)
                depth_map[(queue_name, status)] = int(row["count"])

            # 2. Idade do job mais antigo (reseta para 0.0 filas sem job 'todo')
            age_rows = (await session.execute(text(OLDEST_JOB_AGE_SQL))).mappings().all()
            age_map: dict[str, float] = {}
            for row in age_rows:
                queue_name = str(row["queue_name"])
                _seen_queues.add(queue_name)
                age_map[queue_name] = max(0.0, float(row["age_seconds"] or 0.0))

            for queue_name in sorted(_seen_queues):
                for status in _QUEUE_STATUSES:
                    queue_size_gauge.set(
                        depth_map.get((queue_name, status), 0),
                        {"queue": queue_name, "status": status},
                    )
                queue_oldest_age_gauge.set(
                    age_map.get(queue_name, 0.0),
                    {"queue": queue_name},
                )

            # 3. Dead letters (garante zero para status conhecidos quando vazio)
            dl_rows = (await session.execute(text(DEAD_LETTERS_SQL))).mappings().all()
            dl_map: dict[str, int] = {str(row["status"]): int(row["count"]) for row in dl_rows}
            for status in sorted(set(_DEAD_LETTER_STATUSES) | set(dl_map)):
                dead_letters_gauge.set(dl_map.get(status, 0), {"status": status})
    except Exception:  # noqa: BLE001 — métrica periódica não deve derrubar o worker
        log.warning("falha ao coletar métricas da fila Procrastinate/dead_letters", exc_info=True)


async def queue_metrics_collector_loop(interval_seconds: int = 15) -> None:
    """Loop assíncrono que coleta métricas de fila periodicamente."""
    while True:
        await collect_queue_metrics_once()
        await asyncio.sleep(interval_seconds)
