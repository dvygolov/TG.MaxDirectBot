from __future__ import annotations

import asyncio

import pytest

from tg_max_direct_bot import worker as worker_module
from tg_max_direct_bot.worker import EventWorker


class IdleDatabase:
    def __init__(self) -> None:
        self.claims = 0

    async def claim_next(self):
        self.claims += 1
        return None


@pytest.mark.asyncio
async def test_worker_survives_idle_timeout(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "WORKER_IDLE_TIMEOUT_SECONDS", 0.01)
    database = IdleDatabase()
    worker = EventWorker(database, object())  # type: ignore[arg-type]

    worker.start()
    await asyncio.sleep(0.04)

    assert worker._task is not None
    assert not worker._task.done()
    assert database.claims >= 2

    await worker.stop()
