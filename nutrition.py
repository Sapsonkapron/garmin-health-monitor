from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import storage

MAX_PHOTO_BYTES = 10 * 1024 * 1024
KYIV = ZoneInfo("Europe/Kyiv")


def _request_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def download_photo(file_id: str) -> bytes:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Telegram не налаштований")
    params = urllib.parse.urlencode({"file_id": file_id})
    info = _request_json(f"https://api.telegram.org/bot{token}/getFile?{params}")
    result = info.get("result", {})
    path = result.get("file_path", "")
    if not info.get("ok") or not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path.split("/"):
        raise ValueError("Не вдалося отримати фото")
    if result.get("file_size", 0) > MAX_PHOTO_BYTES:
        raise ValueError("Фото завелике")
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{path}", timeout=45) as response:
        photo = response.read(MAX_PHOTO_BYTES + 1)
    if not photo or len(photo) > MAX_PHOTO_BYTES:
        raise ValueError("Фото порожнє або завелике")
    return photo


def estimate(description: str, photo: bytes | None = None) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("ШІ для харчування не налаштований")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Некоректна назва моделі")
    instruction = (
        "Оціни їжу за описом та фото, якщо воно є. Не вигадуй продукти, які неможливо "
        "обґрунтовано визначити. Якщо їжу не розпізнано, поверни порожній items. "
        "На кожну складову вкажи назву українською, приблизну вагу у грамах та "
        "калорійність і білки, жири, вуглеводи у грамах для цієї порції. "
        "Назви припущення та невизначеність у полі uncertainty. Поверни тільки JSON: "
        '{"items":[{"name":"назва","grams":100,"kcal":100,"protein":10,"fat":5,"carbs":4}],'
        '"uncertainty":"опис припущень"}. Опис: ' + (description[:2000] or "без опису")
    )
    parts = [{"text": instruction}]
    if photo:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(photo).decode("ascii")}})
    payload = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?" + urllib.parse.urlencode({"key": api_key})
    result = _request_json(url, payload)
    candidates = result.get("candidates", [])
    if not candidates:
        raise ValueError("ШІ не зміг оцінити страву")
    text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", []))
    analysis = json.loads(text)
    storage._validate_analysis(analysis)
    uncertainty = analysis.get("uncertainty")
    if not isinstance(uncertainty, str) or not uncertainty.strip():
        analysis["uncertainty"] = "Склад і вага порцій приблизні; уточни їх перед підтвердженням."
    for item in analysis["items"]:
        grams = item.get("grams")
        if isinstance(grams, bool) or not isinstance(grams, (int, float)) or not 0 < grams <= 10000:
            raise ValueError("ШІ не зміг оцінити вагу порції")
    return analysis


def prepare(description: str, photo: bytes | None = None) -> str:
    if storage.get_pending_food():
        return "Є непідтверджена страва. Надішли /food_confirm або /food_cancel, перш ніж додавати іншу."
    if not description.strip() and not photo:
        return "Напиши /food і опис страви з приблизною вагою, або надішли фото."
    analysis = estimate(description, photo)
    storage.save_pending_food({
        "id": str(uuid.uuid4()),
        "eaten_at": datetime.now(KYIV).isoformat(),
        "source": "photo" if photo else "text",
        "description": description[:2000] or "Фото страви",
        "analysis": analysis,
        "uncertainty": analysis["uncertainty"][:1000],
    })
    return format_analysis(analysis) + "\n\nЦе оцінка, не запис. /food_confirm — зберегти; /food_cancel — відхилити. Для виправлення відхили й надішли уточнений опис."


def format_analysis(analysis: dict) -> str:
    items = analysis["items"]
    lines = ["🍽 Приблизна оцінка:"]
    for item in items:
        lines.append(f"• {item['name']}: ~{item['grams']:.0f} г, ~{item['kcal']:.0f} ккал")
    totals = {key: sum(item[key] for item in items) for key in ("kcal", "protein", "fat", "carbs")}
    lines.append(f"Разом: ~{totals['kcal']:.0f} ккал; Б {totals['protein']:.0f} г / Ж {totals['fat']:.0f} г / В {totals['carbs']:.0f} г.")
    lines.append(f"Невизначеність: {analysis.get('uncertainty', 'порції та склад приблизні')}")
    return "\n".join(lines)


def today_log() -> str:
    entries = storage.get_food_log()
    if not entries:
        return "Сьогодні підтверджених записів немає. Харчовий облік неповний."
    items = [item for entry in entries for item in entry.get("analysis", {}).get("items", [])]
    totals = {key: sum(item[key] for item in items) for key in ("kcal", "protein", "fat", "carbs")}
    return (f"🍽 Сьогодні записано {len(entries)} прийомів: приблизно {totals['kcal']:.0f} ккал; "
            f"Б {totals['protein']:.0f} г / Ж {totals['fat']:.0f} г / В {totals['carbs']:.0f} г.\n"
            "Підсумок неповний, якщо не внесено всі прийоми їжі.")
