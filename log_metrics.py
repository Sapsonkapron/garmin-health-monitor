#!/usr/bin/env python3
"""
Вносить вагу та/або тиск в Garmin Connect.
Використання:
  python3 log_metrics.py --weight 92.5
  python3 log_metrics.py --bp 130/80
  python3 log_metrics.py --bp 130/80 --pulse 72
  python3 log_metrics.py --weight 92.5 --bp 130/80 --pulse 72
"""

import os
import argparse
import sys
import logging
from pathlib import Path
from garminconnect import Garmin

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "log_metrics.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_garmin_credentials():
    """Отримує логін/пароль Garmin з env."""
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        logger.error("GARMIN_EMAIL або GARMIN_PASSWORD не налаштовані")
        print("❌ GARMIN_EMAIL або GARMIN_PASSWORD не налаштовані")
        sys.exit(1)
    return email, password


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weight", type=float, help="Вага в кг (напр. 92.5)")
    parser.add_argument("--bp", type=str, help="Тиск у форматі систолічний/діастолічний (напр. 130/80)")
    parser.add_argument("--pulse", type=int, default=0, help="Пульс при вимірі тиску (необов'язково)")
    args = parser.parse_args()

    if not args.weight and not args.bp:
        print("❌ Вкажи --weight і/або --bp")
        sys.exit(1)

    # Логін
    try:
        email, password = get_garmin_credentials()
        client = Garmin(email, password)
        client.login()
    except Exception as e:
        print(f"❌ Не вдалось підключитись до Garmin: {e}")
        logger.error(f"Login failed: {e}")
        sys.exit(1)

    results = []

    # Вага
    if args.weight:
        try:
            client.add_weigh_in(weight=args.weight, unitKey="kg")
            results.append(f"⚖️ Вага {args.weight} кг — записано ✅")
            logger.info(f"Weight logged: {args.weight} kg")
        except Exception as e:
            results.append(f"❌ Помилка запису ваги: {e}")
            logger.error(f"Weight error: {e}")

    # Тиск
    if args.bp:
        try:
            parts = args.bp.split("/")
            systolic = int(parts[0])
            diastolic = int(parts[1])
            pulse = args.pulse if args.pulse else 0
            client.set_blood_pressure(systolic=systolic, diastolic=diastolic, pulse=pulse)
            bp_str = f"{systolic}/{diastolic}"
            if pulse:
                bp_str += f", пульс {pulse}"
            results.append(f"🫀 Тиск {bp_str} — записано ✅")
            logger.info(f"BP logged: {systolic}/{diastolic}, pulse={pulse}")
        except Exception as e:
            results.append(f"❌ Помилка запису тиску: {e}")
            logger.error(f"BP error: {e}")

    for r in results:
        print(r)


if __name__ == "__main__":
    main()
