from tg_max_direct_bot.app import _max_event_key
from tg_max_direct_bot.bridge import _max_reply_mid, _split_for_max


def test_max_event_key_uses_message_id() -> None:
    update = {
        "update_type": "message_created",
        "message": {"body": {"mid": "abc-123"}},
    }
    assert _max_event_key(update) == "message_created:abc-123"


def test_reply_mid_comes_from_linked_message() -> None:
    message = {"link": {"type": "reply", "message": {"mid": "forwarded"}}}
    assert _max_reply_mid(message) == "forwarded"


def test_long_telegram_message_is_not_lost() -> None:
    body = "я" * 5000
    parts = _split_for_max("Заголовок\n\n", body)
    assert all(len(part) <= 4000 for part in parts)
    restored = parts[0].removeprefix("Заголовок\n\n") + "".join(
        part.removeprefix("↳ Продолжение сообщения\n\n") for part in parts[1:]
    )
    assert restored == body
