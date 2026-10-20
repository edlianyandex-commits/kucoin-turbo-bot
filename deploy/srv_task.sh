#!/usr/bin/env bash
# Разведка сервера TB Core — ТОЛЬКО ЧТЕНИЕ. Ничего не меняет, не перезапускает.
T=/opt/tradebot/turbo
echo "== host: $(hostname) | $(date '+%F %T %Z') | up $(uptime -p)"
echo "== user: $(id -un)"
echo "== service tradebot-turbo: $(systemctl is-active tradebot-turbo 2>&1) (since $(systemctl show -p ActiveEnterTimestamp --value tradebot-turbo 2>/dev/null))"
echo "== passivbot live:"; pgrep -af "passivbot live" || echo "  (нет процесса)"
echo "== resources:"; free -m | awk 'NR==2{print "  mem MB: "$2" total, "$7" avail"}'; df -h / | awk 'NR==2{print "  disk /: "$2" total, "$4" free"}'
echo "== md5:"
for f in "$T/configs/tb4/live.json" "$T/tb4/panel/presets.json" "$T/tb4/panel/data/settings.json"; do
  [ -f "$f" ] && echo "  $(md5sum "$f" | cut -d' ' -f1)  $(stat -c%s "$f")B  $f" || echo "  MISSING $f"
done
S=/opt/tradebot/reports/live_turbo_state.json
echo "== transfers_cum: $(python3 -c "import json;print(json.load(open('$S')).get('transfers_cum'))" 2>&1)"
LOG=$(find "$T" -maxdepth 3 -name kucoin_turbo.log 2>/dev/null | head -1)
echo "== log: ${LOG:-не найден}"
if [ -n "$LOG" ]; then
  echo "  size $(stat -c%s "$LOG")B, mtime $(stat -c%y "$LOG" | cut -d. -f1)"
  echo "  ERROR total: $(grep -c ERROR "$LOG")"
  echo "  --- последние 15 строк:"; tail -n 15 "$LOG" | cut -c1-220
fi
