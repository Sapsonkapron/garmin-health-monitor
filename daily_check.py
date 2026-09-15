#!/usr/bin/env python3
"""
Щоденний моніторинг здоров'я через Garmin Connect.
Запуск о 9:00 через cron. Виводить алерти або підтвердження норми в stdout.
"""

import os
import sys
import json
import logging
import argparse
import urllib.request
import urllib.parse
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from garminconnect import Garmin

import assistant_content as ac
import assistant_state as ast

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=3))  # fallback: фіксований EEST

# --- Конфігурація ---
DATA_DIR = Path("/tmp/garmin_data")
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ALERT_STATE_FILE = DATA_DIR / "alert_state.json"
REALERT_DAYS = 3  # через скільки днів повторно надіслати повний алерт, якщо відхилення не зникло


def _read_state() -> dict:
    """Читає весь стан алертів."""
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        with open(ALERT_STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Не вдалось прочитати alert_state.json: {e}")
        return {}


def _write_state(state: dict):
    """Зберігає весь стан алертів."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(ALERT_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Не вдалось зберегти alert_state.json: {e}")


def already_sent_today() -> bool:
    """Чи вже відправляли щоденний звіт сьогодні?"""
    state = _read_state()
    last_sent = state.get("_last_sent_date")
    return last_sent == date.today().isoformat()


def mark_sent_today():
    """Позначає, що щоденний звіт вже відправлено сьогодні."""
    state = _read_state()
    state["_last_sent_date"] = date.today().isoformat()
    _write_state(state)


def get_garmin_credentials():
    """Отримує логін/пароль Garmin з env."""
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        logger.error("GARMIN_EMAIL або GARMIN_PASSWORD не налаштовані")
        print("Не вдалось підключитись до Garmin Connect")
        sys.exit(1)
    return email, password

# Пороги алертів
THRESHOLDS = {
    "rhr_max": 70,           # bpm
    "rhr_spike": 10,         # bpm від середнього
    "hrv_drop_pct": 30,      # % падіння від тижневого середнього
    "hrv_absolute_low": 32,  # абсолютний мінімум — нижче = алерт (baseline 41.6)
    "stress_avg_max": 60,    # середній стрес за добу
    "body_battery_min": 40,  # мінімальний максимум BB за добу
    "body_battery_low": 55,  # попередження якщо BB не заряджається вище цього
    "sleep_min_hours": 6,    # мінімум сну в годинах
    "sleep_warning_hours": 7,  # попередження (рекомендований мінімум)
    "bp_systolic_high": 140, # верхній тиск — високий
    "bp_systolic_low": 90,   # верхній тиск — низький
    "bp_diastolic_high": 90, # нижній тиск — високий
    "bp_diastolic_low": 60,  # нижній тиск — низький
}

# --- Логування ---
logging.basicConfig(
    filename=LOG_DIR / "daily_check.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_env():
    """Завантажує TELEGRAM_ змінні з .env файлу."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


ASSISTANT_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📖 Що означають показники", "callback_data": "legend"},
            {"text": "🏃 Аналіз останнього тренування", "callback_data": "analyze_workout"},
        ],
        [
            {"text": "📋 Меню", "callback_data": "menu"},
        ],
    ]
}


