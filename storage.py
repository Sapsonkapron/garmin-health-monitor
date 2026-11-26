"""Persistent, server-authoritative state and food diary."""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
STATE_FILE = Path("/tmp/garmin_data/assistant_state.json")
ALERT_STATE_FILE = Path("/tmp/garmin_data/alert_state.json")


def _load_env() -> None:
    path = Path(__file__).parent / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def db_call(action: str, payload: dict | None = None):
    _load_env()
    url = os.environ.get("MONITOR_STORE_URL", "")
    secret = os.environ.get("MONITOR_STORE_SECRET", "")
    if not url or not secret:
        raise RuntimeError("Не налаштовано MONITOR_STORE_URL або MONITOR_STORE_SECRET")
    request = urllib.request.Request(
        url,
        data=json.dumps({"action": action, **(payload or {})}).encode(),
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Сховище недоступне (HTTP {exc.code})") from exc
    if not result.get("ok"):
        raise RuntimeError("Сховище повернуло помилку")
    return result.get("data")


def _local(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_local(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _get(key: str) -> dict:
    path = STATE_FILE if key == "assistant" else ALERT_STATE_FILE
    value = db_call("state", {"key": key})
    if not isinstance(value, dict):
        raise RuntimeError("Сховище повернуло некоректний стан")
    _write_local(path, value)
    return value


def update_state(**changes) -> None:
    value = db_call("merge", {"key": "assistant", "changes": changes})
    _write_local(STATE_FILE, value)


def claim_notification(kind: str) -> bool:
    """Atomic before sending; no duplicate when two runners wake together."""
    claimed = bool(db_call("claim", {"kind": kind}))
    if claimed:
        alerts = _local(ALERT_STATE_FILE)
        alerts[f"_last_{kind}"] = _date_key()
        _write_local(ALERT_STATE_FILE, alerts)
    return claimed


def release_notification(kind: str) -> None:
    db_call("release", {"kind": kind})
    alerts = _local(ALERT_STATE_FILE)
    alerts.pop(f"_last_{kind}", None)
    _write_local(ALERT_STATE_FILE, alerts)


def sent_today(kind: str) -> bool:
    return _get("alerts").get(f"_last_{kind}") == _date_key()


def mark_sent_today(kind: str) -> bool:
    """Compatibility marker; the atomic claim is the source of truth."""
    alerts = _local(ALERT_STATE_FILE)
    alerts[f"_last_{kind}"] = _date_key()
    _write_local(ALERT_STATE_FILE, alerts)
    return True


def _date_key() -> str:
    return datetime.now(KYIV).date().isoformat()


def set_pending_challenge(challenge: dict | None) -> None:
    update_state(pending_challenge=challenge)


def set_active_challenge(challenge: dict | None, start_date: str | None = None) -> None:
    if challenge and "challenge" in challenge:
        update_state(active_challenge=challenge)
    else:
        update_state(active_challenge={"challenge": challenge, "start_date": start_date or _date_key()} if challenge else None)


def get_pending_challenge() -> dict | None:
    return _get("assistant").get("pending_challenge")


def get_active_challenge() -> dict | None:
    return _get("assistant").get("active_challenge")


def confirm_challenge_day(repetitions: int | None = None) -> bool:
    active = get_active_challenge()
    if not active:
        return False
    days = dict(active.get("confirmed_days", {}))
    days[_date_key()] = repetitions if repetitions is not None else True
    active["confirmed_days"] = days
    update_state(active_challenge=active)
    return True


def set_mode(**changes) -> None:
    current = _get("assistant")
    update_state(mode={**current.get("mode", {}), **changes})


def is_ai_mode() -> bool:
    return bool(_get("assistant").get("ai_mode", False))


def get_history() -> list:
    return _get("assistant").get("mode", {}).get("history", [])


def save_history(history: list) -> None:
    current = _get("assistant")
    update_state(mode={**current.get("mode", {}), "history": history[-20:]})


def next_prompt_index() -> int:
    return int(_get("assistant").get("prompt_index", 0))


def bump_prompt_index() -> None:
    update_state(prompt_index=next_prompt_index() + 1)


def parse_weight(text: str) -> float | None:
    match = re.search(r"\b(\d{2,3}[.,]\d{1})\s*(?:кг|kg)\b", text, re.I)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value if 30 < value < 300 else None


def parse_bp(text: str) -> tuple[int, int, int | None] | None:
    match = re.search(r"\b(\d{2,3})\s*[/\\]\s*(\d{2,3})(?:\s*[/\\]\s*(\d{2,3}))?\b", text)
    if not match:
        return None
    systolic, diastolic = int(match.group(1)), int(match.group(2))
    pulse = int(match.group(3)) if match.group(3) else None
    if not (60 < systolic < 260 and 30 < diastolic < 160) or systolic <= diastolic:
        return None
    return systolic, diastolic, pulse


def add_metrics(**metrics) -> None:
    update_state(metrics={**_get("assistant").get("metrics", {}), **metrics})


def get_food_entries(start_day: str, end_day: str) -> list[dict]:
    rows = db_call("food_list") or []
    result = []
    for row in rows:
        eaten_at = datetime.fromisoformat(row["eaten_at"].replace("Z", "+00:00"))
        local_date = eaten_at.astimezone(KYIV).date().isoformat()
        if start_day <= local_date <= end_day:
            result.append({**row, "local_date": local_date})
    return result


def get_food_log(day: str | None = None) -> list[dict]:
    target = day or _date_key()
    return get_food_entries(target, target)


def get_pending_food() -> dict | None:
    state = db_call("state", {"key": "assistant"})
    if not isinstance(state, dict):
        raise RuntimeError("Сховище повернуло некоректний стан")
    return state.get("pending_food")


def save_pending_food(entry: dict) -> None:
    update_state(pending_food=entry)


def _validate_analysis(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        raise ValueError("Не розпізнано продукти")
    for item in data["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip() or len(item["name"]) > 200:
            raise ValueError("Некоректний опис продукту")
        for field, maximum in (("kcal", 10000), ("protein", 2000), ("fat", 2000), ("carbs", 2000)):
            if isinstance(item.get(field), bool) or not isinstance(item.get(field), (int, float)):
                raise ValueError("Некоректна поживна цінність")
            if not math.isfinite(item[field]) or not 0 <= item[field] <= maximum:
                raise ValueError("Поживна цінність поза допустимим діапазоном")
    return data


def confirm_pending_food() -> dict | None:
    pending = get_pending_food()
    if not pending:
        return None
    analysis = _validate_analysis(pending["analysis"])
    uuid.UUID(pending["id"])
    return db_call("food_confirm", {
        "id": pending["id"], "eaten_at": pending["eaten_at"], "source": pending["source"],
        "description": pending["description"], "analysis": analysis,
        "uncertainty": pending["uncertainty"],
    })


def discard_pending_food() -> None:
    update_state(pending_food=None)


def delete_last_food() -> bool:
    return bool(db_call("food_delete_last"))


def get_profile() -> dict:
    return db_call("profile") or {}


def update_profile(sex: str, age_years: int, height_cm: int, target_weight_kg: float, activity_factor: float = 1.3) -> dict:
    return db_call("profile_set", {
        "sex": sex, "age_years": age_years, "height_cm": height_cm,
        "target_weight_kg": target_weight_kg, "activity_factor": activity_factor,
    })


def nutrition_targets(weight_kg: float, age: int, height_cm: int, activity_factor: float = 1.3, target_weight_kg: float | None = None, sex: str = "male") -> dict:
    # Mifflin-St Jeor; exercise is already included in TDEE.
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "male" else -161)
    tdee = bmr * activity_factor
    calories = round(max(bmr * 1.05, tdee - 400) / 10) * 10
    protein = round(1.6 * (target_weight_kg or weight_kg))
    fat = round(calories * 0.28 / 9)
    carbs = max(0, round((calories - protein * 4 - fat * 9) / 4))
    return {"bmr": round(bmr), "tdee": round(tdee), "calories": calories,
            "protein": protein, "fat": fat, "carbs": carbs}
