from pathlib import Path

from tg_max_direct_bot.database import Database


async def test_event_queue_deduplicates_source_key(tmp_path: Path) -> None:
    database = Database(tmp_path / "queue.db")
    await database.initialize()

    assert await database.enqueue("telegram", "123", {"update_id": 123}) is True
    assert await database.enqueue("telegram", "123", {"update_id": 123}) is False

    event = await database.claim_next()
    assert event is not None
    assert event.source == "telegram"
    assert event.payload == {"update_id": 123}
    await database.complete(event.id)
    assert await database.claim_next() is None
