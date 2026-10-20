# TB Core — общий движок бота

Форк passivbot. Движок один, используется панелью бота. Проект бота: `C:\bot\turbo`
(режим `bet`, турбо-ставка). До 15.10.2026 ботов было два — `C:\bot\v4` (режим `main`)
и `C:\bot\turbo`; основной удалён, его пресет `ema_wide` и сценарии репетиции лежат
в `C:\bot\extras\v4_research\` и в общем `panel/presets.json` (mode=main). История
решений и исследований — `docs/decisions_log.md`; справка — `docs/` и `docs/ai/`.

## Известная поломка

`src/passivbot_cli/` (консольная команда `passivbot`, entry point в setup.py)
удалён при пересборке. Восстановить с сервера: `/opt/tradebot/<app>/...`
(`passivbot_cli/`, 2 файла). До восстановления `pip install -e .` и запуск
`passivbot ...` не работают; панели стартуют бота через `venv/bin/passivbot`.

## Окружение

venv Python 3.12 в корне каждого проекта бота (`venv/`), Rust MSVC,
`~/.cargo/bin` в PATH, `market_settings_sources` только в комбинированном
режиме. Сборка Rust-модуля — `rustbuild.sh`; команды — `docs/ai/runbooks/commands.md`.

## Локальный патч против апстрима passivbot

`passivbot-rust/src/strategies/ema_anchor.rs`: `cap_entry_qty_by_wel` —
EMA Anchor entries ограничены так, чтобы позиция WE ≤ WEL×(1+allowance)×threshold
при `bot.<side>.risk.position_exposure_enforcer_enabled` (апстрим применял ключ
только к trailing-стратегиям). Пресет turbo_ema5 включает его (threshold 1.0).

## Панели (один код, два режима)

`tb4/panel/`: `proc.py` (запуск/остановка TB Core, одинаков для Windows/Linux)
и `coin_guard.py` (делистинг + hot-фильтр) байт-в-байт одинаковы в обоих ботах;
`server.py` различается:

- **v4** (порт 8770, main): постоянная торговля. `btc_watcher`/`btc_guard_step` —
  обвал BTC (KuCoin 1h, −5%/12h) → стоп + forced panic + пауза до следующего
  00:00 UTC с возобновлением. `run_paper_guarded` для paper.
  Пресеты: `ema_wide` (EMA Anchor 10×8, HSL 35% coin, red 1.0 — основной),
  `ema_btc` (7×3.5, осторожный). STATE_LOCK сериализует переходы.
- **turbo** (порт 8771, bet): ставки на месяц. `bet_watcher`/`bet_step` —
  equity ≥ start×2 → `lock_profit` (panic), equity ≤ start×stop_mult → стоп;
  `btc_pause` внутри ставки (btc_resume), BET_LOCK (RLock) на все переходы,
  crash-loop guard (3×/час), delisting guard, hot-coin filter (hot_ratio 3),
  `run_paper_bets` — ставки подряд в paper.
  Пресет: `turbo_ema5` (EMA Anchor 7×9, TWEL 9, HSL 35% always, tuned params).

База пресетов — `tb4/panel/tuned/ema_anchor.json` (frozen копия
`engine/configs/examples/ema_anchor.json`). Оба бота: KuCoin Futures, 84 монеты
(`tb4/kucoin_coins.json` + `kucoin_min_cost.json`, синхронизированы;
обновление — `tb4/kucoin_refresh.py`).

## Деплой (Selectel YOUR_SERVER_IP, Ubuntu 24.04, 1 vCPU / 2 GB + 2 GB swap)

- Services `tradebot-v4` (:8770) и `tradebot-turbo` (:8771): user `tradebot`,
  /opt/tradebot/<app>, `TB_LITE=1` (no backtests/paper), `KillMode=process`;
  панели биндят 127.0.0.1 only.
- Доступ: `v4/deploy/tunnel.bat` (8780→8770, 8781→8771). SSH-ключ
  `~/.ssh/tradebot_ed25519`; root key-only, ufw + fail2ban.
- Обновление: `v4/deploy/deploy.ps1` (tar → scp → server_install.sh →
  server_services.sh; `-SkipInstall` — без venv/Rust). Здоровье:
  `cmd /c "ssh ... bash -s < v4/deploy/server_check.sh"`.
- Live-бот переживает рестарт панели: LIVE_STATE `want_running` +
  pid/create_time (`proc.py`); `live_resume` — не чаще 3×/час, потом
  `resume_gave_up` (алерт + баннер; ручной старт снимает).
- Отчёты: `v4/deploy/report.py` (cron: hourly sample → equity.csv + alerts.log;
  07:05 MSK daily → latest.txt + чистка логов). TZ сервера = Europe/Moscow.
- Telegram: отдельный сервис `tradebot-telegram` (папка `C:\bot\telegram`),
  стоп/lock из Telegram с 60-секундным подтверждением.
- Память (2 GB): панели лениво импортируют pandas; `LIVE_LIGHT` в build_cfg
  (max_memory_candles_per_symbol 20000, 1m only; monitor snapshot 5 s,
  256 MB cap); child MALLOC_ARENA_MAX=2; zram через `server_memory.sh`;
  OOM-порядок: telegram 500 → панели 300 → passivbot 0.

## Симуляция без биржи

`v4/deploy/sim/`: `make_replays.py` (реальный бэктест с build_cfg панели →
replay json), `sim_passivbot.py` (фальшивый `passivbot live` из replay),
`sim_panel.py` (та же панель, свои порты, вотчеры ×60), `rehearsal.py`
(на сервере, /tmp/tb_sim: копии обеих панелей + dry-run Telegram,
сценарии-дриллы → transcript/results). Сценарии 1–13 покрывают ставки,
BTC-паузы, ребейзы, делистинг.

## Что где

| Задача | Где |
|---|---|
| текущая карта проекта | `C:\bot\ai\AI_MAP.md` |
| правила работы ИИ | `C:\bot\ai\AI_RULES.md` |
| история решений | `docs/decisions_log.md` |
| архитектура, контракты, runbooks | `docs/ai/` |
| research-архив (скрипты и результаты) | `C:\bot\extras\` |
| архив старой документации | `C:\bot\extras\archive\` |
