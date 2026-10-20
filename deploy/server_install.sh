#!/usr/bin/env bash
# Установка/обновление ботов на сервере (запускается от пользователя tradebot).
# Ожидает /tmp/tb_turbo.tar.gz и общую обвязку /tmp/tb_deploy.tar.gz, /tmp/tb_telegram.tar.gz.
# Данные (ключи, настройки панели, журналы, свечи) не трогает.
set -euo pipefail
export PATH="$HOME/.cargo/bin:$PATH"
cd /opt/tradebot

if ! command -v rustup >/dev/null 2>&1; then
  echo "== Rust 1.90.0 (для сборки ядра TB Core)"
  curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.90.0 --no-modify-path >/dev/null
fi
rustc --version

for app in turbo; do
  [ -f "/tmp/tb_$app.tar.gz" ] || { echo "нет /tmp/tb_$app.tar.gz — пропускаю $app"; continue; }
  echo "== $app: код"
  mkdir -p "$app"
  tar -xzf "/tmp/tb_$app.tar.gz" -C "$app"
  cd "$app"
  if [ ! -x venv/bin/python ]; then
    echo "== $app: окружение Python"
    python3 -m venv venv
    venv/bin/pip install -q -U pip wheel
  fi
  echo "== $app: зависимости"
  venv/bin/pip install -q -r requirements-live.txt -r requirements-rust.txt
  if [ ! -f tb4/kucoin_coins.json ] || [ ! -f tb4/kucoin_min_cost.json ]; then
    # выкладка справочник монет не возит (deploy.ps1) — на сервере панель обновляет его сама раз в месяц
    echo "== $app: справочник монет KuCoin (первый раз; дальше — сама панель, 1-го числа)"
    venv/bin/python tb4/kucoin_refresh.py
  fi
  echo "== $app: сборка ядра TB Core (на 1 ядре это 5–15 минут)"
  CARGO_BUILD_JOBS=1 venv/bin/pip install -q -e .
  venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "src")
from rust_utils import check_and_maybe_compile
check_and_maybe_compile()
import passivbot_rust
print("ядро TB Core:", passivbot_rust.__file__)
PY
  cd ..
done
# Общая обвязка: install-скрипты, отчёты, стенд и телеграм--bot. Лежит рядом с ботами, а не внутри
# папки какого-то из них, поэтому ставится отдельными архивами в /opt/tradebot/<имя>.
# config.json и state.json телеграма в архив не входят — на сервере свои.
for shared in deploy telegram; do
  [ -f "/tmp/tb_$shared.tar.gz" ] || { echo "нет /tmp/tb_$shared.tar.gz — пропускаю $shared"; continue; }
  echo "== $shared: общая обвязка"
  mkdir -p "$shared"
  tar -xzf "/tmp/tb_$shared.tar.gz" -C "$shared"
done
echo "ГОТОВО"
