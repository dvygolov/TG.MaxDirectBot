from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from .clients import ExternalAPIError, MaxClient, TelegramClient
from .database import Database

logger = logging.getLogger(__name__)


class PermanentEventError(RuntimeError):
    """Ошибка формата события, повтор которой ничего не исправит."""


@dataclass
class TelegramMedia:
    media_type: str
    file_id: str
    filename: str


def _full_name(user: dict[str, Any]) -> str:
    parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
    return " ".join(part for part in parts if part) or "Пользователь Telegram"


def _sender_header(sender: dict[str, Any], chat: dict[str, Any]) -> str:
    username = sender.get("username") or chat.get("username")
    identity = f"@{username}" if username else f"TG ID: {chat['id']}"
    return f"📨 {_full_name(sender)} ({identity})\n\n"


def _telegram_media(message: dict[str, Any]) -> list[TelegramMedia]:
    if message.get("photo"):
        photo = message["photo"][-1]
        return [TelegramMedia("image", photo["file_id"], "photo.jpg")]
    candidates = (
        ("document", "file", "document.bin"),
        ("video", "video", "video.mp4"),
        ("animation", "video", "animation.mp4"),
        ("audio", "audio", "audio.mp3"),
        ("voice", "audio", "voice.ogg"),
        ("video_note", "video", "video-note.mp4"),
    )
    for field, media_type, fallback in candidates:
        item = message.get(field)
        if item:
            return [
                TelegramMedia(
                    media_type,
                    item["file_id"],
                    str(item.get("file_name") or fallback),
                )
            ]
    return []


def _attachment_labels(message: dict[str, Any]) -> list[str]:
    labels = {
        "photo": "фото",
        "document": "документ",
        "video": "видео",
        "animation": "анимация",
        "audio": "аудио",
        "voice": "голосовое сообщение",
        "video_note": "видеосообщение",
        "sticker": "стикер",
        "location": "геопозиция",
        "contact": "контакт",
        "poll": "опрос",
    }
    return [label for field, label in labels.items() if message.get(field)]


def _split_for_max(header: str, body: str, limit: int = 4000) -> list[str]:
    body = body or "[сообщение без текста]"
    first_room = max(1, limit - len(header))
    chunks = [header + body[:first_room]]
    remainder = body[first_room:]
    continuation = "↳ Продолжение сообщения\n\n"
    room = limit - len(continuation)
    while remainder:
        chunks.append(continuation + remainder[:room])
        remainder = remainder[room:]
    return chunks


def _max_reply_mid(message: dict[str, Any]) -> Optional[str]:
    link = message.get("link") or {}
    if link.get("type") != "reply":
        return None
    linked_body = link.get("message") or {}
    return linked_body.get("mid")


def _attachment_failure_note(names: list[str]) -> str:
    return "\n\n⚠️ Не удалось загрузить вложение в MAX.\nИмя файла: " + ", ".join(names)


