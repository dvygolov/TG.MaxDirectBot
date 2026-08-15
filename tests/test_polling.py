from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import pytest

from tg_max_direct_bot.config import Settings
from tg_max_direct_bot.database import Database
from tg_max_direct_bot.polling import (
    MAX_MARKER_KEY,
    TELEGRAM_OFFSET_KEY,
    PollingRunner,
    _parse_cursor,
)


class WakeRecorder:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


class TelegramPollFake:
    def __init__(self) -> None:
        self.calls = 0

    async def get_updates(
        self, offset: Optional[int], timeout_seconds: int
    ) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            assert offset is None
            return [{"update_id": 10, "business_message": {"message_id": 1}}]
        raise asyncio.CancelledError


class MaxPollFake:
    def __init__(self) -> None:
        self.calls = 0

    async def get_updates(
        self, marker: Optional[int], timeout_seconds: int
    ) -> tuple[list[dict[str, Any]], Optional[int]]:
        self.calls += 1
        if self.calls == 1:
            assert marker is None
            return (
                [
                    {
                        "update_type": "message_created",
                        "message": {"body": {"mid": "max-1"}},
                    }
                ],
                99,
            )
        raise asyncio.CancelledError


def polling_settings(tmp_path: Path) -> Settings:
    return Settings(
        update_mode="polling",
        telegram_bot_token="telegram-test",
        max_bot_token="max-test",
        max_operator_user_id=777,
        database_path=tmp_path / "polling.db",
    )


def test_parse_cursor() -> None:
    assert _parse_cursor(None) is None
    assert _parse_cursor("123") == 123
    assert _parse_cursor("broken") is None


async def test_telegram_polling_enqueues_before_saving_offset(tmp_path: Path) -> None:
    settings = polling_settings(tmp_path)
    database = Database(settings.database_path)
    await database.initialize()
    worker = WakeRecorder()
    runner = PollingRunner(
        settings,
        database,
        TelegramPollFake(),  # type: ignore[arg-type]
        MaxPollFake(),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.telegram_loop()

    assert await database.get_state(TELEGRAM_OFFSET_KEY) == "11"
    event = await database.claim_next()
    assert event is not None
    assert event.source == "telegram"
    assert worker.wakes == 1


async def test_max_polling_enqueues_and_saves_marker(tmp_path: Path) -> None:
    settings = polling_settings(tmp_path)
    database = Database(settings.database_path)
    await database.initialize()
    worker = WakeRecorder()
    runner = PollingRunner(
        settings,
        database,
        TelegramPollFake(),  # type: ignore[arg-type]
        MaxPollFake(),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.max_loop()

    assert await database.get_state(MAX_MARKER_KEY) == "99"
    event = await database.claim_next()
    assert event is not None
    assert event.source == "max"
    assert worker.wakes == 1
