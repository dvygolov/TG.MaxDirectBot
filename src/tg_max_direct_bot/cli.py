from __future__ import annotations

import argparse
import asyncio
import logging

from .clients import MaxClient, TelegramClient
from .config import Settings
from .polling import run_polling


async def _clients(settings: Settings) -> tuple[TelegramClient, MaxClient]:
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
    return telegram, max_client


async def setup_webhooks(settings: Settings) -> None:
    telegram, max_client = await _clients(settings)
    try:
        tg_me = await telegram.get_me()
        if not tg_me.get("can_connect_to_business"):
            raise RuntimeError(
                "Telegram-бот не поддерживает Business connections. "
                "Включите Business Mode в @BotFather."
            )
        await telegram.set_webhook(
            settings.telegram_webhook_url,
            settings.telegram_webhook_secret_value,
        )

        for subscription in await max_client.list_subscriptions():
            if subscription.get("url") == settings.max_webhook_url:
                await max_client.delete_subscription(settings.max_webhook_url)
        await max_client.create_subscription(
            settings.max_webhook_url,
            settings.max_webhook_secret_value,
        )
        print(f"Telegram webhook: {settings.telegram_webhook_url}")
        print(f"MAX webhook:      {settings.max_webhook_url}")
        print("Webhook успешно настроены.")
    finally:
        await telegram.close()
        await max_client.close()


async def check(settings: Settings) -> None:
    telegram, max_client = await _clients(settings)
    try:
        tg_me = await telegram.get_me()
        max_me = await max_client.get_me()
        webhook = await telegram.get_webhook_info()
        subscriptions = await max_client.list_subscriptions()
        print(
            "Telegram:",
            f"@{tg_me.get('username', '?')}",
            f"business={bool(tg_me.get('can_connect_to_business'))}",
        )
        print("Telegram webhook:", webhook.get("url") or "не установлен")
        print("MAX:", f"@{max_me.get('username', '?')}", f"id={max_me.get('user_id', '?')}")
        print("MAX subscriptions:", len(subscriptions))
    finally:
        await telegram.close()
        await max_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление TG.MaxDirectBot")
    parser.add_argument(
        "command",
        choices=("setup-webhooks", "run-polling", "check"),
        help="настроить webhook, запустить polling или проверить подключения",
    )
    args = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "setup-webhooks":
        asyncio.run(setup_webhooks(settings))
    elif args.command == "run-polling":
        asyncio.run(run_polling(settings))
    else:
        asyncio.run(check(settings))


if __name__ == "__main__":
    main()
