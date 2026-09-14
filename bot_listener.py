#!/usr/bin/env python3
"""
Однопрохідний слухач Telegram-команд для GitHub Actions.
Обробляє: вагу, тиск, відповіді на челендж (так/ні/стоп).
Offset зберігається в assistant_state.json (кеш actions/cache).

Команди (регістронезалежні):
  вага 92.5 / 92.5 кг        → запис ваги в Garmin
  тиск 130/80 / 130/80/72    → запис тиску в Garmin
  (130/80/72)                → запис тиску в Garmin
  так                        → прийняти запропонований челендж
  ні                         → відхилити челендж
  стоп                       → скасувати активний челендж
"""

import os
import re
import sys
import json
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import assistant_state as ast

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "bot_listener.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=3))


def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error("Telegram не налаштований")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000]}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except Exception as e:
        logger.error(f"Помилка відправки: {e}")
        return False


def get_updates(offset: int | None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if offset is not None:
        url += f"?offset={offset + 1}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Помилка getUpdates: {e}")
        return {"ok": False}


def parse_weight(text: str) -> float | None:
    t = text.lower()
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*кг", t)
    if match:
        return float(match.group(1).replace(",", "."))
    match = re.search(r"вага\s+(\d+(?:[.,]\d+)?)", t)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def parse_bp(text: str) -> tuple[int, int, int] | None:
    t = text.lower()
    match = re.search(r"\(?(\d{2,3})/(\d{2,3})(?:/(\d{2,3}))?\)?", t)
    if match and ("тиск" in t or "(" in text or "/" in t):
        systolic = int(match.group(1))
        diastolic = int(match.group(2))
        pulse = int(match.group(3)) if match.group(3) else 0
        if 60 <= systolic <= 250 and 30 <= diastolic <= 150:
            return systolic, diastolic, pulse
    return None


def log_weight_to_garmin(weight: float) -> str:
    from garminconnect import Garmin
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    client.add_weigh_in(weight=weight, unitKey="kg")
    return f"⚖️ Вага {weight} кг — записано в Garmin ✅"


def log_bp_to_garmin(systolic: int, diastolic: int, pulse: int) -> str:
    from garminconnect import Garmin
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    client.add_blood_pressure(systolic=systolic, diastolic=diastolic, pulse=pulse)
    pulse_str = f", пульс {pulse}" if pulse else ""
    return f"🩸 Тиск {systolic}/{diastolic}{pulse_str} — записано в Garmin ✅"


def handle_challenge_answer(answer: str) -> str | None:
    """Обробка так/ні/стоп. Повертає текст відповіді або None."""
    pending = ast.get_pending_challenge()
    active = ast.get_active_challenge()

    if answer == "так":
        if not pending:
            return "Немає запропонованого челенджу. Зачекай недільного звіту 🙂"
        if active:
            return f"Вже є активний челендж: {active.get('challenge', {}).get('title')}. Надішли 'стоп', щоб скасувати."
        ast.set_active_challenge({
            "challenge": pending,
            "start_date": date.today().isoformat(),
        })
        ast.set_pending_challenge(None)
        return f"🏆 Челендж прийнято: {pending.get('title')}!\nСтарт сьогодні. Прогрес показуватиму в ранковому звіті. Вперед!"

    if answer == "ні":
        if not pending:
            return None
        ast.set_pending_challenge(None)
        return "Ок, цей челендж пропускаємо. Наступного тижня запропоную інший."

    if answer == "стоп":
        if not active:
            return "Немає активного челенджу."
        title = active.get("challenge", {}).get("title", "Челендж")
        ast.set_active_challenge(None)
        return f"Челендж '{title}' скасовано. Пропозиція нового — у недільному звіті."

    return None


def process_text(text: str) -> str | None:
    """Обробка одного повідомлення. Повертає відповідь або None (ігнор)."""
    t = text.strip().lower()

    # Відповіді на челендж — мають пріоритет (короткі слова)
    if t in ("так", "ні", "стоп"):
        return handle_challenge_answer(t)

    # Вага
    weight = parse_weight(text)
    if weight is not None:
        try:
            return log_weight_to_garmin(weight)
        except Exception as e:
            logger.error(f"Помилка запису ваги: {e}")
            return f"❌ Не вдалось записати вагу в Garmin: {e}"

    # Тиск
    bp = parse_bp(text)
    if bp is not None:
        systolic, diastolic, pulse = bp
        try:
            return log_bp_to_garmin(systolic, diastolic, pulse)
        except Exception as e:
            logger.error(f"Помилка запису тиску: {e}")
            # fallback: локальний запис
            ast.add_local_bp(systolic, diastolic, pulse,
                             datetime.now(KYIV_TZ).isoformat())
            return (f"🩸 Тиск {systolic}/{diastolic} збережено локально "
                    f"(Garmin недоступний). Врахую в звітах.")

    return None


def main():
    load_env()
    allowed_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not allowed_chat:
        logger.error("Telegram не налаштований")
        sys.exit(1)

    offset = ast.get_telegram_offset()
    updates = get_updates(offset)
    if not updates.get("ok"):
        logger.warning("getUpdates повернув помилку — пропуск циклу")
        return

    processed = 0
    new_offset = offset
    for update in updates.get("result", []):
        new_offset = update["update_id"]
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")
        if not text or chat_id != str(allowed_chat):
            continue
        logger.info(f"Отримано: {text}")
        reply = process_text(text)
        if reply:
            send_telegram(reply)
            processed += 1

    if new_offset is not None:
        ast.set_telegram_offset(new_offset)

    logger.info(f"Цикл завершено. Оброблено повідомлень: {processed}")
    print(f"Оброблено: {processed}")


if __name__ == "__main__":
    main()
