from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request

from .bridge import Bridge
from .clients import MaxClient, TelegramClient
from .config import Settings, get_settings
from .database import Database
from .events import max_event_key
from .worker import EventWorker

logger = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    database = Database(settings.database_path)
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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await database.initialize()
        worker.start()
        logger.info("TG.MaxDirectBot запущен")
        try:
            yield
        finally:
            await worker.stop()
            await telegram.close()
            await max_client.close()

    app = FastAPI(
        title="TG.MaxDirectBot",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.worker = worker

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        if not await database.healthcheck():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {"status": "ok"}

    @app.post("/webhooks/telegram")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> dict[str, bool]:
        expected = settings.telegram_webhook_secret_value
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token, expected
        ):
            raise HTTPException(status_code=403, detail="invalid webhook secret")
        update = await request.json()
        update_id = update.get("update_id")
        if update_id is None:
            raise HTTPException(status_code=400, detail="missing update_id")
        inserted = await database.enqueue("telegram", str(update_id), update)
        if inserted:
            worker.wake()
        return {"ok": True}

    @app.post("/webhooks/max")
    async def max_webhook(
        request: Request,
        x_max_bot_api_secret: Optional[str] = Header(default=None),
    ) -> dict[str, bool]:
        expected = settings.max_webhook_secret_value
        if not x_max_bot_api_secret or not hmac.compare_digest(x_max_bot_api_secret, expected):
            raise HTTPException(status_code=403, detail="invalid webhook secret")
        update = await request.json()
        inserted = await database.enqueue("max", max_event_key(update), update)
        if inserted:
            worker.wake()
        return {"ok": True}

    return app
