"""Testes para métricas de filas e dead letters (sem tocar Postgres)."""

from __future__ import annotations

from typing import Any

import pytest

from app.jobs import metrics as jobs_metrics


class _MockSession:
    def __init__(
        self,
        depth_rows: list[dict[str, Any]],
        age_rows: list[dict[str, Any]],
        dl_rows: list[dict[str, Any]],
    ) -> None:
        self.depth_rows = depth_rows
        self.age_rows = age_rows
        self.dl_rows = dl_rows
        self.query_index = 0

    async def execute(self, _sql: Any) -> _MockSession:
        return self

    def mappings(self) -> _MockSession:
        return self

    def all(self) -> list[dict[str, Any]]:
        self.query_index += 1
        if self.query_index == 1:
            return self.depth_rows
        if self.query_index == 2:
            return self.age_rows
        return self.dl_rows

    async def __aenter__(self) -> _MockSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_collect_queue_metrics_once_and_resets_drained_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    size_calls: list[tuple[int, dict[str, str]]] = []
    age_calls: list[tuple[float, dict[str, str]]] = []
    dl_calls: list[tuple[int, dict[str, str]]] = []

    def _record_size(val: int, attrs: dict[str, str]) -> None:
        size_calls.append((val, dict(attrs)))

    def _record_age(val: float, attrs: dict[str, str]) -> None:
        age_calls.append((val, dict(attrs)))

    def _record_dl(val: int, attrs: dict[str, str]) -> None:
        dl_calls.append((val, dict(attrs)))

    monkeypatch.setattr(jobs_metrics.queue_size_gauge, "set", _record_size)
    monkeypatch.setattr(jobs_metrics.queue_oldest_age_gauge, "set", _record_age)
    monkeypatch.setattr(jobs_metrics.dead_letters_gauge, "set", _record_dl)

    mock_session_full = _MockSession(
        depth_rows=[{"queue_name": "billing", "status": "todo", "count": 5}],
        age_rows=[{"queue_name": "billing", "age_seconds": 12.5}],
        dl_rows=[{"status": "pending", "count": 2}],
    )
    monkeypatch.setattr(jobs_metrics, "AsyncSessionLocal", lambda: mock_session_full)

    await jobs_metrics.collect_queue_metrics_once()
    assert mock_session_full.query_index == 3
    assert (5, {"queue": "billing", "status": "todo"}) in size_calls
    assert (0, {"queue": "billing", "status": "doing"}) in size_calls
    assert (12.5, {"queue": "billing"}) in age_calls
    assert (2, {"status": "pending"}) in dl_calls

    # Na rodada seguinte, a fila 'billing' esvaziou (0 linhas retornadas pelo GROUP BY)
    size_calls.clear()
    age_calls.clear()
    dl_calls.clear()

    mock_session_empty = _MockSession(depth_rows=[], age_rows=[], dl_rows=[])
    monkeypatch.setattr(jobs_metrics, "AsyncSessionLocal", lambda: mock_session_empty)

    await jobs_metrics.collect_queue_metrics_once()
    assert (0, {"queue": "billing", "status": "todo"}) in size_calls
    assert (0.0, {"queue": "billing"}) in age_calls
    assert (0, {"status": "pending"}) in dl_calls
