#!/usr/bin/env bash
# Службы systemd для панелей (запускается от root). Панель сама поднимает бота, если он должен работать.
set -euo pipefail
for app in turbo; do
  cat > /etc/systemd/system/tradebot-$app.service <<EOF
[Unit]
Description=TradeBot $app — панель и бот passivbot
After=network-online.target
Wants=network-online.target

[Service]
User=tradebot
WorkingDirectory=/opt/tradebot/$app
Environment=PYTHONUTF8=1
Environment=TB_LITE=1
Environment=MALLOC_ARENA_MAX=2
Environment=PATH=/home/tradebot/.cargo/bin:/opt/tradebot/$app/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/tradebot/$app/venv/bin/python tb4/panel/server.py --no-browser
Restart=always
RestartSec=10
# перезапуск панели не должен убивать работающего бота: панель найдёт его по PID и времени старта
KillMode=process
# при нехватке памяти система закрывает сначала панель (она сама ставит себе очередь 300, боту — 0);
# закрытый процесс не останавливает всю службу: панель перезапустит systemd, бота — сама панель
OOMPolicy=continue

[Install]
WantedBy=multi-user.target
EOF
done
# Сторожевой таймер (остановка бота при упавшей панели) убран 04.10.2026: SIGINT оставлял позиции
# без присмотра, а выход при обвале BTC теперь есть и в самом TB Core. Снимаем его, если он был поставлен.
for app in turbo; do
  systemctl disable --now tradebot-$app-watchdog.timer >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/tradebot-$app-watchdog.timer /etc/systemd/system/tradebot-$app-watchdog.service
done
# Telegram-бот: уведомления и команды. Без токена просто ждёт настройки (deploy/telegram_setup.bat).
cat > /etc/systemd/system/tradebot-telegram.service <<'EOF'
[Unit]
Description=TradeBot — Telegram-бот (уведомления и отчёты)
After=network-online.target tradebot-turbo.service
Wants=network-online.target

[Service]
User=tradebot
WorkingDirectory=/opt/tradebot
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/tradebot/telegram/telegram_bot.py
Restart=always
RestartSec=15
# при нехватке памяти — первым на закрытие (торгующие боты важнее)
OOMScoreAdjust=500

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable tradebot-turbo tradebot-telegram >/dev/null 2>&1
systemctl restart tradebot-turbo tradebot-telegram
sleep 3
for app in turbo telegram; do echo "tradebot-$app: $(systemctl is-active tradebot-$app)"; done
ss -ltnp | grep -E ':8771' || true
