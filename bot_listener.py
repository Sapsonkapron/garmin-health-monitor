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
  агент                      → увімкнути/вимкнути AI-режим (Gemini з контекстом здоров'я)
"""

import os
import re
import sys
import json
import time
import argparse
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import assistant_content as ac
import assistant_state as ast
import nutrition
import storage

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


def send_telegram(text: str, reply_markup: dict | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error("Telegram не налаштований")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
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


def get_updates(offset: int | None, long_poll_sec: int = 0):
    """Отримує updates. long_poll_sec>0 — Telegram тримає з'єднання до появи update."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {}
    if offset is not None:
        params["offset"] = offset + 1
    if long_poll_sec:
        params["timeout"] = long_poll_sec
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=long_poll_sec + 30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Помилка getUpdates: {e}")
        return {"ok": False}


def parse_weight(text: str) -> float | None:
    t = text.lower().strip()
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*кг", t)
    if match:
        return float(match.group(1).replace(",", "."))
    match = re.search(r"вага\s+(\d+(?:[.,]\d+)?)", t)
    if match:
        return float(match.group(1).replace(",", "."))
    # Голе число в межах ваги тіла (30-250 кг) — теж вага
    if re.fullmatch(r"\d{2,3}(?:[.,]\d{1,2})?", t):
        w = float(t.replace(",", "."))
        if 30 <= w <= 250:
            return w
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
    storage.add_metrics(weight_kg=weight)
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
            return "Немає запропонованого челенджу. Нова пропозиція — у завтрашньому ранковому звіті 🙂"
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
            return "Ок, запропонованого челенджу поки немає. Нова пропозиція — у ранковому звіті 🙂"
        ast.set_pending_challenge(None)
        return "Ок, цей челендж пропускаємо. Завтра вранці запропоную інший."

    if answer == "стоп":
        if not active:
            return "Немає активного челенджу."
        title = active.get("challenge", {}).get("title", "Челендж")
        ast.set_active_challenge(None)
        return f"Челендж '{title}' скасовано. Нова пропозиція — у ранковому звіті."

    return None


def analyze_last_workout() -> str:
    """Rule-based аналіз останнього тренування з Garmin (еко-режим, без LLM)."""
    from garminconnect import Garmin
    try:
        client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
        client.login()
        all_activities = client.get_activities(0, 15) or []
    except Exception as e:
        logger.error(f"Garmin недоступний: {e}")
        return "❌ Не вдалось підключитись до Garmin Connect"
    # Пропускаємо нетренувальні активності (риболовля тощо)
    activities = [a for a in all_activities
                  if a.get("activityType", {}).get("typeKey", "") not in ast.EXCLUDED_ACTIVITY_TYPES]
    if not activities:
        return "На Garmin поки немає доступних записів тренувань."

    act = activities[0]
    name = act.get("activityName", "Тренування")
    type_key = act.get("activityType", {}).get("typeKey", "")
    start = act.get("startTimeLocal", "")[:16]
    duration_min = (act.get("duration") or 0) / 60
    distance_km = (act.get("distance") or 0) / 1000
    avg_hr = act.get("averageHR")
    max_hr = act.get("maxHR")
    calories = act.get("calories")

    lines = [f"🏋️ Останнє тренування: {name} ({start})"]
    parts = [f"⏱ {duration_min:.0f} хв"]
    if distance_km > 0.1:
        parts.append(f"📏 {distance_km:.1f} км")
    if avg_hr:
        parts.append(f"❤️ сер. {avg_hr:.0f} bpm")
    if max_hr:
        parts.append(f"макс {max_hr:.0f}")
    if calories:
        parts.append(f"🔥 {calories:.0f} ккал")
    lines.append(" | ".join(parts))

    if type_key in ("hiit", "crossfit", "strength_training"):
        lines.append("💪 Для силових та інтервальних занять середній пульс не визначає якість тренування. Орієнтуйся на план тренера та самопочуття.")
        lines.append("🍽 Після тренування: звичайний прийом їжі з білком, достатньо рідини та відпочинок.")
        return "\n".join(lines)

    lines.append("ℹ️ Пульс і тривалість оцінюй відповідно до мети заняття, самопочуття та плану тренера.")

    # Порівняння з попередньою активністю того ж типу
    prev_same = next((a for a in activities[1:]
                      if a.get("activityType", {}).get("typeKey", "") == type_key), None)
    if prev_same and avg_hr and prev_same.get("averageHR"):
        prev_speed = prev_same.get("averageSpeed")
        curr_speed = act.get("averageSpeed")
        hr_diff = avg_hr - prev_same["averageHR"]
        if prev_speed and curr_speed and abs(curr_speed - prev_speed) / prev_speed < 0.05:
            if hr_diff < -2:
                lines.append(f"📈 Прогрес: той самий темп при пульсі на {abs(hr_diff):.0f} bpm нижче, ніж минулого разу!")
            elif hr_diff > 3:
                lines.append(f"ℹ️ Той самий темп, але пульс на {hr_diff:.0f} bpm вище — можлива втома або спека. Слідкуй за відновленням.")

    lines.append("💤 Наступне навантаження узгоджуй із тренером і самопочуттям; за нездужання відпочинь.")

    return "\n".join(lines)


def answer_callback_query(query_id: str):
    """Знімає 'годинник' на кнопці після натискання."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    data = urllib.parse.urlencode({"callback_query_id": query_id}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        logger.warning(f"answerCallbackQuery не вдався: {e}")


def handle_callback(data: str) -> str | None:
    """Обробка натискань inline-кнопок."""
    if data == "legend":
        return ac.LEGEND_TEXT
    if data == "analyze_workout":
        return analyze_last_workout()
    if data == "menu":
        return ac.MENU_TEXT
    return None


def edit_callback_message(callback: dict):
    """Прибирає кнопки зі старого повідомлення після натискання."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    message = callback.get("message", {})
    message_id = message.get("message_id")
    chat_id = message.get("chat", {}).get("id")
    if not token or not message_id or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps({"inline_keyboard": []}),
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        logger.warning(f"Не вдалось оновити кнопки: {e}")


# ============================================================
# AI-режим: звернення до бота як до AI-агента (Gemini, безкоштовний tier)
# ============================================================
_garmin_cache = {"client": None, "ts": 0.0}
_ai_context_cache = {"text": None, "ts": 0.0}

AI_TOGGLE_WORDS = {"агент", "/агент", "ai", "/ai", "штучний інтелект"}


def _get_garmin_cached():
    """Garmin-клієнт з кешем на 1 годину (loop-процес живе 5+ годин)."""
    if _garmin_cache["client"] and time.time() - _garmin_cache["ts"] < 3600:
        return _garmin_cache["client"]
    from garminconnect import Garmin
    client = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    client.login()
    _garmin_cache.update(client=client, ts=time.time())
    return client


def _get_weight_cached() -> float | None:
    """Остання вага з Garmin (кг) або None."""
    client = _get_garmin_cached()
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=10)).isoformat()
    bc = client.get_body_composition(start, end)
    entries = bc.get("dateWeightList", []) if isinstance(bc, dict) else (bc or [])
    parsed = []
    for e in entries:
        w = e.get("weight")
        if w:
            parsed.append((str(e.get("calendarDate") or ""), w / 1000 if w > 1000 else float(w)))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    return parsed[-1][1]


