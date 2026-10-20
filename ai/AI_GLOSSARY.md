# ГЛОССАРИЙ — термины этого проекта

Сгенерировано из имён, реально встречающихся в коде.

| Термин | Где искать |
|---|---|
| `EMA anchor` | стратегия входа, `engine/passivbot-rust/src/strategies/ema_anchor.rs`, `engine/src/passivbot_hsl.py` |
| `TWEL` | порог входа стратегии; значение 9.0 в пресете `turbo_ema5` — осознанное |
| `fill` | исполнение сделки, `engine/src/fill_events_manager.py` |
| `exposure_x` | экспозиция, во сколько раз превышена, `live_turbo.csv` |
| `realized loss` | реализованный убыток, гейт остановки |
| `preset` / `presets.json` | именованные наборы настроек, источник для панели |
| `build_cfg` | сборка рабочего конфига из пресета, `turbo/tb4/panel/server.py` |
| `overrides_for` | выбор блока overrides пресета |
| `coin_guard` | отсев монет по ликвидности, `turbo/tb4/panel/coin_guard.py` |
| `universe` | множество монет для торговли |
| `entry` / `close` | типы ордеров стратегии |
| `be` / `pb_order_type` | тип заявки в записях сделок |
| `n_trades` | накопительный счётчик сделок |
| `guard_fires` | срабатывания защит |

## Прочие часто встречающиеся имена (топ по частоте)

`None`, `Any`, `False`, `True`, `ValueError`, `Optional`, `Dict`, `Path`, `Exception`, `List`, `Counter`, `EventTypes`, `TypeError`, `Mapping`, `Return`, `Rust`, `Passivbot`, `Sequence`, `HSL`, `Tuple`, `GPU`, `Iterable`, `RuntimeError`, `EMA`, `FatalBotException`, `The`, `BTC`, `PnL`, `CCXT`, `DataFrame`, `EventTags`, `KeyError`, `Bitunix`, `JSON`, `MPS`, `OSError`, `USDT`, `ArgumentParser`, `ReasonCodes`, `API`, `Pareto`, `KuCoin`, `OHLCV`, `Returns`, `Use`, `BaseException`, `EventRoute`, `Callable`, `INFO`, `FileNotFoundError`, `UTC`, `Output`, `This`, `Keep`, `Input`, `Backtest`, `Fetch`, `Hyperliquid`, `Only`, `RED`, `OverflowError`, `Bound`, `LiveEvent`, `Check`, `Runtime`, `ParetoCandidate`, `USDC`, `NetworkError`, `CommandSpec`, `Args`, `CancelledError`, `HLCV`, `SystemExit`, `Emit`, `Build`, `CandlestickManager`, `REST`, `DEBUG`, `WEEX`, `HIP`, `Coin`, `Log`, `Default`, `Convert`, `May`, `Lighter`, `Preserve`, `FillEvent`, `For`, `NOT`