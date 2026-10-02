#!/bin/sh
# Основной процесс cron-контейнера: стартовые задачи, расписание из config/crontab, cron.
set -eu

SCHEDULE=/app/config/crontab
LOG=/var/log/stamp-flow.log

# cron не наследует окружение контейнера: передаём нужные переменные через /etc/environment
printenv | grep -E '^(CONFIG_DIR|TZ|HH_SSH_KEY|PYTHONUNBUFFERED|PLAYWRIGHT_BROWSERS_PATH)=' >> /etc/environment || true

chown -R app:app /app/config
touch "$LOG"
chown app:app "$LOG"

# При старте: обновить токен hh и поднять резюме
su app -s /bin/sh -c 'python -m stamp_flow --config-dir /app/config refresh-token; python -m stamp_flow --config-dir /app/config update-resumes' >> "$LOG" 2>&1 || true

if [ -f "$SCHEDULE" ]; then
    crontab -u app "$SCHEDULE"
else
    echo "Нет $SCHEDULE: запусти на хосте 'python run.py', он создаст расписание по conf.json." >> "$LOG"
fi

cron
exec tail -F "$LOG"