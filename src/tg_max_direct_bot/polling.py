from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Optional

import httpx

from .bridge import Bridge
from .clients import ExternalAPIError, MaxClient, TelegramClient
from .config import Settings
from .database import Database
from .events import max_event_key
from .worker import EventWorker

logger = logging.getLogger(__name__)

TELEGRAM_OFFSET_KEY = "telegram_polling_offset"
MAX_MARKER_KEY = "max_polling_marker"


def _parse_cursor(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class PollingRunner:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        telegram: TelegramClient,
        max_client: MaxClient,
        worker: EventWorker,
    ) -> None:
        self.settings = settings
        self.db = database
        self.telegram = telegram
        self.max = max_client
        self.worker = worker

    async def prepare(self) -> None:
        me = await self.telegram.get_me()
        if not me.get("can_connect_to_business"):
            logger.warning(
                "Telegram Business Mode пока не включён для @%s. "
                "Polling продолжит работу, но business_message не придут "
                "до включения в @BotFather.",
                me.get("username", "unknown"),
            )
        await self.telegram.delete_webhook(self.settings.polling_drop_pending_updates)
        deleted = await self.max.delete_all_subscriptions()
        logger.info(
            "Polling подготовлен: Telegram webhook отключён, удалено MAX subscriptions: %s",
            deleted,
        )

    async def run(self) -> None:
        await self.prepare()
        telegram_task = asyncio.create_task(self.telegram_loop(), name="telegram-long-polling")
        max_task = asyncio.create_task(self.max_loop(), name="max-long-polling")
        tasks = (telegram_task, max_task)
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def telegram_loop(self) -> None:
        offset = _parse_cursor(await self.db.get_state(TELEGRAM_OFFSET_KEY))
        while True:
            try:
                updates = await self.telegram.get_updates(
                    offset, self.settings.polling_timeout_seconds
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    inserted = await self.db.enqueue("telegram", str(update_id), update)
                    if inserted:
                        self.worker.wake()
                    offset = update_id + 1
                    await self.db.set_state(TELEGRAM_OFFSET_KEY, str(offset))
            except asyncio.CancelledError:
                raise
            except (httpx.ReadTimeout, httpx.PoolTimeout):
                continue
            except ExternalAPIError:
                logger.exception("Ошибка Telegram polling; повтор через 3 секунды")
                await asyncio.sleep(3)
            except Exception:
                logger.exception("Неожиданная ошибка Telegram polling; повтор через 3 секунды")
                await asyncio.sleep(3)

    async def max_loop(self) -> None:
        marker = _parse_cursor(await self.db.get_state(MAX_MARKER_KEY))
        while True:
            try:
                updates, next_marker = await self.max.get_updates(
                    marker, self.settings.polling_timeout_seconds
                )
                for update in updates:
                    inserted = await self.db.enqueue("max", max_event_key(update), update)
                    if inserted:
                        self.worker.wake()
                if next_marker is not None:
                    marker = next_marker
                    await self.db.set_state(MAX_MARKER_KEY, str(marker))
            except asyncio.CancelledError:
                raise
            except (httpx.ReadTimeout, httpx.PoolTimeout):
                continue
            except ExternalAPIError:
                logger.exception("Ошибка MAX polling; повтор через 3 секунды")
                await asyncio.sleep(3)
            except Exception:
                logger.exception("Неожиданная ошибка MAX polling; повтор через 3 секунды")
                await asyncio.sleep(3)


async def run_polling(settings: Settings) -> None:
    if settings.update_mode != "polling":
        raise RuntimeError("Для команды run-polling задайте UPDATE_MODE=polling")

    database = Database(settings.database_path)
    await database.initialize()
    telegram = TelegramClient(
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_api_base,
        settings.http_timeout_seconds,
    )
    max_client = MaxClient(
        settings.max_bot_token.get_secret_value(),
        settings.max_api_base,
        settings.http_timeout_seconds,
        settings.max_ca_file,
    )
    bridge = Bridge(
        database=database,
        telegram=telegram,
        max_client=max_client,
        operator_user_id=settings.max_operator_user_id,
        download_limit=settings.max_download_bytes,
        send_confirmations=settings.send_confirmations,
    )
    worker = EventWorker(database, bridge)
    runner = PollingRunner(settings, database, telegram, max_client, worker)
    worker.start()
    logger.info("TG.MaxDirectBot запущен в polling-режиме")
    try:
        await runner.run()
    finally:
        with suppress(Exception):
            await worker.stop()
        await telegram.close()
        await max_client.close()