def _garmin_snapshot() -> str:
    """Компактний знімок метрик за сьогодні/вчора для контексту LLM."""
    client = _get_garmin_cached()
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    lines = []
    try:
        for d in (today, yesterday):
            hr = client.get_heart_rates(d) or {}
            rhr = hr.get("restingHeartRate")
            if rhr:
                lines.append(f"RHR {d}: {rhr} bpm")
                break
    except Exception:
        pass
    try:
        for d in (today, yesterday):
            hrv = client.get_hrv_data(d) or {}
            hrv_val = hrv.get("hrvSummary", {}).get("weeklyAvg") or hrv.get("lastNightAvg")
            status = hrv.get("hrvSummary", {}).get("status")
            if hrv_val or status:
                if hrv_val:
                    lines.append(f"HRV: {hrv_val:.0f} ms" + (f" ({status})" if status else ""))
                break
    except Exception:
        pass
    try:
        bc = client.get_body_composition(yesterday, today)
        weights = bc.get("dateWeightList", []) if isinstance(bc, dict) else bc
        if weights:
            w = weights[-1].get("weight")
            if w:
                w = w / 1000 if w > 1000 else float(w)
                lines.append(f"Вага остання: {w:.1f} кг")
    except Exception:
        pass
    try:
        acts = client.get_activities(0, 3) or []
        for a in acts:
            tk = a.get("activityType", {}).get("typeKey", "")
            if tk not in ast.EXCLUDED_ACTIVITY_TYPES:
                dur = (a.get("duration") or 0) / 60
                hr = a.get("averageHR")
                lines.append(f"Останнє тренування: {a.get('activityName')} ({a.get('startTimeLocal','')[:10]}), "
                             f"{dur:.0f} хв" + (f", сер. пульс {hr}" if hr else ""))
                break
    except Exception:
        pass
    return "\n".join(lines)


