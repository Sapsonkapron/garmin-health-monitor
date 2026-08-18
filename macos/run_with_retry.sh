#!/bin/bash
# Обгортка для скриптів з ретраєм при відсутності мережі
# Використання: run_with_retry.sh <script.py> [args...]
# Перевіряє мережу кожну годину, максимум 8 спроб

export PATH="/Users/serhii/.verdent/verdent-projects/Fly-off/garmin-monitor/.venv/bin:/opt/homebrew/bin:$PATH"

SCRIPT="$1"
shift
ARGS="$@"
MAX_RETRIES=8
RETRY_INTERVAL=3600  # 1 година

for i in $(seq 1 $MAX_RETRIES); do
    # Перевірка мережі
    if curl -s --max-time 10 https://connect.garmin.com > /dev/null 2>&1; then
        python3 "$SCRIPT" $ARGS
        exit $?
    fi
    
    if [ $i -lt $MAX_RETRIES ]; then
        echo "$(date): Немає мережі, спроба $i/$MAX_RETRIES. Наступна через 1 годину." >> /Users/serhii/.verdent/verdent-projects/Fly-off/garmin-monitor/logs/retry.log
        sleep $RETRY_INTERVAL
    fi
done

echo "$(date): Не вдалось підключитись після $MAX_RETRIES спроб" >> /Users/serhii/.verdent/verdent-projects/Fly-off/garmin-monitor/logs/retry.log
exit 1
