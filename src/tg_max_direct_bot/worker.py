from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Optional

from .bridge import Bridge, PermanentEventError
from .database import Database

logger = logging.getLogger(__name__)
WORKER_IDLE_TIMEOUT_SECONDS = 1.0


class EventWorker:
    def __init__(self, database: Database, bridge: Bridge) -> None:
        self.db = database
        self.bridge = bridge
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="bridge-event-worker")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            await self._task

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            event = await self.db.claim_next()
            if event is None:
                self._wake.clear()
                # On Python 3.9 asyncio.TimeoutError is not the built-in
                # TimeoutError, so suppress the asyncio exception explicitly.
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=WORKER_IDLE_TIMEOUT_SECONDS)
                continue
            try:
                if event.source == "telegram":
                    await self.bridge.handle_telegram(event.payload)
                elif event.source == "max":
                    await self.bridge.handle_max(event.payload)
                else:
                    raise PermanentEventError(f"неизвестный источник {event.source}")
            except PermanentEventError as exc:
                logger.error("Событие %s:%s отклонено: %s", event.source, event.event_key, exc)
                await self.db.complete(event.id)
            except Exception as exc:
                logger.exception(
                    "Ошибка обработки %s:%s, попытка %s",
                    event.source,
                    event.event_key,
                    event.attempts,
                )
                await self.db.retry(event, str(exc))
            else:
                await self.db.complete(event.id)