def get_ai_context() -> str:
    """Контекст користувача для LLM (кеш 30 хв)."""
    if _ai_context_cache["text"] and time.time() - _ai_context_cache["ts"] < 1800:
        return _ai_context_cache["text"]
    parts = [
        f"Дата: {date.today().isoformat()}",
        f"Днів без алкоголю: {ast.streak_days()} (користувач кинув пити, підтримуй це)",
        "Основна активність — силові тренування в залі та кардіо з Garmin Connect; не змінюй програму тренера.",
        "Челенджі вимкнені: не пропонуй їх і не нагадуй про них.",
        "Мета — поступове зниження ваги; зріст, вік і цільову вагу потрібно підтвердити перед розрахунками.",
        "Не нагадуй про прийом ліків. BJJ поки не займається. Риболовля — не тренування.",
    ]
    try:
        snap = _garmin_snapshot()
        if snap:
            parts.append(snap)
    except Exception as e:
        logger.warning(f"Garmin-контекст недоступний: {e}")
    text = "\n".join(parts)
    _ai_context_cache.update(text=text, ts=time.time())
    return text


AI_SYSTEM_PROMPT = (
    "Ти — персональний асистент здоров'я українською мовою. Тон: підтримуючий, дружній, конкретний. "
    "Відповідай СТІСЛО (до 6 речень), без watermark'ів і пояснень про те, що ти ІІ. "
    "Не став медичних діагнозів і не призначай ліки — при серйозних питаннях радь лікаря. "
    "Спиратись на контекст користувача нижче."
)


