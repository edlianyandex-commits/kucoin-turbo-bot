#!/usr/bin/env bash
# Базовая настройка сервера для ботов (Ubuntu 24.04). Повторный запуск безопасен.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== swap 2 ГБ"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
# сжатая подкачка zram и настройки памяти — deploy/server_memory.sh
swapon --show

echo "== часовой пояс: Москва (время в панелях, отчётах и расписании — МСК)"
timedatectl set-timezone Europe/Moscow

echo "== пакеты"
apt-get update -qq
apt-get -y -qq upgrade
apt-get -y -qq install build-essential pkg-config libssl-dev python3-venv python3-dev git curl ufw fail2ban unzip >/dev/null
echo "пакеты установлены"

echo "== пользователь tradebot"
id tradebot >/dev/null 2>&1 || useradd -m -s /bin/bash tradebot
mkdir -p /home/tradebot/.ssh
cp /root/.ssh/authorized_keys /home/tradebot/.ssh/authorized_keys
chown -R tradebot:tradebot /home/tradebot/.ssh && chmod 700 /home/tradebot/.ssh && chmod 600 /home/tradebot/.ssh/authorized_keys
mkdir -p /opt/tradebot && chown tradebot:tradebot /opt/tradebot

echo "== файрвол: снаружи только SSH (панели слушают только 127.0.0.1)"
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status | head -5

echo "== SSH: только по ключу"
cat > /etc/ssh/sshd_config.d/10-keys-only.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t && systemctl reload ssh
echo "вход по паролю отключён"

systemctl enable --now fail2ban >/dev/null 2>&1 || true
echo "fail2ban: $(systemctl is-active fail2ban)"
echo "ГОТОВО"
