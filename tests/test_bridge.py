from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from tg_max_direct_bot import bridge as bridge_module
from tg_max_direct_bot.bridge import Bridge, _sender_header
from tg_max_direct_bot.clients import ExternalAPIError, _attachment_not_ready
from tg_max_direct_bot.database import Database


class FakeTelegram:
    def __init__(self) -> None:
        self.sent_text: list[dict[str, Any]] = []
        self.sent_media: list[dict[str, Any]] = []
        self.downloaded: list[str] = []

    async def get_business_connection(self, connection_id: str) -> dict[str, Any]:
        return {
            "id": connection_id,
            "user": {"id": 100},
            "is_enabled": True,
            "rights": {"can_reply": True},
        }

    async def send_text(self, **kwargs: Any) -> dict[str, Any]:
        self.sent_text.append(kwargs)
        return {"message_id": 99}

    async def send_media(self, **kwargs: Any) -> dict[str, Any]:
        self.sent_media.append(kwargs)
        return {"message_id": 100}

    async def download_file(self, file_id: str, limit: int) -> tuple[bytes, str]:
        self.downloaded.append(file_id)
        return b"image", "photos/file.jpg"


class FakeMax:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.counter = 0
        self.fail_upload = False
        self.uploaded: list[tuple[str, str]] = []

    async def send_message(
        self,
        user_id: int,
        text: Optional[str],
        attachments: Optional[list[dict[str, Any]]] = None,
        *,
        notify: bool = True,
    ) -> dict[str, Any]:
        self.counter += 1
        self.sent.append(
            {
                "user_id": user_id,
                "text": text,
                "attachments": attachments,
                "notify": notify,
            }
        )
        return {"body": {"mid": f"max-{self.counter}"}}

    async def upload(self, media_type: str, content: bytes, filename: str) -> dict[str, Any]:
        if self.fail_upload:
            raise ExternalAPIError("MAX upload вернул не JSON")
        self.uploaded.append((media_type, filename))
        return {"type": media_type, "payload": {"token": "uploaded"}}

    async def download_attachment(
        self, attachment: dict[str, Any], limit: int
    ) -> tuple[bytes, str, Optional[str]]:
        return b"reply-image", "answer.jpg", "image/jpeg"


@pytest.fixture
async def bridge(tmp_path: Path) -> tuple[Bridge, Database, FakeTelegram, FakeMax]:
    database = Database(tmp_path / "bridge.db")
    await database.initialize()
    telegram = FakeTelegram()
    max_client = FakeMax()
    service = Bridge(
        database=database,
        telegram=telegram,  # type: ignore[arg-type]
        max_client=max_client,  # type: ignore[arg-type]
        operator_user_id=777,
        download_limit=1024 * 1024,
        send_confirmations=True,
    )
    await service.handle_telegram(
        {
            "business_connection": {
                "id": "connection-1",
                "user": {"id": 100},
                "is_enabled": True,
                "rights": {"can_reply": True},
            }
        }
    )
    max_client.sent.clear()
    max_client.counter = 0
    return service, database, telegram, max_client


