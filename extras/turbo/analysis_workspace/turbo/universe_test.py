"""EMA Anchor (10 монет × экспозиция 8, как основной бот) на расширенном наборе монет KuCoin (01.10.2026).

Наборы: нынешние 35 (tb4/kucoin_coins.json), все бессрочные USDT-фьючерсы KuCoin с оборотом ≥ 10 млн $ и ≥ 5 млн $
за сутки (turbo/results/kucoin_contracts_2026-10-01.json), которые есть на Binance (свечи для бэктеста); токены акций,
золота и стейблкоины исключены. Монеты моложе года стратегия сама не берёт (minimum_coin_age_days 365 в её настройках).
Окна — как turbo/param_search.py: 30-дневный прогон со $200, старт каждые 10 дней в I пол. 2026 (отбор), II пол. 2026
(проверка) и на обвале 10.10.2025.

    venv\\Scripts\\python turbo\\universe_test.py [--workers 6]
Итог: turbo/results/universe_test.json; кривые — turbo/results/universe_test/<набор>/<старт>.npy.
"""
import argparse
import json
import math
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import engine_month as EM  # noqa: E402
import paper_window as PW  # noqa: E402
import param_search as PS  # noqa: E402
import risk_rules as RR  # noqa: E402

OUT = os.path.join(HERE, "results", "universe_test")
NOT_CRYPTO = {"PAXG", "XAUT", "SNDK", "MU", "SKHYNIX", "SOXL", "NVDA", "TSLA", "AAPL", "MSTR", "COIN", "HOOD", "QQQ", "SPY",
              "USDC", "FDUSD", "USDE", "KCS"}
OVER = {PS.L + "total_wallet_exposure_limit": 8.0, PS.L + "n_positions": 10.0, **EM.NOSHORT}


def universes():
    rows = json.load(open(os.path.join(HERE, "results", "kucoin_contracts_2026-10-01.json")))
    ok = [r for r in rows if r["binance"] and r["coin"] not in NOT_CRYPTO]
    return {"нынешние 35": DS.MC.default_coins(),
            "оборот ≥ 10 млн $": [r["coin"] for r in ok if r["turnover"] >= 10e6],
            "оборот ≥ 5 млн $": [r["coin"] for r in ok if r["turnover"] >= 5e6]}


def run(uname, coins, start):
    key = {"нынешние 35": "u35", "оборот ≥ 10 млн $": "u10m", "оборот ≥ 5 млн $": "u5m"}[uname]
    path = os.path.join(OUT, key, f"{start}.npy")
    if os.path.exists(path):
        return np.load(path)
    end = (datetime.fromisoformat(start) + timedelta(days=30)).strftime("%Y-%m-%d")
    res = DS.backtest_curve({**PW.ENGINE3, **OVER}, start, end, f"ut_{key}_{start}", base=EM.EMA, coins=coins)
    if isinstance(res, str):
        print("  ошибка", uname, start, res[:200], flush=True)
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, res[0])
    shutil.rmtree(res[1], ignore_errors=True)
    return res[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    U = universes()
    for n, c in U.items():
        print(f"{n}: {len(c)} монет", flush=True)
    jobs = [(u, s) for u in U for ss in PS.SETS.values() for s in ss]
    # сначала по одному прогону на набор — passivbot докачивает свечи новых монет, параллельная докачка мешает сама себе
    for u in U:
        run(u, U[u], PS.SETS["I пол. 2026 (отбор)"][0])
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(lambda j: run(j[0], U[j[0]], j[1]), jobs)))
    btc = RR.btc_hourly()
    rows = []
    for u in U:
        row = {"name": u, "coins": len(U[u])}
        for w, ss in PS.SETS.items():
            ok = [(s, res[(u, s)]) for s in ss if res[(u, s)] is not None]
            m0 = [PS.month(r, s, btc, False) for s, r in ok]
            m1 = [PS.month(r, s, btc, True, cost=0.008) for s, r in ok]
            row[w] = {"geo": PS.geo(m0), "geo_exit": PS.geo(m1), "worst": round(min(m0) * 100 - 100) if m0 else None, "n": len(ok)}
        rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "universe_test.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    W = list(PS.SETS)
    print("\nEMA Anchor 10 монет × 8; типичный месяц без защиты (с выходом по BTC и без торговли до конца месяца)\n")
    for r in rows:
        x, y, z = r[W[0]], r[W[1]], r[W[2]]
        print(f"{r['name']:<18} ({r['coins']:>3} монет) | I: {x['geo']:+5.1f}% ({x['geo_exit']:+5.1f}%), худш. {x['worst']:+4d}% | "
              f"II: {y['geo']:+5.1f}% ({y['geo_exit']:+5.1f}%) | обвал 2025: {z['geo']:+5.1f}% ({z['geo_exit']:+5.1f}%)")


if __name__ == "__main__":
    main()
