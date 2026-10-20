#!/usr/bin/env bash
# Сведения о сервере (только чтение)
. /etc/os-release
echo "ОС: $PRETTY_NAME"
echo "ядер: $(nproc)"
free -m | awk 'NR==2{print "память, МБ: " $2 " (свободно " $7 ")"}'
df -h / | awk 'NR==2{print "диск: " $2 ", свободно " $4}'
echo "swap: $(swapon --show --noheadings | awk '{print $1, $3}')"
echo "внешний IP: $(curl -s -m 10 https://api.ipify.org)"
echo "python3: $(python3 --version 2>&1)"
echo "часовой пояс: $(timedatectl show -p Timezone --value 2>/dev/null)"
echo "время синхронизировано: $(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
echo "KuCoin публичный API: $(curl -s -m 10 -o /dev/null -w '%{http_code}' https://api-futures.kucoin.com/api/v1/timestamp)"
