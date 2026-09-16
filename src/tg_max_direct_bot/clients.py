from __future__ import annotations

import asyncio
import json
import mimetypes
import ssl
import time
from pathlib import Path
from typing import Any, Optional

import httpx


class ExternalAPIError(RuntimeError):
    pass


def _attachment_not_ready(error: ExternalAPIError) -> bool:
    text = str(error).lower()
    return "attachment.not.ready" in text or "attachment.file.not.processed" in text


def _safe_error(service: str, response: httpx.Response) -> ExternalAPIError:
    try:
        data = response.json()
        detail = data.get("description") or data.get("message") or data.get("code")
    except (ValueError, AttributeError):
        detail = None
    suffix = f": {detail}" if detail else ""
    return ExternalAPIError(f"{service} API вернул HTTP {response.status_code}{suffix}")


class TelegramClient:
    def __init__(self, token: str, base_url: str, timeout: float) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.base_url}/bot{self.token}/{method}"
        try:
            request_timeout = timeout if timeout is not None else self.http.timeout
            response = await self.http.post(url, json=payload, timeout=request_timeout)
        except httpx.RequestError:
            raise ExternalAPIError("сетевая ошибка Telegram API") from None
        if response.is_error:
            raise _safe_error("Telegram", response)
        data = response.json()
        if not data.get("ok"):
            raise ExternalAPIError(
                f"Telegram API отклонил запрос: {data.get('description', 'неизвестная ошибка')}"
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self._request("getMe", {})

    async def get_business_connection(self, connection_id: str) -> dict[str, Any]:
        return await self._request(
            "getBusinessConnection", {"business_connection_id": connection_id}
        )

    async def set_webhook(self, url: str, secret: str) -> bool:
        return bool(
            await self._request(
                "setWebhook",
                {
                    "url": url,
                    "secret_token": secret,
                    "allowed_updates": ["business_connection", "business_message"],
                },
            )
        )

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._request("getWebhookInfo", {})

    async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        return bool(
            await self._request(
                "deleteWebhook",
                {"drop_pending_updates": drop_pending_updates},
            )
        )

    async def get_updates(
        self,
        offset: Optional[int],
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["business_connection", "business_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._request(
            "getUpdates",
            payload,
            timeout=max(float(timeout_seconds) + 15.0, 30.0),
        )
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    async def send_text(
        self,
        *,
        business_connection_id: str,
        chat_id: int,
        text: str,
        reply_to_message_id: int,
    ) -> dict[str, Any]:
        return await self._request(
            "sendMessage",
            {
                "business_connection_id": business_connection_id,
                "chat_id": chat_id,
                "text": text,
                "reply_parameters": {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                },
            },
        )

    async def send_media(
        self,
        *,
        business_connection_id: str,
        chat_id: int,
        media_type: str,
        content: bytes,
        filename: str,
        mime_type: Optional[str],
        caption: Optional[str],
        reply_to_message_id: int,
    ) -> dict[str, Any]:
        methods = {
            "image": ("sendPhoto", "photo"),
            "video": ("sendVideo", "video"),
            "audio": ("sendAudio", "audio"),
            "file": ("sendDocument", "document"),
        }
        method, field = methods.get(media_type, methods["file"])
        data: dict[str, str] = {
            "business_connection_id": business_connection_id,
            "chat_id": str(chat_id),
            "reply_parameters": json.dumps(
                {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            ),
        }
        if caption:
            data["caption"] = caption
        guessed = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {field: (filename, content, guessed)}
        url = f"{self.base_url}/bot{self.token}/{method}"
        try:
            response = await self.http.post(url, data=data, files=files)
        except httpx.RequestError:
            raise ExternalAPIError("сетевая ошибка Telegram API при отправке файла") from None
        if response.is_error:
            raise _safe_error("Telegram", response)
        payload = response.json()
        if not payload.get("ok"):
            raise ExternalAPIError(
                f"Telegram API отклонил файл: {payload.get('description', 'неизвестная ошибка')}"
            )
        return payload["result"]

    async def download_file(self, file_id: str, limit: int) -> tuple[bytes, str]:
        info = await self._request("getFile", {"file_id": file_id})
        file_path = str(info["file_path"])
        url = f"{self.base_url}/file/bot{self.token}/{file_path}"
        try:
            async with self.http.stream("GET", url) as response:
                if response.is_error:
                    raise _safe_error("Telegram", response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ExternalAPIError(
                            f"файл Telegram превышает настроенный лимит {limit} байт"
                        )
        except httpx.RequestError:
            raise ExternalAPIError("сетевая ошибка при скачивании файла Telegram") from None
        return bytes(content), file_path


class MaxClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        timeout: float,
        ca_file: Optional[Path] = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        context = ssl.create_default_context()
        if ca_file is not None:
            context.load_verify_locations(cafile=ca_file)
        self.http = httpx.AsyncClient(
            timeout=timeout,
            verify=context,
            headers={"Authorization": token},
        )
        self.media_http = httpx.AsyncClient(timeout=timeout, verify=context)
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0

    async def close(self) -> None:
        await self.http.aclose()
        await self.media_http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        try:
            request_timeout = timeout if timeout is not None else self.http.timeout
            response = await self.http.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=payload,
                timeout=request_timeout,
            )
        except httpx.RequestError:
            raise ExternalAPIError("сетевая ошибка MAX API") from None
        if response.is_error:
            raise _safe_error("MAX", response)
        data = response.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise ExternalAPIError(
                f"MAX API отклонил запрос: {data.get('message', 'неизвестная ошибка')}"
            )
        if isinstance(data, dict) and data.get("code"):
            raise ExternalAPIError(
                f"MAX API отклонил запрос: {data.get('code')}: "
                f"{data.get('message', 'неизвестная ошибка')}"
            )
        return data

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/subscriptions")
        return list(data.get("subscriptions") or [])

    async def delete_subscription(self, url: str) -> None:
        await self._request("DELETE", "/subscriptions", params={"url": url})

    async def create_subscription(self, url: str, secret: str) -> None:
        await self._request(
            "POST",
            "/subscriptions",
            payload={
                "url": url,
                "update_types": ["message_created", "bot_started"],
                "secret": secret,
            },
        )

    async def delete_all_subscriptions(self) -> int:
        subscriptions = await self.list_subscriptions()
        deleted = 0
        for subscription in subscriptions:
            url = subscription.get("url")
            if isinstance(url, str) and url:
                await self.delete_subscription(url)
                deleted += 1
        return deleted

    async def get_updates(
        self,
        marker: Optional[int],
        timeout_seconds: int,
    ) -> tuple[list[dict[str, Any]], Optional[int]]:
        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            "types": "message_created,bot_started",
        }
        if marker is not None:
            params["marker"] = marker
        data = await self._request(
            "GET",
            "/updates",
            params=params,
            timeout=max(float(timeout_seconds) + 15.0, 30.0),
        )
        updates = data.get("updates") if isinstance(data, dict) else None
        next_marker = data.get("marker") if isinstance(data, dict) else None
        clean_updates = (
            [item for item in updates if isinstance(item, dict)]
            if isinstance(updates, list)
            else []
        )
        try:
            parsed_marker = int(next_marker) if next_marker is not None else None
        except (TypeError, ValueError):
            parsed_marker = None
        return clean_updates, parsed_marker

    async def send_message(
        self,
        user_id: int,
        text: Optional[str],
        attachments: Optional[list[dict[str, Any]]] = None,
        *,
        notify: bool = True,
    ) -> dict[str, Any]:
        async with self._send_lock:
            wait = 0.52 - (time.monotonic() - self._last_send_at)
            if wait > 0:
                await asyncio.sleep(wait)
            request_payload = {
                "text": text,
                "attachments": attachments or [],
                "notify": notify,
            }
            for attempt in range(3):
                try:
                    data = await self._request(
                        "POST",
                        "/messages",
                        params={"user_id": user_id},
                        payload=request_payload,
                    )
                    break
                except ExternalAPIError as exc:
                    if not attachments or not _attachment_not_ready(exc) or attempt == 2:
                        raise
                    await asyncio.sleep(2**attempt)
            self._last_send_at = time.monotonic()
            return data.get("message", data)

    async def upload(self, media_type: str, content: bytes, filename: str) -> dict[str, Any]:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        for attempt in range(3):
            slot = await self._request("POST", "/uploads", params={"type": media_type})
            try:
                response = await self.media_http.post(
                    slot["url"],
                    files={"data": (filename, content, mime)},
                )
            except httpx.RequestError:
                raise ExternalAPIError("сетевая ошибка при загрузке файла в MAX") from None
            if response.is_error:
                raise _safe_error("MAX upload", response)
            try:
                uploaded = response.json()
            except ValueError:
                if slot.get("token") and b"<retval>1</retval>" in response.content:
                    return {"type": media_type, "payload": {"token": slot["token"]}}
                if attempt == 2:
                    raise ExternalAPIError("MAX upload вернул не JSON") from None
                await asyncio.sleep(2**attempt)
                continue
            if not isinstance(uploaded, dict):
                raise ExternalAPIError("MAX upload вернул неожиданный ответ")
            if "token" not in uploaded and "token" in slot:
                uploaded["token"] = slot["token"]
            return {"type": media_type, "payload": uploaded}
        raise ExternalAPIError("MAX upload не завершился")

    async def download_attachment(
        self, attachment: dict[str, Any], limit: int
    ) -> tuple[bytes, str, Optional[str]]:
        payload = attachment.get("payload") or {}
        url = payload.get("url")
        if not url:
            urls = attachment.get("urls") or payload.get("urls") or {}
            for key in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240"):
                if urls.get(key):
                    url = urls[key]
                    break
        if not url:
            raise ExternalAPIError("во вложении MAX нет ссылки для скачивания")
        try:
            async with self.media_http.stream("GET", url) as response:
                if response.is_error:
                    raise _safe_error("MAX media", response)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ExternalAPIError(f"файл MAX превышает настроенный лимит {limit} байт")
                content_type = response.headers.get("content-type")
        except httpx.RequestError:
            raise ExternalAPIError("сетевая ошибка при скачивании файла MAX") from None
        filename = attachment.get("filename") or payload.get("filename") or "attachment"
        return bytes(content), str(filename), content_type
