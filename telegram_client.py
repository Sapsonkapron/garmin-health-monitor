import os
import urllib.parse
import urllib.request


def send_telegram(text: str, parse_mode: str | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        return True
    except Exception:
        return False
