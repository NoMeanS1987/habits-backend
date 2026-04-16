import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_AGE_SECONDS = 86400  # 24 hours


def validate_init_data(init_data: str) -> dict | None:
    """
    Validate Telegram WebApp initData using HMAC-SHA256.
    Returns parsed user dict on success, None on failure.
    Docs: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not BOT_TOKEN or not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        auth_date = int(parsed.get("auth_date", 0))
        if time.time() - auth_date > MAX_AGE_SECONDS:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()

        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None
