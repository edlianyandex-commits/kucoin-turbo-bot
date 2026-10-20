# TB — trading bot for KuCoin Futures

**English** · [Русский](README.ru.md)

A trading bot for KuCoin Futures: a web control panel, a monthly "bet" turbo
mode, BTC-crash protection, a delisting filter and a Telegram bot for stopping
and locking in profit. The trading core is TB Core (`engine/`), a fork of
[passivbot](https://github.com/enarjord/passivbot) (public domain, Unlicense).

## Layout

| Folder | What it is |
|---|---|
| `engine/` | TB Core: Python + Rust module (backtest, optimization, live trading) |
| `turbo/` | the bot: `tb4/panel/` panel, configs, coin lists |
| `panel/` | shared panel source and presets |
| `telegram/` | Telegram bot (stop / lock profit with confirmation) |
| `deploy/` | server deployment (Ubuntu + systemd), reports, `sim/` simulator |
| `ai/` | project map and rules for AI-assisted work on the code |
| `extras/` | research and archive |

Most comments and internal docs are in Russian.

## Installation

1. Python 3.12, Rust (stable), Git.
2. Core: see [engine/docs/installation.md](engine/docs/installation.md); the Rust
   module is built with `engine/rustbuild.sh`.
3. Exchange keys go into `api-keys.json` (listed in `.gitignore`, never committed).
4. Server: replace `YOUR_SERVER_IP` in the `deploy/` scripts and
   `telegram/telegram_setup.bat` with your address, then run `deploy/deploy.ps1`.

## Referral link

Signing up on KuCoin with this link supports the project:
https://www.kucoin.com/r/rf/CXEENJJA

## Disclaimer

Trading futures with leverage carries a high risk of losing your funds. This
software is provided as is, without any warranty; use it at your own risk.

## License

The core is inherited from passivbot and is released into the public domain
(Unlicense); see the License section in [engine/README.md](engine/README.md).
