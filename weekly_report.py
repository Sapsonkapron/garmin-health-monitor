#!/usr/bin/env python3
"""
Тижневий аналітичний звіт здоров'я через Garmin Connect.
Запуск в неділю ввечері. Порівнює поточний тиждень з попереднім.
"""

import os
import sys
import json
import logging
import argparse
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from garminconnect import Garmin

import assistant_content as ac
import assistant_state as ast

# --- Конфігурація ---
DATA_DIR = Path("/tmp/garmin_data")
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

VO2MAX_GOAL = 44
STATE_FILE = DATA_DIR / "weekly_state.json"

# --- Логування ---
logging.basicConfig(
    filename=LOG_DIR / "weekly_report.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning(f"Не вдалось зберегти weekly_state.json: {e}")


def already_sent_this_week() -> bool:
    """Чи вже генерували звіт за поточний тиждень?"""
    state = _read_state()
    return state.get("last_week") == get_week_dates(0)[0].isoformat()


def mark_sent_this_week():
    state = _read_state()
    state["last_week"] = get_week_dates(0)[0].isoformat()
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


def get_week_dates(offset: int = 0) -> tuple[date, date]:
    """Повертає (понеділок, неділя) для тижня з offset від поточного."""
    today = date.today()
    # Поточний понеділок
    monday = today - timedelta(days=today.weekday())
    # Зміщення
    monday = monday - timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def collect_week_data(client: Garmin, start: date, end: date) -> dict:
    """Збирає всі метрики за вказаний тиждень."""
    data = {
        "rhr_values": [],
        "hrv_values": [],
        "hrv_statuses": [],
        "stress_values": [],
        "body_battery_charged": [],
        "body_battery_drained": [],
        "sleep_hours": [],
        "sleep_scores": [],
        "activities": [],
        "vo2max": None,
    }

    current = start
    while current <= end and current <= date.today():
        day_str = current.isoformat()

        # RHR
        try:
            hr = client.get_heart_rates(day_str)
            rhr = hr.get("restingHeartRate")
            if rhr:
                data["rhr_values"].append(rhr)
        except Exception:
            pass

        # HRV
        try:
            hrv = client.get_hrv_data(day_str)
            if hrv and "hrvSummary" in hrv:
                summary = hrv["hrvSummary"]
                val = summary.get("lastNightAvg") or summary.get("weeklyAvg")
                if val:
                    data["hrv_values"].append(val)
                status = summary.get("status")
                if status:
                    data["hrv_statuses"].append(status)
        except Exception:
            pass

        # Stress
        try:
            stress = client.get_stress_data(day_str)
            if stress:
                avg = stress.get("overallStressLevel") or stress.get("avgStressLevel")
                if avg:
                    data["stress_values"].append(avg)
        except Exception:
            pass

        # Body Battery
        try:
            bb = client.get_body_battery(day_str)
            if bb and isinstance(bb, dict):
                charged = bb.get("totalChargedValue") or bb.get("charged")
                drained = bb.get("totalDrainedValue") or bb.get("drained")
                if charged:
                    data["body_battery_charged"].append(charged)
                if drained:
                    data["body_battery_drained"].append(drained)
        except Exception:
            pass

        # Sleep
        try:
            sleep = client.get_sleep_data(day_str)
            if sleep and isinstance(sleep, dict):
                daily = sleep.get("dailySleepDTO", sleep)
                seconds = daily.get("sleepTimeSeconds") or daily.get("totalSleepTimeInSeconds")
                if seconds:
                    data["sleep_hours"].append(seconds / 3600)
                score = daily.get("sleepScores", {}).get("overall", {}).get("value")
                if score:
                    data["sleep_scores"].append(score)
        except Exception:
            pass

        current += timedelta(days=1)

    # Activities за тиждень
    try:
        activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
        if activities:
            for act in activities:
                data["activities"].append({
                    "type": act.get("activityType", {}).get("typeKey", "unknown"),
                    "name": act.get("activityName", ""),
                    "duration_min": round(act.get("duration", 0) / 60, 1),
                    "avg_hr": act.get("averageHR"),
                    "max_hr": act.get("maxHR"),
                    "calories": act.get("calories"),
                    "distance_km": round(act.get("distance", 0) / 1000, 1) if act.get("distance") else None,
                })
    except Exception as e:
        logger.warning(f"Не вдалось отримати активності: {e}")

    # VO2max
    try:
        vo2_data = client.get_max_metrics(end.isoformat())
        if vo2_data:
            if isinstance(vo2_data, list) and vo2_data:
                for item in vo2_data:
                    generic = item.get("generic", {})
                    vo2 = generic.get("vo2MaxValue")
                    if vo2:
                        data["vo2max"] = vo2
                        break
            elif isinstance(vo2_data, dict):
                data["vo2max"] = vo2_data.get("vo2MaxValue")
    except Exception as e:
        logger.warning(f"Не вдалось отримати VO2max: {e}")

    return data


def trend_arrow(current: float, previous: float) -> str:
    """Стрілка тренду."""
    if current > previous:
        return "↑"
    elif current < previous:
        return "↓"
    return "→"


def safe_avg(values: list) -> float | None:
    """Безпечне середнє."""
    return sum(values) / len(values) if values else None


def send_telegram(text: str) -> bool:
    """Відправляє звіт у Telegram, розбиваючи на повідомлення ≤4000 символів.
    HTML parse_mode: новина містить клікабельний лінк; прев'ю вимкнене."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram token або chat_id не налаштовані")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Розбиття по рядках
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    ok = True
    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception as e:
            logger.error(f"Помилка відправки в Telegram: {e}")
            ok = False
    return ok


def escape_html(text: str) -> str:
    """Екранує HTML-символи, зберігаючи згенерований anchor-тег новини."""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def translate_to_ukrainian(text: str) -> str | None:
    """Переклад EN→UK через безкоштовний gtx-ендпоінт Google Translate.
    Повертає None при збої (fallback на англійський текст)."""
    if not text:
        return None
    try:
        params = urllib.parse.urlencode({
            "client": "gtx", "sl": "en", "tl": "uk", "dt": "t", "q": text,
        })
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(part[0] for part in data[0] if part[0]).strip()
    except Exception as e:
        logger.warning(f"Переклад недоступний: {e}")
        return None


def get_week_weights(client: Garmin, start: date, end: date) -> list[float]:
    """Ваги (кг) за тиждень з body composition."""
    try:
        bc = client.get_body_composition(start.isoformat(), end.isoformat())
        entries = []
        if isinstance(bc, dict):
            entries = bc.get("dateWeightList", [])
        elif isinstance(bc, list):
            entries = bc
        weights = []
        for e in entries:
            w = e.get("weight")
            if w:
                weights.append(w / 1000 if w > 1000 else float(w))
        return weights
    except Exception as e:
        logger.warning(f"Не вдалось отримати вагу: {e}")
        return []


def get_fresh_health_news() -> dict | None:
    """Свіжа новина з BBC Health: заголовок і короткий опис українською.
    Повертає {title_uk, desc_uk, link} або None при збої/відсутності нових."""
    last_link = ast.get_last_news_link()
    for feed_url in ac.HEALTH_RSS_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)
            for item in root.iter("item"):
                title_en = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc_en = (item.findtext("description") or "").strip()
                if not title_en or not link or link == last_link:
                    continue
                ast.set_last_news_link(link)

                title_uk = translate_to_ukrainian(title_en) or title_en
                # Опис: 1-2 речення, перекладені
                desc_uk = ""
                if desc_en:
                    sentences = [s.strip() for s in desc_en.split(". ") if s.strip()]
                    short = ". ".join(sentences[:2])
                    if short and not short.endswith("."):
                        short += "."
                    desc_uk = translate_to_ukrainian(short) or short
                return {"title_uk": title_uk, "desc_uk": desc_uk, "link": link}
        except Exception as e:
            logger.warning(f"RSS {feed_url} недоступний: {e}")
            continue
    return None


def get_plan_execution(activities: list) -> list[str]:
    """Виконання прогресивного плану тижня."""
    week = ast.sport_week()
    plan_info = None
    for (w_from, w_to), info in ac.PROGRESSIVE_PLAN.items():
        if w_from <= week <= w_to:
            plan_info = info
            break
    if not plan_info:
        return []
    # кількість сесій за тиждень
    target = 3 if week <= 4 else 4
    done = len(activities)
    status = "✅ виконано" if done >= target else f"⚠️ {done}/{target} сесій"
    return [
        f"📈 Повернення у форму — тиждень {week}: {status}",
        f"   План був: {plan_info['plan']}",
    ]


def propose_challenge():
    """Пропонує челендж на наступний тиждень (якщо немає активного/запропонованого)."""
    if ast.get_active_challenge() or ast.get_pending_challenge():
        return None
    week_no = date.today().isocalendar()[1]
    challenge = ac.CHALLENGES[week_no % len(ac.CHALLENGES)]
    ast.set_pending_challenge({
        "id": challenge["id"],
        "title": challenge["title"],
        "garmin_types": challenge["garmin_types"],
    })
    return challenge["proposal"]


def generate_report(force: bool = False):
    """Генерує тижневий звіт."""
    logger.info("Початок генерації тижневого звіту")

    # Захист від дублювання в той самий тиждень (для scheduled-резервів;
    # workflow_dispatch через cron-job.org іде з --force)
    if not force and already_sent_this_week():
        logger.info("Звіт за поточний тиждень вже генерувався — пропускаємо")
        print("Звіт за поточний тиждень вже генерувався")
        return

    client = connect_garmin()

    # Поточний та попередній тиждень
    curr_start, curr_end = get_week_dates(0)
    prev_start, prev_end = get_week_dates(1)

    curr_data = collect_week_data(client, curr_start, curr_end)
    prev_data = collect_week_data(client, prev_start, prev_end)

    # --- Формування звіту ---
    report_lines = []
    report_lines.append(f"📊 Тижневий звіт здоров'я: {curr_start.strftime('%d.%m')} – {curr_end.strftime('%d.%m.%Y')}")
    report_lines.append("=" * 50)

    # VO2max
    vo2_curr = curr_data["vo2max"]
    vo2_prev = prev_data["vo2max"]
    if vo2_curr:
        line = f"🫁 VO2max: {vo2_curr}"
        if vo2_prev:
            line += f" {trend_arrow(vo2_curr, vo2_prev)} (попередній: {vo2_prev})"
        remaining = VO2MAX_GOAL - vo2_curr
        line += f" | До мети ({VO2MAX_GOAL}): {remaining:+.1f}" if remaining > 0 else " | 🎯 Мета досягнута!"
        report_lines.append(line)
    report_lines.append("")

    # RHR
    rhr_avg = safe_avg(curr_data["rhr_values"])
    rhr_prev = safe_avg(prev_data["rhr_values"])
    if rhr_avg:
        line = f"❤️ RHR середній: {rhr_avg:.0f} bpm"
        if rhr_prev:
            line += f" {trend_arrow(rhr_prev, rhr_avg)} (попередній: {rhr_prev:.0f})"
            # Для RHR менше = краще, тому інвертуємо стрілку
        report_lines.append(line)

    # HRV
    hrv_avg = safe_avg(curr_data["hrv_values"])
    hrv_prev = safe_avg(prev_data["hrv_values"])
    if hrv_avg:
        line = f"💚 HRV середній: {hrv_avg:.0f} ms"
        if hrv_prev:
            line += f" {trend_arrow(hrv_avg, hrv_prev)} (попередній: {hrv_prev:.0f})"
        # % BALANCED
        balanced_count = curr_data["hrv_statuses"].count("BALANCED")
        total_statuses = len(curr_data["hrv_statuses"])
        if total_statuses:
            pct = (balanced_count / total_statuses) * 100
            line += f" | BALANCED: {pct:.0f}% днів"
        report_lines.append(line)

    # Stress
    stress_avg = safe_avg(curr_data["stress_values"])
    stress_prev = safe_avg(prev_data["stress_values"])
    if stress_avg:
        line = f"😰 Стрес середній: {stress_avg:.0f}"
        if stress_prev:
            # Для стресу менше = краще
            line += f" {trend_arrow(stress_prev, stress_avg)} (попередній: {stress_prev:.0f})"
        report_lines.append(line)

    # Body Battery
    bb_charged_avg = safe_avg(curr_data["body_battery_charged"])
    bb_prev_avg = safe_avg(prev_data["body_battery_charged"])
    if bb_charged_avg:
        line = f"🔋 Body Battery (середній заряд): {bb_charged_avg:.0f}"
        if bb_prev_avg:
            line += f" {trend_arrow(bb_charged_avg, bb_prev_avg)} (попередній: {bb_prev_avg:.0f})"
        report_lines.append(line)

    # Sleep
    sleep_avg = safe_avg(curr_data["sleep_hours"])
    sleep_prev = safe_avg(prev_data["sleep_hours"])
    if sleep_avg:
        line = f"😴 Сон середній: {sleep_avg:.1f}г"
        if sleep_prev:
            line += f" {trend_arrow(sleep_avg, sleep_prev)} (попередній: {sleep_prev:.1f}г)"
        score_avg = safe_avg(curr_data["sleep_scores"])
        if score_avg:
            line += f" | Якість: {score_avg:.0f}/100"
        report_lines.append(line)

    report_lines.append("")

    # Вага / ІМТ
    curr_weights = get_week_weights(client, curr_start, curr_end)
    prev_weights = get_week_weights(client, prev_start, prev_end)
    if curr_weights:
        w_avg = safe_avg(curr_weights)
        w_latest = curr_weights[-1]
        bmi = ast.calc_bmi(w_latest)
        line = f"⚖️ Вага: {w_latest:.1f} кг (ІМТ {bmi:.1f} — {ast.bmi_category(bmi)})"
        if prev_weights:
            w_prev = safe_avg(prev_weights)
            diff = w_avg - w_prev
            line += f" {trend_arrow(w_prev, w_avg)} ({diff:+.1f} кг до сер. попер. тижня)"
        to_goal = w_latest - ast.TARGET_WEIGHT_KG
        line += f" | до цілі {ast.TARGET_WEIGHT_KG:.0f} кг: {'-' if to_goal > 0 else '+'}{abs(to_goal):.1f}"
        report_lines.append(line)

    # Тренування
    activities = curr_data["activities"]
    if activities:
        report_lines.append(f"🏋️ Тренування за тиждень: {len(activities)}")
        total_duration = sum(a["duration_min"] for a in activities)
        report_lines.append(f"   Загальний час: {total_duration:.0f} хв ({total_duration/60:.1f}г)")

        # Групування по типу
        types = {}
        for act in activities:
            t = act["type"]
            types[t] = types.get(t, 0) + 1
        type_str = ", ".join(f"{k}: {v}" for k, v in types.items())
        report_lines.append(f"   Типи: {type_str}")

        # Детально
        for act in activities:
            parts = [f"   • {act['name'] or act['type']}"]
            parts.append(f"{act['duration_min']:.0f} хв")
            if act["avg_hr"]:
                parts.append(f"HR avg/max: {act['avg_hr']}/{act['max_hr'] or '?'}")
            if act["distance_km"]:
                parts.append(f"{act['distance_km']} км")
            report_lines.append(" | ".join(parts))
    else:
        report_lines.append("🏋️ Тренувань на Garmin за тиждень: 0")

    # Порівняння кількості тренувань
    prev_activities = prev_data["activities"]
    if activities or prev_activities:
        report_lines.append(f"   Порівняно з попереднім тижнем: {len(activities)} vs {len(prev_activities)}")

    # Виконання прогресивного плану
    report_lines.extend(get_plan_execution(activities))

    report_lines.append("")

    # --- Підсумок ---
    report_lines.append("─" * 50)
    report_lines.append("📝 Підсумок:")

    # Прогрес до мети
    if vo2_curr:
        if vo2_curr >= VO2MAX_GOAL:
            report_lines.append(f"   🎯 VO2max мета досягнута! ({vo2_curr}/{VO2MAX_GOAL})")
        else:
            progress = ((vo2_curr / VO2MAX_GOAL) * 100)
            report_lines.append(f"   Прогрес до мети VO2max {VO2MAX_GOAL}: {vo2_curr} ({progress:.0f}%)")

    # Загальна оцінка тижня
    good_signs = []
    bad_signs = []

    if rhr_avg and rhr_prev and rhr_avg < rhr_prev:
        good_signs.append("RHR знизився")
    elif rhr_avg and rhr_prev and rhr_avg > rhr_prev:
        bad_signs.append("RHR зріс")

    if hrv_avg and hrv_prev and hrv_avg > hrv_prev:
        good_signs.append("HRV покращився")
    elif hrv_avg and hrv_prev and hrv_avg < hrv_prev:
        bad_signs.append("HRV знизився")

    if stress_avg and stress_prev and stress_avg < stress_prev:
        good_signs.append("стрес знизився")
    elif stress_avg and stress_prev and stress_avg > stress_prev:
        bad_signs.append("стрес зріс")

    if sleep_avg and sleep_avg >= 7:
        good_signs.append(f"сон достатній ({sleep_avg:.1f}г)")
    elif sleep_avg and sleep_avg < 6:
        bad_signs.append(f"мало сну ({sleep_avg:.1f}г)")

    if good_signs:
        report_lines.append(f"   ✅ Позитив: {', '.join(good_signs)}")
    if bad_signs:
        report_lines.append(f"   ⚠️ Увага: {', '.join(bad_signs)}")

    if not good_signs and not bad_signs:
        report_lines.append("   Стабільний тиждень, без суттєвих змін.")

    # Стрік без алкоголю
    days = ast.streak_days()
    streak_line = f"   🍺❌ Стрік без алкоголю: день {days}"
    milestone = ac.milestone_message(days)
    if milestone:
        streak_line += f" | {milestone}"
    report_lines.append(streak_line)

    # Новина тижня (%%NEWS_ANCHOR%% — плейсхолдер для HTML-лінка)
    news_anchor_html = ""
    news = get_fresh_health_news()
    if news:
        report_lines.append("")
        report_lines.append("📰 Новина тижня про здоров'я: %%NEWS_ANCHOR%%")
        if news["desc_uk"]:
            report_lines.append(f"   {news['desc_uk']}")
        news_anchor_html = (
            f'<a href="{news["link"]}">{escape_html(news["title_uk"])}</a>'
        )

    # Пропозиція челенджу
    proposal = propose_challenge()
    if proposal:
        report_lines.append("")
        report_lines.append(proposal)
    else:
        active = ast.get_active_challenge()
        if active:
            report_lines.append("")
            report_lines.append(f"🏆 Активний челендж: {active.get('challenge', {}).get('title')} — прогрес у ранкових звітах")

    # Підказка про кнопки (легенда винесена у кнопку бота)
    report_lines.append("")
    report_lines.append("💡 Кнопки 'Що означають показники' і 'Аналіз тренування' — під ранковим повідомленням")

    # Вивід
    report = "\n".join(report_lines)
    print(report)

    # Збереження (у файл — читабельна версія з лінком)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    report_file = DATA_DIR / f"weekly_report_{curr_start.isoformat()}.txt"
    try:
        file_report = report
        if news:
            file_report = report.replace(
                "%%NEWS_ANCHOR%%", f'{news["title_uk"]} — {news["link"]}')
        with open(report_file, "w") as f:
            f.write(file_report)
    except Exception as e:
        logger.warning(f"Не вдалось зберегти звіт: {e}")

    mark_sent_this_week()

    # Відправка в Telegram (HTML: екрануємо все, крім anchor новини)
    report_html = escape_html(report)
    if news_anchor_html:
        report_html = report_html.replace("%%NEWS_ANCHOR%%", news_anchor_html)
    send_telegram(report_html)

    logger.info("Тижневий звіт згенеровано")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Ігнорувати захист від дублювання (для workflow_dispatch)")
    args = parser.parse_args()
    generate_report(force=args.force)
