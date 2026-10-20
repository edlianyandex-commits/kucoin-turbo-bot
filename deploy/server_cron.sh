#!/usr/bin/env bash
# Расписание отчётов (запускается от tradebot). Часовой пояс сервера — Москва (server_setup.sh).
set -euo pipefail
R=/opt/tradebot/deploy/report.py
( crontab -l 2>/dev/null | grep -v 'deploy/report.py' || true
  echo "17 * * * * /usr/bin/python3 $R sample >/dev/null 2>&1"
  echo "5 7 * * * /usr/bin/python3 $R daily >/dev/null 2>&1"
) | crontab -
chmod 600 /opt/tradebot/*/api-keys.json 2>/dev/null || true
mkdir -p /opt/tradebot/reports && chmod 700 /opt/tradebot/reports
echo "расписание:"; crontab -l
