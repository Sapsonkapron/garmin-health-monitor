#!/usr/bin/env python3
"""
Спільний стан health-асистента: стрік, челенджі, Telegram offset, новини.
Зберігається у /tmp/garmin_data/assistant_state.json (персиститься через actions/cache).
"""

import json
from pathlib import Path
from datetime import date

DATA_DIR = Path("/tmp/garmin_data")
STATE_FILE = DATA_DIR / "assistant_state.json"

# --- Конфігурація користувача ---
STREAK_START_DATE = date(2026, 9, 6)      # день 1 без алкоголю (14.09 — день 9)
HEIGHT_CM = 179
TARGET_WEIGHT_KG = 95.0
SPORT_RETURN_DATE = date(2026, 9, 14)     # день 1 повернення у спорт
AGE_YEARS = 37                            # для BMR (Mifflin-St Jeor)
ACTIVITY_FACTOR = 1.3                     # сидячий ритм поза тренуваннями (~5000 кроків)
CALORIE_DEFICIT = 500                     # помірний дефіцит для схуднення
PROTEIN_PER_KG = 1.6                      # г білка на кг ЦІЛЬОВОЇ ваги

# Активності Garmin, що НЕ є тренуваннями — не враховуються в аналізі і звітах
EXCLUDED_ACTIVITY_TYPES = {"fishing", "fishing_v2"}


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Не вдалось зберегти assistant_state.json: {e}")


def update_state(**kwargs):
    """Оновлює окремі ключі стану."""
    state = read_state()
    state.update(kwargs)
    write_state(state)


# --- Стрік без алкоголю ---
def streak_days(today: date = None) -> int:
    d = today or date.today()
    return (d - STREAK_START_DATE).days + 1


# --- Повернення у спорт ---
def sport_week(today: date = None) -> int:
    """Номер тижня повернення у спорт (1-based)."""
    d = today or date.today()
    return max(1, ((d - SPORT_RETURN_DATE).days // 7) + 1)


# --- BMI ---
def calc_bmi(weight_kg: float) -> float:
    h_m = HEIGHT_CM / 100
    return weight_kg / (h_m * h_m)


def bmi_category(bmi: float) -> str:
    if bmi < 18.5:
        return "недостатня вага"
    if bmi < 25:
        return "норма"
    if bmi < 30:
        return "надлишкова вага"
    return "ожиріння"


# --- Челенджі ---
def get_pending_challenge() -> dict | None:
    return read_state().get("pending_challenge")


def set_pending_challenge(challenge: dict | None):
    state = read_state()
    if challenge is None:
        state.pop("pending_challenge", None)
    else:
        state["pending_challenge"] = challenge
    write_state(state)


def get_active_challenge() -> dict | None:
    return read_state().get("active_challenge")


def set_active_challenge(challenge: dict | None):
    state = read_state()
    if challenge is None:
        state.pop("active_challenge", None)
    else:
        state["active_challenge"] = challenge
    write_state(state)


# --- Telegram offset ---
def get_telegram_offset() -> int | None:
    return read_state().get("telegram_offset")


def set_telegram_offset(offset: int):
    update_state(telegram_offset=offset)


# --- Новини ---
def get_last_news_link() -> str | None:
    return read_state().get("last_news_link")


def set_last_news_link(link: str):
    update_state(last_news_link=link)


# --- AI-режим (звернення до бота як до AI-агента) ---
def is_ai_mode() -> bool:
    return bool(read_state().get("ai_mode", False))


def set_ai_mode(enabled: bool):
    update_state(ai_mode=enabled)


# --- Харчування: денна ціль + кеш поради дня ---
def calc_calorie_target(weight_kg: float) -> tuple[int, int, float]:
    """(kcal_ціль, білок_г, вода_л) з актуальної ваги. Mifflin-St Jeor, чол."""
    bmr = 10 * weight_kg + 6.25 * HEIGHT_CM - 5 * AGE_YEARS + 5
    tdee = bmr * ACTIVITY_FACTOR
    target = max(tdee - CALORIE_DEFICIT, bmr)  # не нижче BMR
    kcal = int(round(target / 50.0) * 50)
    protein = int(PROTEIN_PER_KG * TARGET_WEIGHT_KG)
    water = round(weight_kg * 0.03, 1)  # ~30 мл/кг
    return kcal, protein, water


def get_daily_tip(day: str | None = None) -> str | None:
    """Порада дня, якщо вже генерувалась для цієї дати (кеш від дублів)."""
    d = day or date.today().isoformat()
    tip = read_state().get("daily_tip")
    if tip and tip.get("date") == d:
        return tip.get("text")
    return None


def set_daily_tip(text: str, day: str | None = None):
    d = day or date.today().isoformat()
    update_state(daily_tip={"date": d, "text": text})


# --- Локальні записи тиску (fallback, якщо write-API недоступне) ---
def add_local_bp(systolic: int, diastolic: int, pulse: int, ts: str):
    state = read_state()
    records = state.get("local_bp_records", [])
    records.append({"systolic": systolic, "diastolic": diastolic, "pulse": pulse, "ts": ts})
    state["local_bp_records"] = records[-30:]  # зберігаємо останні 30
    write_state(state)
