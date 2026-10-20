"""Собирает конфиг passivbot для тестов под KuCoin Futures.

Свечи — из архивов Binance (полные минутные данные; у KuCoin в минутках REST-API пропуски),
правила контрактов (шаг цены, размер лота, минимальный ордер) — KuCoin, комиссии — KuCoin VIP0.

    python tb4/make_config.py OUT.json --start 2026-04-22 --end 2026-09-29 [--set путь=значение ...]
    пример: --set bot.long.risk.total_wallet_exposure_limit=3 --set bot.short.risk.total_wallet_exposure_limit=1
"""
import argparse
import copy
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE = os.path.join(ROOT, "configs", "examples", "default_trailing_martingale_long.json")
KUCOIN_MAKER, KUCOIN_TAKER = 0.0002, 0.0006
MIN_TURNOVER = 5_000_000


def set_path(cfg, path, value):
    keys = path.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def default_coins():
    """Монеты с нормальной ликвидностью на KuCoin Futures (оборот от MIN_TURNOVER за сутки)."""
    with open(os.path.join(HERE, "kucoin_coins.json"), encoding="utf-8") as f:
        ku = json.load(f)
    return [c for c in ku["coins"] if ku["kucoin"][c]["turnover24h"] >= MIN_TURNOVER]


def build(start, end, balance=1000.0, base_path=BASE, overrides=None, coins=None):
    with open(base_path, encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(HERE, "kucoin_coins.json"), encoding="utf-8") as f:
        ku = json.load(f)
    # по умолчанию — монеты с нормальной ликвидностью на KuCoin Futures (оборот от 5 млн $ за сутки);
    # пустой список — ошибка, а не «взять все монеты»: молчаливая подмена скрывает потерю фильтра монет
    if coins is None:
        coins = [c for c in ku["coins"] if ku["kucoin"][c]["turnover24h"] >= MIN_TURNOVER]
    else:
        coins = list(coins)
        if not coins:
            raise ValueError("coins: пустой список монет — передайте None или непустой список")
    bt = cfg["backtest"]
    # market_settings_sources действует только в комбинированном режиме (бирж > 1):
    # свечи принудительно с Binance (coin_sources), правила контрактов — с KuCoin
    bt.update({"exchanges": ["binance", "kucoin"], "start_date": start, "end_date": end, "starting_balance": balance,
               "maker_fee_override": KUCOIN_MAKER, "taker_fee_override": KUCOIN_TAKER, "suite_enabled": False,
               "coin_sources": {c: "binance" for c in coins}, "market_settings_sources": {c: "kucoin" for c in coins},
               "volume_normalization": False, "base_dir": "backtests/tb4"})
    cfg["live"]["approved_coins"] = {"long": list(coins), "short": list(coins)}
    cfg["live"]["user"] = "kucoin_01"
    for path, value in (overrides or {}).items():
        set_path(cfg, path, value)
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--balance", type=float, default=1000.0)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--coins", help="через запятую, по умолчанию все монеты из kucoin_coins.json")
    ap.add_argument("--set", action="append", default=[], metavar="ПУТЬ=ЗНАЧЕНИЕ")
    a = ap.parse_args()
    over = {}
    for item in a.set:
        k, v = item.split("=", 1)
        over[k] = parse_value(v)
    coins = [c.strip().upper() for c in a.coins.split(",")] if a.coins else None
    cfg = build(a.start, a.end, a.balance, a.base, over, coins)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("записан", a.out)


if __name__ == "__main__":
    main()
