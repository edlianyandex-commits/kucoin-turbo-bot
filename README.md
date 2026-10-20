# TB — торговый бот для KuCoin Futures

Бот для фьючерсов KuCoin: веб-панель управления, турбо-режим «ставок» на месяц,
защита от обвала BTC, фильтр делистинга и Telegram-бот для остановки и фиксации
прибыли. Ядро торговли — TB Core (`engine/`), форк [passivbot](https://github.com/enarjord/passivbot)
(public domain, Unlicense).

## Состав

| Папка | Что это |
|---|---|
| `engine/` | ядро TB Core: Python + Rust-модуль (бэктест, оптимизация, live) |
| `turbo/` | бот: панель `tb4/panel/`, конфиги, справочники монет |
| `panel/` | общий исходник панели и пресеты |
| `telegram/` | Telegram-бот (стоп / фиксация с подтверждением) |
| `deploy/` | выкладка на сервер (Ubuntu + systemd), отчёты, симулятор `sim/` |
| `ai/` | карта проекта и правила для работы ИИ с кодом |
| `extras/` | исследования и архив |

## Установка

1. Python 3.12, Rust (stable), Git.
2. Ядро: см. [engine/docs/installation.md](engine/docs/installation.md); Rust-модуль
   собирается скриптом `engine/rustbuild.sh`.
3. Ключи биржи — в `api-keys.json` (файл в `.gitignore`, в репозиторий не попадает).
4. Сервер: укажите свой адрес вместо `YOUR_SERVER_IP` в скриптах `deploy/` и
   `telegram/telegram_setup.bat`, затем `deploy/deploy.ps1`.

## Реферальная ссылка

Регистрация на KuCoin по ссылке поддерживает проект:
https://www.kucoin.com/r/rf/CXEENJJA

## Лицензия

Ядро унаследовано от passivbot и распространяется как public domain (Unlicense),
см. раздел License в [engine/README.md](engine/README.md).
