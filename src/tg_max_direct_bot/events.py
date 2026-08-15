from __future__ import annotations

import hashlib
import json
from typing import Any


def max_event_key(update: dict[str, Any]) -> str:
    update_type = str(update.get("update_type") or "unknown")
    message = update.get("message") or {}
    mid = (message.get("body") or {}).get("mid")
    if mid:
        return f"{update_type}:{mid}"
    canonical = json.dumps(update, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{update_type}:{hashlib.sha256(canonical.encode()).hexdigest()}"
