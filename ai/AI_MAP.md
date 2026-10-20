# КАРТА ПРОЕКТА — для работы ИИ с кодом

Актуально на 2026-10-15. Обновляется вручную; цифры строк — из `wc -l`.

## Устройство проекта

**Движок один** — `engine/` (внутреннее имя TB Core): форк passivbot.
Единственная точка входа — CLI `passivbot` (`passivbot_cli.main:console_main`
из setup.py). Модуль `engine/src/passivbot_cli/` был удалён при пересборке и
восстановлен с сервера 2026-10-15; рядом с ним лежит `engine/src/main.py` —
тонкий загрузчик, который и запускает живой бот. Локальная копия CLI урезана до
трёх команд, полная таблица — только на сервере (см. AI_RULES.md).

**Бот один.** 15.10.2026 основной бот удалён, остался turbo (`C:\bot\turbo`,
режим `bet`, порт 8771, профиль ключей `kucoin_turbo`, пресет `turbo_ema5`).
Общий код панели — `panel/`, он же поставлен в бота через `install_panel.py`.

| Проект | Что это | Размер |
|---|---|---|
| `turbo/` | бот: `tb4/panel/`, `configs/`, справочники монет | 43 файла |
| `panel/` | общий исходник панели + `profile.turbo.json` | 8 файлов |
| `deploy/` | выкладка на сервер, systemd, отчёты, стенд `sim/` | 22 файла |
| `telegram/` | телеграм-бот | 3 файла |
| `engine/` | ядро TB Core (движок) | 273 файла |
| `extras/` | архив: исследовательские скрипты, `v4_research/`, старые версии панелей | 1 271 файл |

Бота раньше было два, и режимы `main`/`bet` различались тем, какой файл состояния
переводится в переход при обвале BTC (`live.json` против `bet.json`). Механизм
режимов в коде сохранён целиком — вернуть второго бота можно готовым профилем
`panel/profile.v4.json` и пресетом `ema_wide` из `panel/presets.json`.

Общий код панели обслуживает обоих по единому принципу: личность (порт, профиль
ключей, пресет по умолчанию) задаёт `profile.json`, а не код.

```python
PROFILE = _read_profile()      # panel/profile.json — ставится при установке бота
MODE    = PROFILE["mode"]      # "main" | "bet"
PORT    = PROFILE["port"]; USER = PROFILE["user"]
```

Режим переключает: набор правил пресета (`defaults` против `bet_defaults`),
разбор сохранённых настроек, поток-наблюдатель (`bet_watcher` против
`btc_watcher`), ветку бумажной торговли, маршруты `/api/bet/*` и элементы
интерфейса. Одна лента на оба файла состояния: `STATE_LOCK`, а `BET_LOCK` — её
псевдоним, иначе соседние операции над `bet.json` и `live.json` гонялись бы.

На сервере раскладка та же, один бот:

```
/opt/tradebot/turbo      1,7 ГБ   бот: панель, конфиги, venv, ядро
/opt/tradebot/deploy     708 КБ   выкладка, systemd, отчёты, стенд sim/
/opt/tradebot/telegram   164 КБ   телеграм-бот + config.json/state.json
/opt/tradebot/reports    408 КБ   отчёты и срезы live_watch
```

Службы: `tradebot-turbo` (панель), `tradebot-telegram` (уведомления).
`tradebot-v4` снята 15.10.2026 вместе с ботом.

Тонкость, на которой уже ломались: `telegram_bot.py` делает `import report`,
а `report.py` лежит в `deploy/`. Поэтому в unit телеграма обязателен
`Environment=PYTHONPATH=/opt/tradebot/deploy` — без него служба падает
с `ModuleNotFoundError: No module named 'report'` и уходит в цикл рестартов.

Крон (4 задачи) ходит в `/opt/tradebot/deploy/`: `live_watch.py` каждые 5 минут,
`overnight_report.py` в 05:30, `report.py sample` в час, `report.py daily` в 07:05.

Установка/проверка: `python C:\bot\install_panel.py check | turbo`.
Прежние копии складываются в `panel_backup_<метка>/`, `data/` не трогается.

