#!/usr/bin/env python3
"""
Нагадування про тренування з рандомізованими повідомленнями.
Використання: python training_reminder.py --type bjj|gravel
"""

import argparse
import random
import sys

# --- Повідомлення BJJ ---
BJJ_MESSAGES = [
    "Сьогодні BJJ о 20:00! 🥋 Не забудь: капу, рашгард, воду. Гарного тренування!",
    "BJJ час! 🥋 20:00 на матах. Капа, рашгард, вода — перевір сумку. Осс!",
    "Вечірнє BJJ о 20:00 🥋 Візьми капу, рашгард, воду. Працюй техніку, не силу!",
    "Нагадування: BJJ сьогодні о 20:00! 🥋 Капа + рашгард + вода. Зроби хоча б один сабмішн!",
    "20:00 — час котитись! 🥋 Капа, рашгард, вода в сумці? Тоді вперед. Осс!",
]

# --- Повідомлення Gravel ---
GRAVEL_MESSAGES = [
    "Сьогодні день гравелу! 🚴 60-90 хв у зоні Z2 (118-137 bpm). Їдь з годинником — Garmin порахує VO2max.",
    "Gravel day! 🚴 Тримай пульс 118-137 bpm (Z2), 60-90 хв. Годинник на руці = VO2max трекінг.",
    "Час крутити педалі! 🚴 Сьогодні гравел: Z2 зона, 118-137 bpm, 60-90 хвилин. Garmin все запише.",
    "Гравел сьогодні! 🚴 План: 60-90 хв, зона Z2 (пульс 118-137). Їдь рівно, без ривків — це база для VO2max.",
    "На велосипед! 🚴 Сьогодні easy ride: 60-90 хв у Z2 (118-137 bpm). Спокійно, без героїзму. Garmin зафіксує прогрес.",
]

MESSAGES = {
    "bjj": BJJ_MESSAGES,
    "gravel": GRAVEL_MESSAGES,
}


def main():
    parser = argparse.ArgumentParser(description="Нагадування про тренування")
    parser.add_argument(
        "--type",
        required=True,
        choices=["bjj", "gravel"],
        help="Тип тренування: bjj або gravel",
    )
    args = parser.parse_args()

    messages = MESSAGES[args.type]
    message = random.choice(messages)
    print(message)


if __name__ == "__main__":
    main()
