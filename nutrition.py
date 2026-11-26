from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import zxingcpp
from PIL import Image

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


def valid_barcode(code: str) -> bool:
    if not re.fullmatch(r"(?:[0-9]{8}|[0-9]{12,14})", code):
        return False
    total = sum(int(digit) * (3 if index % 2 == 0 else 1)
                for index, digit in enumerate(reversed(code[:-1])))
    return (10 - total % 10) % 10 == int(code[-1])


def read_barcodes(photo: bytes) -> list[str]:
    with Image.open(io.BytesIO(photo)) as image:
        if image.width * image.height > 25_000_000:
            raise ValueError("Фото завелике для зчитування")
        barcodes = zxingcpp.read_barcodes(
            image.convert("RGB"), formats=(zxingcpp.BarcodeFormat.EAN13 | zxingcpp.BarcodeFormat.EAN8 |
                                           zxingcpp.BarcodeFormat.UPCA | zxingcpp.BarcodeFormat.ITF14),
        )
    return sorted({barcode.text for barcode in barcodes if barcode.valid and valid_barcode(barcode.text)})


def lookup_barcode(code: str) -> dict | None:
    if not valid_barcode(code):
        raise ValueError("Некоректний штрихкод")
    fields = "code,product_name,product_name_uk,brands,nutriments"
    url = f"https://world.openfoodfacts.org/api/v3/product/{code}?" + urllib.parse.urlencode({"fields": fields})
    request = urllib.request.Request(url, headers={
        "User-Agent": "GarminHealthMonitor/1.0 (https://github.com/Sapsonkapron/garmin-health-monitor)",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    product = result.get("product")
    if result.get("status") not in ("success", "success_with_warnings") or not isinstance(product, dict):
        return None
    product_code = str(product.get("code", ""))
    if not valid_barcode(product_code) or product_code.lstrip("0") != code.lstrip("0"):
        raise ValueError("Код товару не збігається")
    name = product.get("product_name_uk") or product.get("product_name")
    if not isinstance(name, str) or not name.strip():
        return None
    nutrients = product.get("nutriments") or {}
    fields = {"kcal": "energy-kcal_100g", "protein": "proteins_100g", "fat": "fat_100g", "carbs": "carbohydrates_100g"}
    values = {}
    for field, key in fields.items():
        value = nutrients.get(key)
        maximum = 900 if field == "kcal" else 100
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
            return None
        values[field] = value
    brand = product.get("brands")
    return {"code": code, "name": name.strip()[:160], "brand": brand.strip()[:80] if isinstance(brand, str) else "", "per_100g": values}


def barcode_portion(text: str) -> float | None:
    match = re.fullmatch(r"\s*(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:г|грам(?:ів|и)?|g)?\s*", text, re.IGNORECASE)
    if not match:
        return None
    grams = float(match.group(1).replace(",", "."))
    return grams if 0 < grams <= 10000 else None


def prepare_barcode(product: dict, grams: float, source: str) -> str:
    if storage.get_pending_food():
        return "Є непідтверджена страва. Натисни «Підтвердити страву» або «Скасувати оцінку»."
    values = product["per_100g"]
    item = {"name": product["name"], "grams": grams}
    item.update({field: value * grams / 100 for field, value in values.items()})
    uncertainty = (f"Дані Open Food Facts для коду {product['code']}; звір назву і БЖВ з етикеткою. "
                   "Вага порції вказана користувачем; дані бази можуть бути неточними.")
    analysis = storage._validate_analysis({"items": [item], "uncertainty": uncertainty})
    storage.save_pending_food({
        "id": str(uuid.uuid4()),
        "eaten_at": datetime.now(KYIV).isoformat(),
        "source": source,
        "description": f"Штрихкод {product['code']}: {product['name']}",
        "analysis": analysis,
        "uncertainty": uncertainty,
    })
    return format_analysis(analysis) + "\n\nЦе оцінка, не запис. Натисни «Підтвердити страву» або «Скасувати оцінку»."


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
    try:
        analysis = estimate(description, photo)
    except Exception as error:
        status = getattr(error, "code", None)
        print(f"Food analysis failure: error={type(error).__name__}, http_status={status if isinstance(status, int) else 'none'}", flush=True)
        raise
    try:
        storage.save_pending_food({
            "id": str(uuid.uuid4()),
            "eaten_at": datetime.now(KYIV).isoformat(),
            "source": "photo" if photo else "text",
            "description": description[:2000] or "Фото страви",
            "analysis": analysis,
            "uncertainty": analysis["uncertainty"][:1000],
        })
    except Exception as error:
        print(f"Food pending save failure: error={type(error).__name__}", flush=True)
        raise
    return format_analysis(analysis) + "\n\nЦе оцінка, не запис. Натисни «Підтвердити страву» або «Скасувати оцінку». Для виправлення скасуй оцінку й надішли уточнений опис."


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