def ask_gemini(text: str) -> str:
    """Запит до Gemini (безкоштовний tier). Повертає текст відповіді."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "⚠️ AI-режим тимчасово не налаштований. Не надсилай ключі у чат."
    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    payload = {
        "system_instruction": {"parts": [{"text": AI_SYSTEM_PROMPT + "\n\nКонтекст:\n" + get_ai_context()}]},
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {"maxOutputTokens": 600, "temperature": 0.7,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        reply = (result.get("candidates", [{}])[0]
                 .get("content", {}).get("parts", [{}])[0].get("text", "")).strip()
        if not reply:
            return "⚠️ Порожня відповідь від AI. Спробуй ще раз."
        return reply
    except urllib.error.HTTPError as e:
        logger.error(f"Gemini HTTP {e.code}: {e.read()[:200]}")
        if e.code == 429:
            return "⏳ Ліміт запитів AI вичерпано (оновлюється щохвилини). Спробуй за хвилину."
        if e.code == 400 and "API key" in str(e):
            return "⚠️ Ключ Gemini невірний. Перезапиши GEMINI_API_KEY."
        return f"⚠️ Помилка AI ({e.code}). Спробуй пізніше."
    except Exception as e:
        logger.error(f"Помилка Gemini: {e}")
        return "⚠️ AI тимчасово недоступний. Спробуй пізніше."


def process_food_command(text: str) -> str | None:
    command, _, description = text.strip().partition(" ")
    command = command.lower().split("@", 1)[0]
    if command not in ("/food", "/food_confirm", "/food_cancel", "/food_delete"):
        return None
    try:
        if command == "/food":
            return nutrition.prepare(description) if description.strip() else nutrition.today_log()
        if command == "/food_confirm":
            return "🍽 Страву збережено." if storage.confirm_pending_food() else "Немає страви, що очікує підтвердження."
        if command == "/food_cancel":
            if not storage.get_pending_food():
                return "Немає страви, що очікує підтвердження."
            storage.discard_pending_food()
            return "Оцінку скасовано; страву не записано."
        return "Останній запис харчування видалено." if storage.delete_last_food() else "Записів харчування немає."
    except Exception as error:
        logger.warning("Харчовий щоденник недоступний: %s", type(error).__name__)
        return "⚠️ Не вдалося отримати підтвердження від сервісу. Перевір /food перед повторною спробою."


def process_profile_command(text: str) -> str | None:
    command = text.strip().split(maxsplit=1)[0].lower().split("@", 1)[0] if text.strip() else ""
    if command != "/profile":
        return None
    parts = text.strip().split()
    try:
        if len(parts) == 1:
            profile = storage.get_profile()
            if not profile.get("confirmed_at"):
                return "Профіль ще не підтверджено. Формат: /profile ч 37 179 95 — стать (ч/ж), вік, зріст у см, цільова вага у кг. Необов’язково п’ятим числом — коефіцієнт активності 1.2–1.6."
            sex = "ч" if profile["sex"] == "male" else "ж"
            return (f"Профіль підтверджено: {sex}, {profile['age_years']} років, {profile['height_cm']} см, "
                    f"ціль {float(profile['target_weight_kg']):g} кг, активність {float(profile['activity_factor']):g}. "
                    "Для зміни надішли новий /profile з параметрами.")
        if len(parts) not in (5, 6) or parts[1].lower() not in ("ч", "ж", "male", "female"):
            return "Формат: /profile ч 37 179 95 або /profile ж 37 165 60 1.3"
        sex = "male" if parts[1].lower() in ("ч", "male") else "female"
        age = int(parts[2])
        height = int(parts[3])
        target = float(parts[4].replace(",", "."))
        activity = float(parts[5].replace(",", ".")) if len(parts) == 6 else 1.3
        if not 18 <= age <= 120 or not 100 <= height <= 250 or not 35 <= target <= 300 or not 1 <= activity <= 2.5:
            return "Перевір параметри: вік 18–120, зріст 100–250 см, вага 35–300 кг, активність 1–2.5."
        storage.update_profile(sex, age, height, target, activity)
        return "✅ Профіль підтверджено. Наступний ранковий звіт використає його для орієнтовних BMR, TDEE, калорій і БЖВ."
    except (ValueError, TypeError):
        return "Не вдалося прочитати параметри. Приклад: /profile ч 37 179 95 1.3"
    except Exception as error:
        logger.warning("Профіль недоступний: %s", type(error).__name__)
        return "⚠️ Не вдалося зберегти профіль. Спробуй ще раз."


def process_text(text: str) -> str | None:
    """Обробка одного повідомлення. Повертає відповідь або None (ігнор)."""
    profile_reply = process_profile_command(text)
    if profile_reply is not None:
        return profile_reply
    food_reply = process_food_command(text)
    if food_reply is not None:
        return food_reply
    t = text.strip().lower()

    # /-команди (нативне меню Telegram) → мапінг на текстові
    SLASH_MAP = {
        "/menu": "меню", "/help": "меню", "/start": "меню",
        "/analyze": "аналіз", "/legend": "показники",
        "/agent": "агент", "/stop": "стоп",
    }
    if t in SLASH_MAP:
        t = SLASH_MAP[t]

    # Меню
    if t == "меню":
        return ac.MENU_TEXT

    # Перемикач AI-режиму (працює завжди, в обох режимах)
    if t in AI_TOGGLE_WORDS:
        if ast.is_ai_mode():
            ast.set_ai_mode(False)
            return ("🔄 AI-режим ВИМКНЕНО.\n"
                    "Знову як до AI-агента: надішли 'агент'.")
        ast.set_ai_mode(True)
        return ("🤖 AI-режим УВІМКНЕНО!\n"
                "Тепер пиши будь-що — я відповім як AI-асистент з твоїми даними "
                "(стрік, вага, тренування, план).\n"
                "Команди (вага/тиск/аналіз) працюють як завжди.\n"
                "Вимкнути: надішли 'агент' ще раз.")

    if t == "стоп" or re.fullmatch(r"/done(?:\s+\d{1,4})?", t):
        return "Челенджі вимкнені. Відстежую силові та кардіо з Garmin Connect."

    # Текстові дублі кнопок
    if t in ("показники", "легенда"):
        return ac.LEGEND_TEXT
    if t in ("аналіз", "аналіз тренування", "останнє тренування"):
        return analyze_last_workout()

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

    # AI-режим: все невідоме йде до Gemini з контекстом здоров'я
    if ast.is_ai_mode():
        return ask_gemini(text)

    # Невідоме повідомлення — підказка, щоб бот ніколи не мовчав
    return ("🤔 Не зрозумів. Ось що я вмію:\n"
            "• вага: 106,7 або 106,7 кг\n"
            "• тиск: 130/80 або 130/80/72\n"
            "• аналіз — розбір останнього тренування\n"
            "• показники — легенда метрик\n"
            "• агент — AI-режим (звертайся як до AI-асистента)")


def handle_update(update: dict, allowed_chat: str) -> bool:
    """Обробляє один update (callback або повідомлення). True якщо була відповідь."""
    # Натискання inline-кнопки
    callback = update.get("callback_query")
    if callback:
        chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        data = callback.get("data", "")
        answer_callback_query(callback.get("id", ""))
        if chat_id == allowed_chat and data:
            edit_callback_message(callback)
            logger.info(f"Callback: {data}")
            reply = handle_callback(data)
            if reply:
                send_telegram(reply)
                return True
        return False

    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "")
    if chat_id != allowed_chat:
        return False
    photos = message.get("photo") or []
    if photos:
        try:
            if storage.get_pending_food():
                reply = "Є непідтверджена страва. Надішли /food_confirm або /food_cancel."
            else:
                photo = nutrition.download_photo(photos[-1]["file_id"])
                reply = nutrition.prepare(message.get("caption", ""), photo)
        except Exception as error:
            logger.warning("Не вдалося обробити фото їжі: %s", type(error).__name__)
            reply = "⚠️ Не вдалося оцінити фото. Перевір /food перед повторною спробою."
        send_telegram(reply)
        return True
    if not text:
        if message:
            send_telegram("🤔 Надішли текст або фото страви з описом.")
            return True
        return False
    logger.info("Отримано текстове повідомлення")
    reply = process_text(text)
    if reply:
        send_telegram(reply)
        return True
    return False


def run_once():
    """Одноразовий прохід getUpdates (поточний режим bot_poll)."""
    offset = ast.get_telegram_offset()
    updates = get_updates(offset)
    if not updates.get("ok"):
        logger.warning("getUpdates повернув помилку — пропуск циклу")
        return

    processed = 0
    new_offset = offset
    for update in updates.get("result", []):
        new_offset = update["update_id"]
        if handle_update(update, str(os.environ["TELEGRAM_CHAT_ID"])):
            processed += 1

    if new_offset is not None:
        ast.set_telegram_offset(new_offset)

    logger.info(f"Цикл завершено. Оброблено повідомлень: {processed}")
    print(f"Оброблено: {processed}")


def run_loop(max_seconds: int):
    """Безперервний long-poll режим: кнопки відповідають за секунди.
    Використовується лише коли репо публічне (Actions хвилини безлімітні)."""
    offset = ast.get_telegram_offset()
    deadline = time.monotonic() + max_seconds
    processed = 0
    last_beat = time.monotonic()
    logger.info(f"Loop-режим: {max_seconds} c, offset={offset}")

    while time.monotonic() < deadline:
        updates = get_updates(offset, long_poll_sec=50)
        if not updates.get("ok"):
            time.sleep(30)  # назад після помилки мережі/API
            continue
        for update in updates.get("result", []):
            offset = update["update_id"]
            ast.set_telegram_offset(offset)
            if handle_update(update, str(os.environ["TELEGRAM_CHAT_ID"])):
                processed += 1
        if time.monotonic() - last_beat > 600:
            last_beat = time.monotonic()
            logger.info(f"Пульс: оброблено {processed}")
            print(f"Пульс: оброблено {processed}", flush=True)

    logger.info(f"Loop завершено. Оброблено: {processed}")
    print(f"Loop завершено. Оброблено: {processed}")


def main():
    load_env()
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        logger.error("Telegram не налаштований")
        sys.exit(1)
    run_once()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true",
                        help="Безперервний long-poll режим (24/7 bot)")
    parser.add_argument("--max-seconds", type=int, default=18900,
                        help="Максимальна тривалість loop-режиму (сек)")
    args = parser.parse_args()
    if args.loop:
        load_env()
        run_loop(args.max_seconds)
    else:
        main()
