from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass(slots=True)
class QueuedEvent:
    id: int
    source: str
    event_key: str
    payload: dict[str, Any]
    attempts: int


@dataclass(slots=True)
class TelegramTarget:
    business_connection_id: str
    chat_id: int
    message_id: int


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 5000")
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(source, event_key)
                );

                CREATE INDEX IF NOT EXISTS idx_events_pending
                    ON events(status, next_attempt_at, id);

                CREATE TABLE IF NOT EXISTS business_connections (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    is_enabled INTEGER NOT NULL,
                    can_reply INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_links (
                    max_message_id TEXT PRIMARY KEY,
                    business_connection_id TEXT NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_message_links_telegram
                    ON message_links(telegram_chat_id, telegram_message_id);
                """
            )
            await db.execute(
                "UPDATE events SET status = 'pending', next_attempt_at = 0 "
                "WHERE status = 'processing'"
            )
            await db.execute(
                "DELETE FROM events WHERE status = 'done' AND updated_at < ?",
                (time.time() - 7 * 86400,),
            )
            await db.commit()

    async def healthcheck(self) -> bool:
        async with self.connect() as db:
            row = await (await db.execute("SELECT 1 AS ok")).fetchone()
            return bool(row and row["ok"] == 1)

    async def enqueue(self, source: str, event_key: str, payload: dict[str, Any]) -> bool:
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO events
                    (source, event_key, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source, event_key, encoded, now, now),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def claim_next(self) -> QueuedEvent | None:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT id, source, event_key, payload, attempts
                    FROM events
                    WHERE status = 'pending' AND next_attempt_at <= ?
                    ORDER BY id
                    LIMIT 1
                    """,
                    (time.time(),),
                )
            ).fetchone()
            if row is None:
                await db.commit()
                return None
            attempts = int(row["attempts"]) + 1
            await db.execute(
                "UPDATE events SET status = 'processing', attempts = ?, "
                "updated_at = ? WHERE id = ?",
                (attempts, time.time(), row["id"]),
            )
            await db.commit()
            return QueuedEvent(
                id=int(row["id"]),
                source=str(row["source"]),
                event_key=str(row["event_key"]),
                payload=json.loads(row["payload"]),
                attempts=attempts,
            )

    async def complete(self, event_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE events SET status = 'done', last_error = NULL, updated_at = ? WHERE id = ?",
                (time.time(), event_id),
            )
            await db.commit()

    async def retry(self, event: QueuedEvent, error: str) -> None:
        delay = min(300.0, 2.0 ** min(event.attempts, 8))
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE events
                SET status = 'pending', next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (time.time() + delay, error[:1000], time.time(), event.id),
            )
            await db.commit()

    async def save_business_connection(self, connection: dict[str, Any]) -> None:
        rights = connection.get("rights") or {}
        can_reply = bool(rights.get("can_reply", connection.get("can_reply", False)))
        user = connection.get("user") or {}
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO business_connections
                    (id, telegram_user_id, is_enabled, can_reply, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    is_enabled = excluded.is_enabled,
                    can_reply = excluded.can_reply,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    connection["id"],
                    int(user["id"]),
                    int(bool(connection.get("is_enabled", True))),
                    int(can_reply),
                    json.dumps(connection, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                ),
            )
            await db.commit()

    async def get_business_user_id(self, connection_id: str) -> int | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT telegram_user_id FROM business_connections WHERE id = ?",
                    (connection_id,),
                )
            ).fetchone()
            return int(row["telegram_user_id"]) if row else None

    async def add_message_link(
        self,
        max_message_id: str,
        business_connection_id: str,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO message_links
                    (max_message_id, business_connection_id, telegram_chat_id,
                     telegram_message_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    max_message_id,
                    business_connection_id,
                    telegram_chat_id,
                    telegram_message_id,
                    time.time(),
                ),
            )
            await db.commit()

    async def get_telegram_target(self, max_message_id: str) -> TelegramTarget | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    """
                    SELECT business_connection_id, telegram_chat_id, telegram_message_id
                    FROM message_links
                    WHERE max_message_id = ?
                    """,
                    (max_message_id,),
                )
            ).fetchone()
            if row is None:
                return None
            return TelegramTarget(
                business_connection_id=str(row["business_connection_id"]),
                chat_id=int(row["telegram_chat_id"]),
                message_id=int(row["telegram_message_id"]),
            )