async def test_forwards_incoming_telegram_and_saves_reply_mapping(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, database, _, max_client = bridge
    await service.handle_telegram(
        {
            "business_message": {
                "business_connection_id": "connection-1",
                "message_id": 55,
                "from": {
                    "id": 200,
                    "first_name": "Иван",
                    "last_name": "Петров",
                    "username": "ivan",
                },
                "chat": {"id": 200, "type": "private"},
                "text": "Сколько стоит реклама?",
            }
        }
    )

    assert len(max_client.sent) == 1
    assert "Иван Петров (@ivan)" in str(max_client.sent[0]["text"])
    assert "Сколько стоит реклама?" in str(max_client.sent[0]["text"])
    target = await database.get_telegram_target("max-1")
    assert target is not None
    assert target.chat_id == 200
    assert target.message_id == 55


async def test_forwards_document_and_uses_tg_id_without_username(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, _, _, max_client = bridge
    await service.handle_telegram(
        {
            "business_message": {
                "business_connection_id": "connection-1",
                "message_id": 57,
                "from": {"id": 201, "first_name": "Илья"},
                "chat": {"id": 201, "first_name": "Илья", "type": "private"},
                "document": {
                    "file_id": "pdf-1",
                    "file_name": "bill.pdf",
                    "mime_type": "application/pdf",
                },
            }
        }
    )

    assert "Илья (TG ID: 201)" in max_client.sent[0]["text"]
    assert "Telegram ID:" not in max_client.sent[0]["text"]
    assert max_client.sent[0]["attachments"][0]["type"] == "file"


async def test_reports_failed_attachment_with_filename(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, _, _, max_client = bridge
    max_client.fail_upload = True
    await service.handle_telegram(
        {
            "business_message": {
                "business_connection_id": "connection-1",
                "message_id": 58,
                "from": {"id": 202, "first_name": "Илья", "username": "fesko_il"},
                "chat": {"id": 202, "type": "private"},
                "text": "Посмотри счёт",
                "document": {"file_id": "pdf-2", "file_name": "счёт.pdf"},
            }
        }
    )

    assert "Посмотри счёт" in max_client.sent[0]["text"]
    assert "Имя файла: счёт.pdf" in max_client.sent[0]["text"]
    assert max_client.sent[0]["attachments"] == []


async def test_downloads_and_forwards_voice_as_audio(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, telegram, max_client = bridge

    async def fake_transcode(content: bytes, filename: str) -> tuple[bytes, str]:
        return b"mp3", "voice.mp3"

    monkeypatch.setattr(bridge_module, "_transcode_ogg", fake_transcode)
    await service.handle_telegram(
        {
            "business_message": {
                "business_connection_id": "connection-1",
                "message_id": 59,
                "from": {"id": 203, "first_name": "Антон"},
                "chat": {"id": 203, "type": "private"},
                "voice": {"file_id": "voice-1", "duration": 4, "mime_type": "audio/ogg"},
            }
        }
    )

    assert telegram.downloaded == ["voice-1"]
    assert max_client.sent[0]["attachments"][0]["type"] == "audio"
    assert max_client.uploaded == [("audio", "voice.mp3")]


async def test_downloads_and_forwards_video(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, _, telegram, max_client = bridge
    await service.handle_telegram(
        {
            "business_message": {
                "business_connection_id": "connection-1",
                "message_id": 60,
                "from": {"id": 204, "first_name": "Ольга"},
                "chat": {"id": 204, "type": "private"},
                "video": {"file_id": "video-1", "file_name": "clip.mp4", "mime_type": "video/mp4"},
            }
        }
    )

    assert telegram.downloaded == ["video-1"]
    assert max_client.sent[0]["attachments"][0]["type"] == "video"


def test_sender_header_prefers_username() -> None:
    assert _sender_header({"first_name": "Илья", "username": "fesko_il"}, {"id": 201}) == (
        "📨 Илья (@fesko_il)\n\n"
    )


def test_attachment_processing_errors_are_retryable() -> None:
    assert _attachment_not_ready(
        ExternalAPIError(
            "MAX API вернул HTTP 400: Key: errors.process.attachment.video.not.processed"
        )
    )


async def test_ignores_outgoing_business_account_message(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, _, _, max_client = bridge
    await service.handle_telegram(
        {
            "business_message": {
                "business_connection_id": "connection-1",
                "message_id": 56,
                "from": {"id": 100, "first_name": "Владелец"},
                "chat": {"id": 200, "type": "private"},
                "text": "Исходящее сообщение",
            }
        }
    )
    assert max_client.sent == []


async def test_routes_operator_reply_to_original_telegram_chat(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, database, telegram, max_client = bridge
    await database.add_message_link("forwarded-mid", "connection-1", 200, 55)

    await service.handle_max(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 777},
                "recipient": {"user_id": 777, "chat_type": "dialog"},
                "link": {
                    "type": "reply",
                    "message": {"mid": "forwarded-mid", "seq": 1, "text": "card"},
                },
                "body": {"mid": "operator-mid", "seq": 2, "text": "Стоимость — 10 000 ₽"},
            },
        }
    )

    assert len(telegram.sent_text) == 1
    assert telegram.sent_text[0]["business_connection_id"] == "connection-1"
    assert telegram.sent_text[0]["chat_id"] == 200
    assert telegram.sent_text[0]["reply_to_message_id"] == 55
    assert telegram.sent_text[0]["text"] == "Стоимость — 10 000 ₽"
    assert max_client.sent[-1]["text"] == "✅ Ответ отправлен в Telegram."


async def test_requires_native_reply_in_max(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, _, telegram, max_client = bridge
    await service.handle_max(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 777},
                "body": {"mid": "operator-mid", "seq": 1, "text": "Ответ без reply"},
            },
        }
    )
    assert telegram.sent_text == []
    assert "используйте функцию «Ответить»" in str(max_client.sent[-1]["text"])


async def test_ignores_unknown_max_user(
    bridge: tuple[Bridge, Database, FakeTelegram, FakeMax],
) -> None:
    service, _, telegram, max_client = bridge
    await service.handle_max(
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 999},
                "body": {"mid": "intruder", "seq": 1, "text": "test"},
            },
        }
    )
    assert telegram.sent_text == []
    assert max_client.sent == []
