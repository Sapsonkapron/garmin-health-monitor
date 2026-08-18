# Garmin Health Monitor

Щоденний моніторинг здоров'я через Garmin Connect з відправкою алертів у Telegram.

## Що робить

- **daily_check.py** — щодня о 9:00 за Києвом перевіряє показники з Garmin Connect (RHR, HRV, стрес, сон, тиск) і відправляє повідомлення в Telegram.
- **weekly_report.py** — щонеділі о 18:00 за Києвом формує тижневий звіт з порівнянням із попереднім тижнем.
- **telegram_bot.py** — локальний Telegram-бот, який записує вагу і тиск у Garmin Connect.
- **log_metrics.py** — дозволяє вручну записати вагу та/або тиск у Garmin Connect.
- **training_reminder.py** — локальний скрипт для рандомізованих нагадувань про тренування.

## Секрети GitHub

У налаштуваннях репозиторію (Settings → Secrets and variables → Actions → New repository secret) додай:

| Secret | Опис |
|---|---|
| `GARMIN_EMAIL` | Email від Garmin Connect |
| `GARMIN_PASSWORD` | Пароль від Garmin Connect |
| `TELEGRAM_BOT_TOKEN` | Токен бота від @BotFather |
| `TELEGRAM_CHAT_ID` | Твій chat ID від @userinfobot |

## Локальний запуск

1. Створи `.env` за прикладом `.env.example`.
2. Встанови залежності:
   ```bash
   pip install -r requirements.txt
   ```
3. Запусти:
   ```bash
   python daily_check.py --telegram
   python weekly_report.py
   python telegram_bot.py       # бот для ваги/тиску
   python log_metrics.py --weight 92.5 --bp 130/80
   ```

## Розклад GitHub Actions

| Workflow | Час за Києвом | Cron UTC |
|---|---|---|
| Daily | 9:00 ранку | `0 6 * * *` |
| Weekly | Неділя 18:00 | `0 15 * * 0` |

Щоб запустити вручну: перейди у вкладку **Actions**, вибери workflow і натисни **Run workflow**.

## Telegram-бот: формати повідомлень

Надсилай боту:
- Вага: `92.5 кг` або `92 кг`
- Тиск: `(130/80/72)` або `(130/80)`

Бот відповість підтвердженням і запише дані в Garmin Connect.

## Макети для macOS

Якщо хочеш продовжити запускати локально на Mac, використовуй файли в папці `macos/`:
- `com.garmin.daily_check.plist`
- `run_with_retry.sh`
