"""Обновить справочник монет KuCoin Futures для панели и конфигов: tb4/kucoin_coins.json и tb4/kucoin_min_cost.json.

Берутся бессрочные USDT-фьючерсы KuCoin (публичный API) с оборотом за сутки ≥ --min-turnover, которые есть на Binance USDⓈ-M
(свечи для бэктеста и бумаги берутся с Binance — без них проверка и бумага не работают); токены акций, золота, стейблкоины
и KCS исключаются. Минимальный ордер — 1 контракт: multiplier × цена. Торгует бот монетами с оборотом ≥ 5 млн $
(make_config.MIN_TURNOVER) — в справочник попадают и чуть менее ликвидные, панель их показывает.
Расширение с 35 до 84 монет (оборот ≥ 5 млн $): основной бот +172% → +395% за 15 месяцев при той же просадке
(turbo/universe_chain.py в проекте Turbo, 01.10.2026).

С 02.10.2026 панели обновляют справочник сами раз в месяц (1-го числа после 00:10 UTC; fetch_rows + write) — так же,
как в честной проверке (проект Turbo turbo/turnover_rule.py: список по месяцам по обороту того времени). Файл одинаковый
у основного бота и Turbo.

    venv\\Scripts\\python tb4\\kucoin_refresh.py [--min-turnover 1000000]
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NOT_CRYPTO = {"PAXG", "XAUT", "SNDK", "MU", "SKHYNIX", "SOXL", "NVDA", "TSLA", "AAPL", "MSTR", "COIN", "HOOD", "QQQ", "SPY",
              "USDC", "FDUSD", "USDE", "KCS"}
MIN_TURNOVER = 1_000_000          # в справочник (панель показывает); торгует бот от make_config.MIN_TURNOVER


def fetch_rows(session, min_turnover=MIN_TURNOVER):
    """[(монета, {symbol, multiplier, maxLeverage, turnover24h}, минимальный ордер $)] — по убыванию оборота."""
    ku = session.get("https://api-futures.kucoin.com/api/v1/contracts/active", timeout=30).json()["data"]
    bn = session.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=30).json()["symbols"]
    on_binance = {x["baseAsset"] for x in bn if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT"
                  and x.get("status") == "TRADING"}
    if not ku or not on_binance:
        raise RuntimeError("KuCoin или Binance вернули пустой список контрактов")
    rows = []
    for c in ku:
        if c.get("quoteCurrency") != "USDT" or c.get("isInverse") or c.get("type") != "FFWCSX":
            continue
        coin = "BTC" if c["baseCurrency"] == "XBT" else c["baseCurrency"]
        turnover = float(c.get("turnoverOf24h") or 0)
        if coin in NOT_CRYPTO or coin not in on_binance or turnover < min_turnover:
            continue
        price = float(c.get("markPrice") or c.get("lastTradePrice") or 0)
        rows.append((coin, {"symbol": c["symbol"], "multiplier": float(c["multiplier"]), "maxLeverage": int(c.get("maxLeverage") or 0),
                            "turnover24h": turnover}, float(c["multiplier"]) * price))
    rows.sort(key=lambda r: -r[1]["turnover24h"])
    return rows


def write(rows, here=HERE):
    """Записать справочник (атомарно: сначала .tmp, потом замена)."""
    coins = {"coins": [r[0] for r in rows], "kucoin": {r[0]: r[1] for r in rows}}
    min_cost = {r[0]: round(r[2], 6) for r in rows}
    for name, data in (("kucoin_coins.json", coins), ("kucoin_min_cost.json", min_cost)):
        path = os.path.join(here, name)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)


def main():
    import requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-turnover", type=float, default=MIN_TURNOVER)
    a = ap.parse_args()
    s = requests.Session()
    s.trust_env = False
    rows = fetch_rows(s, a.min_turnover)
    write(rows)
    n5 = sum(1 for r in rows if r[1]["turnover24h"] >= 5_000_000)
    print(f"монет в справочнике: {len(rows)} (оборот ≥ {a.min_turnover / 1e6:g} млн $), торгуемых (≥ 5 млн $): {n5}")


if __name__ == "__main__":
    main()