class Bridge:
    def __init__(
        self,
        *,
        database: Database,
        telegram: TelegramClient,
        max_client: MaxClient,
        operator_user_id: int,
        download_limit: int,
        send_confirmations: bool,
    ) -> None:
        self.db = database
        self.telegram = telegram
        self.max = max_client
        self.operator_user_id = operator_user_id
        self.download_limit = download_limit
        self.send_confirmations = send_confirmations

    async def handle_telegram(self, update: dict[str, Any]) -> None:
        if connection := update.get("business_connection"):
            await self._handle_connection(connection)
            return
        if message := update.get("business_message"):
            await self._handle_business_message(message)

    async def _handle_connection(self, connection: dict[str, Any]) -> None:
        if not connection.get("id") or not (connection.get("user") or {}).get("id"):
            raise PermanentEventError("неполное событие business_connection")
        await self.db.save_business_connection(connection)
        enabled = bool(connection.get("is_enabled", True))
        status = "подключён" if enabled else "отключён"
        try:
            await self.max.send_message(
                self.operator_user_id,
                f"ℹ️ Telegram Business-бот {status}.",
            )
        except ExternalAPIError as exc:
            logger.warning("Не удалось отправить оператору статус подключения: %s", exc)

    async def _handle_business_message(self, message: dict[str, Any]) -> None:
        connection_id = message.get("business_connection_id")
        if not connection_id:
            raise PermanentEventError("у business_message нет business_connection_id")
        if message.get("sender_business_bot"):
            return

        business_user_id = await self.db.get_business_user_id(connection_id)
        if business_user_id is None:
            connection = await self.telegram.get_business_connection(connection_id)
            await self.db.save_business_connection(connection)
            business_user_id = int(connection["user"]["id"])

        sender = message.get("from") or {}
        if int(sender.get("id") or 0) == business_user_id:
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        if not message.get("message_id") or not chat.get("id"):
            raise PermanentEventError("у business_message нет идентификаторов")

        header = _sender_header(sender, chat)
        text = str(message.get("text") or message.get("caption") or "")
        labels = _attachment_labels(message)
        if labels:
            marker = "Вложение: " + ", ".join(labels)
            text = f"{text}\n\n[{marker}]" if text else f"[{marker}]"

        attachments: list[dict[str, Any]] = []
        attachment_names = [media.filename for media in _telegram_media(message)]
        failed_attachment_names: list[str] = []
        for media in _telegram_media(message):
            try:
                content, actual_path = await self.telegram.download_file(
                    media.file_id, self.download_limit
                )
                filename = media.filename
                if filename.endswith(".bin") and "." in actual_path:
                    filename = actual_path.rsplit("/", 1)[-1]
                attachments.append(await self.max.upload(media.media_type, content, filename))
            except Exception as exc:
                logger.warning("Не удалось перенести вложение Telegram в MAX: %s", exc)
                failed_attachment_names.append(media.filename)

        if failed_attachment_names:
            text += _attachment_failure_note(failed_attachment_names)

        chunks = _split_for_max(header, text)
        for index, chunk in enumerate(chunks):
            chunk_attachments = attachments if index == 0 else None
            try:
                sent = await self.max.send_message(
                    self.operator_user_id,
                    chunk,
                    chunk_attachments,
                )
            except ExternalAPIError:
                if not chunk_attachments:
                    raise
                fallback = chunk + _attachment_failure_note(attachment_names)
                sent = await self.max.send_message(self.operator_user_id, fallback)
            mid = ((sent.get("body") or {}).get("mid")) if isinstance(sent, dict) else None
            if not mid:
                raise ExternalAPIError("MAX API не вернул ID отправленного сообщения")
            await self.db.add_message_link(
                str(mid),
                str(connection_id),
                int(chat["id"]),
                int(message["message_id"]),
            )
    async def handle_max(self, update: dict[str, Any]) -> None:
        update_type = update.get("update_type")
        if update_type == "bot_started":
            user_id = int((update.get("user") or {}).get("user_id") or 0)
            if user_id == self.operator_user_id:
                await self.max.send_message(
                    self.operator_user_id,
                    "Бот готов. Ответьте в MAX на пересланное сообщение — ответ уйдёт "
                    "в соответствующий личный чат Telegram.",
                )
            else:
                logger.warning(
                    "MAX-бот запущен пользователем user_id=%s, но разрешён user_id=%s",
                    user_id,
                    self.operator_user_id,
                )
            return
        if update_type != "message_created":
            return

        message = update.get("message") or {}
        sender_id = int((message.get("sender") or {}).get("user_id") or 0)
        if sender_id != self.operator_user_id:
            logger.warning("Проигнорировано сообщение MAX от постороннего user_id=%s", sender_id)
            return

        linked_mid = _max_reply_mid(message)
        if not linked_mid:
            await self.max.send_message(
                self.operator_user_id,
                "Чтобы ответить клиенту, используйте функцию «Ответить» на его карточке.",
            )
            return
        target = await self.db.get_telegram_target(linked_mid)
        if target is None:
            await self.max.send_message(
                self.operator_user_id,
                "Не нашёл связь с Telegram. Возможно, это старое или не пересланное "
                "ботом сообщение.",
            )
            return

        body = message.get("body") or {}
        text = str(body.get("text") or "").strip()
        max_attachments = list(body.get("attachments") or [])
        supported = [
            item
            for item in max_attachments
            if item.get("type") in {"image", "video", "audio", "file"}
        ]
        if not text and not supported:
            await self.max.send_message(
                self.operator_user_id,
                "В ответе нет текста или поддерживаемого вложения.",
            )
            return

        results: list[dict[str, Any]] = []
        if supported:
            caption = text if text and len(text) <= 1024 else None
            if text and caption is None:
                results.append(
                    await self.telegram.send_text(
                        business_connection_id=target.business_connection_id,
                        chat_id=target.chat_id,
                        text=text,
                        reply_to_message_id=target.message_id,
                    )
                )
            for index, attachment in enumerate(supported):
                media_type = str(attachment.get("type"))
                content, filename, mime = await self.max.download_attachment(
                    attachment, self.download_limit
                )
                results.append(
                    await self.telegram.send_media(
                        business_connection_id=target.business_connection_id,
                        chat_id=target.chat_id,
                        media_type=media_type,
                        content=content,
                        filename=filename,
                        mime_type=mime,
                        caption=caption if index == 0 else None,
                        reply_to_message_id=target.message_id,
                    )
                )
        else:
            results.append(
                await self.telegram.send_text(
                    business_connection_id=target.business_connection_id,
                    chat_id=target.chat_id,
                    text=text,
                    reply_to_message_id=target.message_id,
                )
            )

        if self.send_confirmations and results:
            await self.max.send_message(
                self.operator_user_id,
                "✅ Ответ отправлен в Telegram.",
                notify=False,
            )
