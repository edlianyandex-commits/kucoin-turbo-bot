#!/usr/bin/env bash
# Память сервера (запускается от root). Повторный запуск безопасен.
# zram — сжатая подкачка прямо в памяти: страницы Python сжимаются в 2–3 раза и возвращаются за микросекунды,
# поэтому при нехватке памяти сначала работает она, а файл подкачки на диске — только запасным.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== модуль ядра zram (в Ubuntu он в пакете linux-modules-extra, ~110 МБ на диске)"
if ! modprobe -n zram 2>/dev/null; then
  apt-get -y -qq -o DPkg::Lock::Timeout=120 install "linux-modules-extra-$(uname -r)" >/dev/null
fi
mkdir -p /etc/systemd/system/zramswap.service.d
cat > /etc/systemd/system/zramswap.service.d/10-modules.conf <<'EOF'
# после обновления ядра модуль zram нужен и для нового ядра — поставить его при загрузке, если его нет
[Unit]
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/bin/sh -c 'modprobe -n zram 2>/dev/null || apt-get -y -qq -o DPkg::Lock::Timeout=300 install linux-modules-extra-$$(uname -r)'
TimeoutStartSec=600
EOF
systemctl daemon-reload

echo "== zram (сжатая подкачка в памяти)"
dpkg -s zram-tools >/dev/null 2>&1 || apt-get -y -qq -o DPkg::Lock::Timeout=120 install zram-tools >/dev/null
cat > /etc/default/zramswap <<'EOF'
# сжатие lz4 — быстрое, для 1 ядра; до половины памяти; приоритет выше файла подкачки на диске
ALGO=lz4
PERCENT=50
PRIORITY=100
EOF
systemctl enable zramswap >/dev/null 2>&1
systemctl restart zramswap

echo "== ядро: подкачка сначала в zram, без упреждающего чтения"
rm -f /etc/sysctl.d/99-swappiness.conf
cat > /etc/sysctl.d/99-tradebot-memory.conf <<'EOF'
vm.swappiness=60
vm.page-cluster=0
EOF
sysctl -q --system

echo "== журнал systemd не больше 100 МБ"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\n' > /etc/systemd/journald.conf.d/10-size.conf
systemctl restart systemd-journald

swapon --show
free -m
