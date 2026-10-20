#!/usr/bin/env bash
# Проверка ботов на сервере (только чтение)
python3 - <<'PY'
import json, urllib.request
for port, name in ((8770, "основной"), (8771, "турбо")):
    try:
        s = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=30))
    except Exception as exc:
        print(f"панель {name} (:{port}) — не отвечает: {exc}")
        continue
    line = (f"панель {name} (:{port}) — облегчённый режим: {s.get('lite')} | пресет: {s['settings']['preset']} | "
            f"монет доступно: {s['coins']['ok_count']} | ключи заданы: {s['keys']['set']} | бот работает: {s['live']['running']}")
    print(line)
PY
echo "--- память:"; free -m | awk 'NR==2{print "занято " $3 " МБ из " $2 ", доступно " $7}'
echo "--- диск:"; df -h / | awk 'NR==2{print "занято " $3 " из " $2 ", свободно " $4}'
echo "--- службы:"; for a in turbo; do echo "tradebot-$a: $(systemctl is-active tradebot-$a), автозапуск: $(systemctl is-enabled tradebot-$a)"; done