def send_telegram(text: str) -> bool:
    """Відправляє повідомлення в Telegram з кнопками асистента."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram token або chat_id не налаштовані")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "reply_markup": json.dumps(ASSISTANT_KEYBOARD),
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        logger.info("Повідомлення відправлено в Telegram")
        return True
    except Exception as e:
        logger.error(f"Помилка відправки в Telegram: {e}")
        return False


def is_send_time(hour: int = 9, tolerance_min: int = 5) -> bool:
    """Перевіряє, чи зараз час відправки за Києвом (за замовчуванням 9:00 ± 5 хв)."""
    now = datetime.now(KYIV_TZ)
    scheduled = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    diff = abs((now - scheduled).total_seconds())
    return diff <= tolerance_min * 60


def load_alert_state() -> dict:
    """Завантажує стан попередніх алертів {метрика: дата останнього повного сповіщення}."""
    state = _read_state()
    # Видаляємо службові ключі
    return {k: v for k, v in state.items() if not k.startswith("_")}


def save_alert_state(state: dict):
    """Зберігає стан алертів на диск, зберігаючи службові ключі."""
    full_state = _read_state()
    # Оновлюємо тільки метрики, службові ключі залишаємо
    for key in list(full_state.keys()):
        if not key.startswith("_") and key not in state:
            del full_state[key]
    full_state.update(state)
    _write_state(full_state)


def dedupe_alerts(alerts: list) -> tuple:
    """
    Розділяє алерти на нові/повторні через REALERT_DAYS та прибирає з стану вирішені.
    Повертає (new_alerts, suppressed_alerts).
    """
    state = load_alert_state()
    today_date = date.today()
    current_keys = {a["metric"] for a in alerts}

    new_alerts = []
    suppressed_alerts = []

    for alert in alerts:
        key = alert["metric"]
        last_date_str = state.get(key)
        should_notify = True
        if last_date_str:
            try:
                last_date = date.fromisoformat(last_date_str)
                if (today_date - last_date).days < REALERT_DAYS:
                    should_notify = False
            except ValueError:
                pass

        if should_notify:
            new_alerts.append(alert)
            state[key] = today_date.isoformat()
        else:
            suppressed_alerts.append(alert)

    # Прибираємо з стану метрики, які більше не активні (відхилення зникло)
    for key in list(state.keys()):
        if key not in current_keys:
            del state[key]

    save_alert_state(state)
    return new_alerts, suppressed_alerts


def connect_garmin() -> Garmin:
    """Підключення до Garmin Connect."""
    try:
        email, password = get_garmin_credentials()
        client = Garmin(email, password)
        client.login()
        return client
    except Exception as e:
        logger.error(f"Помилка підключення: {e}")
        print("Не вдалось підключитись до Garmin Connect")
        sys.exit(1)


def get_rhr_history(client: Garmin, days: int = 7) -> list[int]:
    """Отримати історію RHR за N днів для розрахунку середнього."""
    values = []
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        try:
            hr_data = client.get_heart_rates(d.isoformat())
            rhr = hr_data.get("restingHeartRate")
            if rhr:
                values.append(rhr)
        except Exception:
            continue
    return values


def get_hrv_weekly(client: Garmin) -> list[int]:
    """Отримати HRV за останній тиждень."""
    values = []
    for i in range(1, 8):
        d = date.today() - timedelta(days=i)
        try:
            hrv_data = client.get_hrv_data(d.isoformat())
            if hrv_data and "hrvSummary" in hrv_data:
                weekly_avg = hrv_data["hrvSummary"].get("weeklyAvg")
                if weekly_avg:
                    values.append(weekly_avg)
                    continue
                last_night = hrv_data["hrvSummary"].get("lastNightAvg")
                if last_night:
                    values.append(last_night)
        except Exception:
            continue
    return values


def get_weight_data(client: Garmin) -> dict | None:
    """Остання вага з Garmin + вага тиждень тому. Повертає dict або None."""
    try:
        end = date.today()
        start = end - timedelta(days=10)
        bc = client.get_body_composition(start.isoformat(), end.isoformat())
        entries = []
        if isinstance(bc, dict):
            entries = bc.get("dateWeightList", [])
        elif isinstance(bc, list):
            entries = bc
        parsed = []
        for e in entries:
            w = e.get("weight")
            d = e.get("calendarDate") or e.get("date") or e.get("timestampGMT")
            if w and d:
                w_kg = w / 1000 if w > 1000 else float(w)
                parsed.append((str(d)[:10], round(w_kg, 1)))
        if not parsed:
            return None
        parsed.sort(key=lambda x: x[0])
        latest_date, latest_w = parsed[-1]
        week_ago_w = None
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        older = [p for p in parsed if p[0] <= cutoff]
        if older:
            week_ago_w = older[-1][1]
        return {"latest": latest_w, "latest_date": latest_date, "week_ago": week_ago_w}
    except Exception as e:
        logger.warning(f"Не вдалось отримати вагу: {e}")
        return None


def compute_day_color(metrics: dict, alerts: list) -> str:
    """GREEN/YELLOW/RED — рекомендація по інтенсивності."""
    alert_metrics = {a["metric"] for a in alerts}
    # Червоний: критичні відновлювальні проблеми
    if any("HRV" in m for m in alert_metrics):
        return "RED"
    if metrics.get("body_battery_max") is not None and metrics["body_battery_max"] < THRESHOLDS["body_battery_min"]:
        return "RED"
    if metrics.get("sleep_hours") is not None and metrics["sleep_hours"] < THRESHOLDS["sleep_min_hours"]:
        return "RED"
    if metrics.get("rhr") is not None and metrics["rhr"] > THRESHOLDS["rhr_max"]:
        return "RED"
    # Жовтий: неідеальне відновлення
    if alerts:
        return "YELLOW"
    if metrics.get("body_battery_max") is not None and metrics["body_battery_max"] < 60:
        return "YELLOW"
    if metrics.get("stress") is not None and metrics["stress"] > 50:
        return "YELLOW"
    return "GREEN"


def get_plan_block() -> list[str]:
    """Блок прогресивного плану повернення у спорт."""
    week = ast.sport_week()
    for (w_from, w_to), info in ac.PROGRESSIVE_PLAN.items():
        if w_from <= week <= w_to:
            return [
                f"📈 Повернення у форму — тиждень {week}:",
                f"   План: {info['plan']}",
                f"   Фокус: {info['focus']}",
            ]
    return []


def get_weight_block(weight: dict | None) -> list[str]:
    """Блок ваги/ІМТ."""
    if not weight:
        return []
    latest = weight["latest"]
    bmi = ast.calc_bmi(latest)
    category = ast.bmi_category(bmi)
    to_goal = latest - ast.TARGET_WEIGHT_KG
    lines = [f"⚖️ Вага: {latest} кг (ІМТ {bmi:.1f} — {category}) | до цілі {ast.TARGET_WEIGHT_KG:.0f} кг: {'-' if to_goal > 0 else '+'}{abs(to_goal):.1f} кг"]
    if weight.get("week_ago"):
        diff = latest - weight["week_ago"]
        arrow = "↓" if diff < 0 else ("↑" if diff > 0 else "→")
        lines.append(f"   Тиждень тому: {weight['week_ago']} кг {arrow} ({diff:+.1f})")
    lines.append(f"   💡 {ac.tip_of_the_day(ac.WEIGHT_LOSS_TIPS)}")
    return lines


def get_streak_block() -> list[str]:
    """Блок стріку без алкоголю."""
    days = ast.streak_days()
    milestone = ac.milestone_message(days)
    lines = [f"🍺❌ День {days} без алкоголю!"]
    if milestone:
        lines.append(f"   {milestone}")
    lines.append(f"   {ac.tip_of_the_day(ac.ALCOHOL_FREE_MOTIVATION)}")
    return lines


def get_challenge_progress(client: Garmin) -> list[str]:
    """Прогрес активного челенджу."""
    challenge = ast.get_active_challenge()
    if not challenge:
        return []
    ch = challenge.get("challenge", {})
    start = challenge.get("start_date")
    ch_id = ch.get("id", "")
    title = ch.get("title", "Челендж")
    if not start:
        return []
    try:
        activities = client.get_activities_by_date(start, date.today().isoformat()) or []
    except Exception as e:
        logger.warning(f"Не вдалось отримати активності для челенджу: {e}")
        return [f"🏆 {title} — прогрес недоступний (Garmin)"]

    types = set(ch.get("garmin_types", []))
    # Доповнюємо типи з каталогу — стан міг бути записаний до оновлення каталогу
    for c in ac.CHALLENGES:
        if c.get("id") == ch_id:
            types.update(c.get("garmin_types", []))
    matched = [a for a in activities
               if a.get("activityType", {}).get("typeKey", "") in types]

    if ch_id in ("run_20k", "ride_100k"):
        total_km = sum((a.get("distance") or 0) / 1000 for a in matched)
        goal = 20 if ch_id == "run_20k" else 100
        pct = min(100, total_km / goal * 100)
        return [f"🏆 {title}: {total_km:.1f}/{goal} км ({pct:.0f}%)"]
    elif ch_id == "walk_10k":
        days_done = 0
        start_d = date.fromisoformat(start)
        d = start_d
        while d <= date.today():
            try:
                steps_data = client.get_steps_data(d.isoformat()) or []
                total = sum(s.get("steps", 0) for s in steps_data if isinstance(s, dict))
                if total >= 10000:
                    days_done += 1
            except Exception:
                pass
            d += timedelta(days=1)
        return [f"🏆 {title}: {days_done}/7 днів"]
    else:
        # берпі/скакалка: чест-система — дні з відповідною активністю
        days = {a.get("startTimeLocal", "")[:10] for a in matched if a.get("startTimeLocal")}
        return [f"🏆 {title}: {len(days)}/7 днів (відмічайся активністю на Garmin)"]


def ask_gemini_tip(prompt: str) -> str | None:
    """Один виклик Gemini для поради дня. None при будь-якій помилці."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-3.6-flash:generateContent?key=" + api_key)
    payload = {
        "system_instruction": {"parts": [{"text": (
            "Ти дієтолог-асистент. Дай ОДНУ конкретну пораду по харчуванню "
            "на сьогодні для цього користувача: 1-2 речення, українською, "
            "практично і без води. Без медичних призначень і діагнозів."
        )}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0.7,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = (result.get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0].get("text", "")).strip()
        return text or None
    except Exception as e:
        logger.warning(f"Gemini-порада недоступна: {e}")
        return None


def get_nutrition_block(client: Garmin, metrics: dict) -> list[str]:
    """Ціль калорій + порада дня (AI з fallback на статичний банк)."""
    weight_data = get_weight_data(client)
    weight = weight_data["latest"] if weight_data else 105.0
    kcal, protein, water = ast.calc_calorie_target(weight)
    lines = [f"🍽 Харчування: ціль ~{kcal} ккал | білок ~{protein} г | вода ~{water} л"]

    # Порада дня: кеш → AI → статичний банк
    tip = ast.get_daily_tip()
    if tip:
        lines.append(f"💡 {tip}")
        return lines

    context = [
        f"Вага: {weight} кг (ціль {ast.TARGET_WEIGHT_KG:.0f} кг), ціль {kcal} ккал/день, "
        f"білок {protein} г, вода {water} л.",
        f"Днів без алкоголю: {ast.streak_days()}. Тиждень повернення у спорт: {ast.sport_week()} "
        f"(тижні 1-2: 3 легкі сесії Z2 20-30 хв).",
    ]
    if metrics.get("sleep_hours"):
        context.append(f"Сон вчора: {metrics['sleep_hours']} год.")
    try:
        acts = client.get_activities(0, 3) or []
        for a in acts:
            tk = a.get("activityType", {}).get("typeKey", "")
            if tk not in ast.EXCLUDED_ACTIVITY_TYPES:
                context.append(
                    f"Вчора/сьогодні тренування: {a.get('activityName')}, "
                    f"{(a.get('duration') or 0) / 60:.0f} хв.")
                break
    except Exception:
        pass
    tip = ask_gemini_tip("\n".join(context)) or ac.tip_of_the_day(ac.NUTRITION_FALLBACK_TIPS)
    ast.set_daily_tip(tip)
    lines.append(f"💡 {tip}")
    return lines


def build_assistant_blocks(client: Garmin, metrics: dict, alerts: list) -> list[str]:
    """Збирає всі блоки асистента для щоденного повідомлення."""
    lines = []

    # Колір дня
    color = compute_day_color(metrics, alerts)
    color_emoji = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}[color]
    lines.append(f"🎯 День: {color_emoji} {color} — {ac.DAY_RECOMMENDATIONS[color]}")

    # Прогресивний план
    lines.extend(get_plan_block())

    # Вага/ІМТ
    lines.extend(get_weight_block(get_weight_data(client)))

    # Харчування: ціль калорій + AI-порада дня
    try:
        lines.extend(get_nutrition_block(client, metrics))
    except Exception as e:
        logger.warning(f"Не вдалось побудувати блок харчування: {e}")
        lines.append(f"😴 Порада дня: {ac.tip_of_the_day(ac.SLEEP_HEALTH_TIPS)}")

    # Ліки (ранок)
    lines.append("💊 Ранковий прийом ліків — не забудь")

    # Стрік
    lines.extend(get_streak_block())

    # Челендж
    challenge_lines = get_challenge_progress(client)
    if challenge_lines:
        lines.extend(challenge_lines)
    else:
        pending = ast.get_pending_challenge()
        if pending:
            lines.append(f"🏆 Запропоновано: {pending.get('title')} — відповідай 'так' або 'ні'")

    return lines


def check_daily_health(force_telegram: bool = False):
    """Основна функція перевірки здоров'я."""
    logger.info("Початок щоденної перевірки")

    # Захист від повторної відправки в той самий день (для scheduled runs)
    if not force_telegram and already_sent_today():
        logger.info("Щоденний звіт вже відправлено сьогодні — пропускаємо")
        print("Вже відправлено сьогодні")
        return

    client = connect_garmin()
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    alerts = []
    metrics = {}

    # --- 1. Resting Heart Rate ---
    try:
        hr_data = client.get_heart_rates(today)
        rhr = hr_data.get("restingHeartRate")
        if not rhr:
            hr_data = client.get_heart_rates(yesterday)
            rhr = hr_data.get("restingHeartRate")

        if rhr:
            metrics["rhr"] = rhr
            rhr_history = get_rhr_history(client)
            rhr_avg = sum(rhr_history) / len(rhr_history) if rhr_history else rhr

            if rhr > THRESHOLDS["rhr_max"]:
                alerts.append({
                    "metric": "Resting HR",
                    "value": f"{rhr} bpm",
                    "norm": f"<{THRESHOLDS['rhr_max']} bpm",
                    "cause": "Можливі: недосип, стрес, перетренованість, алкоголь",
                    "advice": "Відпочинок, уникати інтенсивних тренувань сьогодні",
                })
            elif rhr_history and (rhr - rhr_avg) > THRESHOLDS["rhr_spike"]:
                alerts.append({
                    "metric": "Resting HR (стрибок)",
                    "value": f"{rhr} bpm (середній: {rhr_avg:.0f})",
                    "norm": f"Відхилення <{THRESHOLDS['rhr_spike']} bpm",
                    "cause": "Різкий стрибок — можливий початок хвороби або перевтома",
                    "advice": "Знизити навантаження, спостерігати 1-2 дні",
                })
    except Exception as e:
        logger.warning(f"Не вдалось отримати HR: {e}")

    # --- 2. HRV ---
    try:
        hrv_data = client.get_hrv_data(today)
        if not hrv_data or "hrvSummary" not in hrv_data:
            hrv_data = client.get_hrv_data(yesterday)

        if hrv_data and "hrvSummary" in hrv_data:
            hrv_summary = hrv_data["hrvSummary"]
            hrv_status = hrv_summary.get("status", "UNKNOWN")
            hrv_value = hrv_summary.get("lastNightAvg") or hrv_summary.get("weeklyAvg")

            if hrv_value:
                metrics["hrv"] = hrv_value

            # Перевірка статусу
            if hrv_status == "LOW":
                alerts.append({
                    "metric": "HRV статус",
                    "value": f"LOW ({hrv_value} ms)" if hrv_value else "LOW",
                    "norm": "BALANCED або вище",
                    "cause": "Стрес, недосип, фізичне навантаження",
                    "advice": "Легке тренування або відпочинок, раніше лягти спати",
                })
            elif hrv_value:
                # Перевірка різкого падіння
                hrv_history = get_hrv_weekly(client)
                if hrv_history:
                    hrv_avg = sum(hrv_history) / len(hrv_history)
                    drop_pct = ((hrv_avg - hrv_value) / hrv_avg) * 100
                    if drop_pct > THRESHOLDS["hrv_drop_pct"]:
                        alerts.append({
                            "metric": "HRV (різке падіння)",
                            "value": f"{hrv_value} ms (тижневий середній: {hrv_avg:.0f} ms, -{drop_pct:.0f}%)",
                            "norm": f"Падіння <{THRESHOLDS['hrv_drop_pct']}%",
                            "cause": "Накопичена втома, стрес або початок хвороби",
                            "advice": "Уникати інтенсивних тренувань, відновлення пріоритет",
                        })
    except Exception as e:
        logger.warning(f"Не вдалось отримати HRV: {e}")

    # --- 3. Stress ---
    try:
        stress_data = client.get_stress_data(today)
        if not stress_data:
            stress_data = client.get_stress_data(yesterday)

        if stress_data:
            avg_stress = stress_data.get("overallStressLevel") or stress_data.get("avgStressLevel")
            if avg_stress:
                metrics["stress"] = avg_stress
                if avg_stress > THRESHOLDS["stress_avg_max"]:
                    alerts.append({
                        "metric": "Стрес (середній за добу)",
                        "value": f"{avg_stress}",
                        "norm": f"<{THRESHOLDS['stress_avg_max']}",
                        "cause": "Високий рівень стресу: робота, недосип, перетренованість",
                        "advice": "Дихальні вправи, прогулянка, зменшити кофеїн",
                    })
    except Exception as e:
        logger.warning(f"Не вдалось отримати Stress: {e}")

    # --- 4. Body Battery ---
    try:
        bb_data = client.get_body_battery(today)
        if not bb_data:
            bb_data = client.get_body_battery(yesterday)

        if bb_data:
            bb_values = []
            if isinstance(bb_data, list):
                for entry in bb_data:
                    val = entry.get("chargedValue") or entry.get("bodyBatteryLevel")
                    if val:
                        bb_values.append(val)
            elif isinstance(bb_data, dict):
                charged = bb_data.get("charged") or bb_data.get("chargedValue")
                if charged:
                    bb_values.append(charged)
                body_battery_list = bb_data.get("bodyBatteryValuesArray", [])
                for item in body_battery_list:
                    if isinstance(item, (list, tuple)) and len(item) > 1:
                        bb_values.append(item[1])
                    elif isinstance(item, dict):
                        val = item.get("value") or item.get("bodyBatteryLevel")
                        if val:
                            bb_values.append(val)

            if bb_values:
                bb_max = max(bb_values)
                metrics["body_battery_max"] = bb_max
                if bb_max < THRESHOLDS["body_battery_min"]:
                    alerts.append({
                        "metric": "Body Battery (макс за добу)",
                        "value": f"{bb_max}%",
                        "norm": f">{THRESHOLDS['body_battery_min']}%",
                        "cause": "Погане відновлення: мало сну, високий стрес",
                        "advice": "Полегшити день, раніше лягти, уникати важких тренувань",
                    })
    except Exception as e:
        logger.warning(f"Не вдалось отримати Body Battery: {e}")

    # --- 5. Sleep ---
    try:
        sleep_data = client.get_sleep_data(today)
        if not sleep_data:
            sleep_data = client.get_sleep_data(yesterday)

        if sleep_data:
            sleep_seconds = None
            if isinstance(sleep_data, dict):
                daily_sleep = sleep_data.get("dailySleepDTO", sleep_data)
                sleep_seconds = daily_sleep.get("sleepTimeSeconds")
                if not sleep_seconds:
                    sleep_seconds = daily_sleep.get("totalSleepTimeInSeconds")

            if sleep_seconds:
                sleep_hours = sleep_seconds / 3600
                metrics["sleep_hours"] = round(sleep_hours, 1)
                if sleep_hours < THRESHOLDS["sleep_min_hours"]:
                    alerts.append({
                        "metric": "Сон",
                        "value": f"{sleep_hours:.1f} годин",
                        "norm": f">{THRESHOLDS['sleep_min_hours']} годин",
                        "cause": "Недостатнє відновлення",
                        "advice": "Раніше лягти сьогодні, уникати кофеїну після 14:00",
                    })
    except Exception as e:
        logger.warning(f"Не вдалось отримати Sleep: {e}")

    # --- 6. Тиск ---
    try:
        bp_data = client.get_blood_pressure(today)
        if not bp_data or not bp_data.get("measurementSummaries"):
            bp_data = client.get_blood_pressure(yesterday)
        if bp_data and bp_data.get("measurementSummaries"):
            latest = None
            for summary in bp_data["measurementSummaries"]:
                for m in summary.get("measurements", []):
                    if latest is None or m.get("measurementTimestampLocal", "") > latest.get("measurementTimestampLocal", ""):
                        latest = m
            if not latest:
                latest = bp_data["measurementSummaries"][-1]
                systolic = latest.get("highSystolic")
                diastolic = latest.get("highDiastolic")
            else:
                systolic = latest.get("systolic")
                diastolic = latest.get("diastolic")
            if systolic and diastolic:
                metrics["bp_systolic"] = systolic
                metrics["bp_diastolic"] = diastolic
                if systolic > THRESHOLDS["bp_systolic_high"] or diastolic > THRESHOLDS["bp_diastolic_high"]:
                    alerts.append({
                        "metric": "Тиск",
                        "value": f"{systolic}/{diastolic} мм рт.ст.",
                        "norm": "<140/90 мм рт.ст.",
                        "cause": "Підвищений АТ — стрес, недосип, кофеїн, сіль, алкоголь",
                        "advice": "Повторити вимірювання через 15 хв у спокої. Якщо стабільно високий — повідомити лікаря",
                    })
                elif systolic < THRESHOLDS["bp_systolic_low"] or diastolic < THRESHOLDS["bp_diastolic_low"]:
                    alerts.append({
                        "metric": "Тиск",
                        "value": f"{systolic}/{diastolic} мм рт.ст.",
                        "norm": ">90/60 мм рт.ст.",
                        "cause": "Знижений АТ — зневоднення, ефект Трипліксану, перевтома",
                        "advice": "Випити води, встати повільно, уникати різких рухів. При запамороченні — лікар",
                    })
    except Exception as e:
        logger.warning(f"Не вдалось отримати тиск: {e}")

    # --- Збереження даних ---
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_file = DATA_DIR / f"daily_{today}.json"
    try:
        with open(data_file, "w") as f:
            json.dump({"date": today, "metrics": metrics, "alerts_count": len(alerts)}, f, indent=2)
    except Exception as e:
        logger.warning(f"Не вдалось зберегти дані: {e}")

    # --- Формування повідомлення ---
    now_str = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    lines = [f"📅 {now_str}"]

    if alerts:
        new_alerts, suppressed_alerts = dedupe_alerts(alerts)

        if new_alerts:
            lines.append("⚠️ Увага! Виявлено відхилення:")
            for alert in new_alerts:
                lines.append(f"- {alert['metric']}: {alert['value']} (норма: {alert['norm']})")
                lines.append(f"  Можлива причина: {alert['cause']}")
                lines.append(f"  Рекомендація: {alert['advice']}")

        if suppressed_alerts:
            lines.append("ℹ️ Досі стежимо (вже повідомлено раніше):")
            for alert in suppressed_alerts:
                lines.append(f"- {alert['metric']}: {alert['value']}")
    else:
        rhr = metrics.get("rhr", "N/A")
        hrv = metrics.get("hrv", "N/A")
        stress = metrics.get("stress", "N/A")
        bb = metrics.get("body_battery_max", "N/A")
        sleep = metrics.get("sleep_hours", "N/A")
        bp = f"{metrics.get('bp_systolic', 'N/A')}/{metrics.get('bp_diastolic', 'N/A')}"
        lines.append(f"✅ Всі показники в нормі. RHR: {rhr}, HRV: {hrv}, Stress: {stress}, BB max: {bb}, Сон: {sleep}г, Тиск: {bp}")

    # --- Блоки асистента (завжди) ---
    lines.append("")
    try:
        lines.extend(build_assistant_blocks(client, metrics, alerts))
    except Exception as e:
        logger.warning(f"Не вдалось побудувати блоки асистента: {e}")

    report = "\n".join(lines)
    print(report)

    # --- Відправка в Telegram (завжди — корисний контент щодня) ---
    send_telegram(report)
    mark_sent_today()

    logger.info(f"Перевірка завершена. Алертів: {len(alerts)}")


if __name__ == "__main__":
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Примусово відправити в Telegram")
    args = parser.parse_args()
    check_daily_health(force_telegram=args.telegram)