**Деплой и симуляция** — `v4/deploy/`: deploy.ps1 (tar → scp → install/services),
server_*.sh, report.py, validate_live.py, sim/ (rehearsal.py, sim_panel.py,
sim_passivbot.py, make_replays.py — сухие прогоны панелей без биржи).

**Не код:** `telegram/` (телеграм-бот, не попадает в контекст торговых задач),
`extras/` (research-архив: analysis_workspace с исследовательскими скриптами
и результатами, старые версии панелей, апстрим-дополнения),
`extras/archive/` (заархивированная история: docs/plans, release notes, sim/out).

## Состав движка (engine/src, 132 .py, ~130K строк)

| Раздел | Файлов | Строк |
|---|---:|---:|
| корень src | 49 | 82 682 |
| `live/` | 23 | 16 450 |
| `exchanges/` | 19 | 12 344 |
| `config/` | 37 | 8 680 |
| `tools/` | 3 | 1 243 |
| `optimization/` | 9 | 1 033 (бэкенды удалены — заглушка) |

Rust: `engine/passivbot-rust` — 32 модуля, ~52K строк; в Python ходят через
`rust_utils.py`. Локальный патч против апстрима: `strategies/ema_anchor.rs`
`cap_entry_qty_by_wel` (ограничение входа по exposure).

## Самые тяжёлые файлы — не читают целиком, только grep + окно 100–200 строк

| Файл | Строк |
|---|---:|
| `engine/src/passivbot.py` | 22 917 |
| `engine/src/candlestick_manager.py` | 10 653 |
| `engine/src/fill_events_manager.py` | 8 626 |
| `engine/src/passivbot_hsl.py` | 7 778 |
| `engine/src/hlcv_preparation.py` | 6 234 |
| `engine/src/live/event_emitters.py` | 5 404 |
| `engine/src/live/event_bus.py` | 4 636 |
| `engine/src/backtest.py` | 3 420 |
| `turbo/tb4/panel/server.py` | 1 611 |
| `v4/deploy/sim/rehearsal.py` | 789 |

## Что где искать — частые вопросы

| Вопрос | Файл | Символ |
|---|---|---|
| сторона позиции по ордеру | `engine/src/exchanges/kucoin.py` | `determine_pos_side`, `_get_position_side_for_order` |
| что бот видит по позициям сейчас | `engine/src/passivbot.py` | `get_current_n_positions` |
| порог остановки по убытку | `engine/src/passivbot.py` | `_live_max_realized_loss_pct` |
| лог сделок и события | `engine/src/live/event_emitters.py` | — |
| сборка конфига из пресета | `turbo/tb4/panel/server.py` | `build_cfg` |
| настройки пресета | `turbo/tb4/panel/server.py` | `overrides_for` |
| bet-механика (ставки) | `turbo/tb4/panel/server.py` | `bet_step`, `bet_watcher`, `lock_profit` |
| BTC-пауза в main-режиме | `v4/tb4/panel/server.py` | `btc_guard_step`, `btc_watcher` |
| проверка живости процесса | `turbo/tb4/panel/server.py` | `_pid_confirmed_dead` |
| запуск/остановка процесса | `*/tb4/panel/proc.py` | — |
| отсев монет (делистинг, hot) | `*/tb4/panel/coin_guard.py` | — |
| обновление списка монет | `*/tb4/kucoin_refresh.py` | — |
| бюджет запросов свечей | `engine/src/candlestick_manager.py` | — |
| команды запуска/сборки | `engine/docs/ai/runbooks/commands.md` | — |

Символы ищутся grep по `ai/AI_SYMBOLS.txt` (формат `путь:строка  вид  имя`).
Индекс устареет после любого рефакторинга — перегенерация: `python ai/gen_symbols.py`.

## Документация движка

Справка — `engine/docs/` (68 файлов; ядро: `ai/` — архитектура и контракты,
корень — руководства: configuration, backtesting, optimizing, live, monitor).
История решений бота — `engine/docs/decisions_log.md`.
Всё остальное (планы, release notes, логи симуляций) — `extras/archive/`.
