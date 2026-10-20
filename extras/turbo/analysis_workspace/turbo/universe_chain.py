"""EMA Anchor 10 монет × экспозиция 8 (основной бот) на расширенном наборе монет — «как в жизни»: без остановок, выход
при обвале BTC (закрытие часа на 5% ниже максимума за 12 ч) и новый запуск со следующего дня, когда обвал кончился
(как tb4/main_strategy.py в проекте v4). Периоды: II пол. 2025 (обвал 10.10), I пол. 2026, II пол. 2026.

    venv\\Scripts\\python turbo\\universe_chain.py [--workers 6]
Итог: turbo/results/universe_chain.json и таблица.
"""
import argparse
import json
import math
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_test as UT  # noqa: E402

PERIODS = {"II пол. 2025 (обвал 10.10)": ("2025-07-01", "2026-01-01"), "I пол. 2026": ("2026-01-01", "2026-07-01"),
           "II пол. 2026": ("2026-07-01", "2026-09-30")}
COST = 0.008                                   # выход: 0,1% капитала на единицу экспозиции (8)
HSL = {"bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.35, "bot.long.hsl.restart_after_red_policy": "always",
       "bot.long.hsl.cooldown_minutes_after_red": 4320.0, "live.hsl_signal_mode": "unified"}
OVER = {**UT.OVER, **HSL, "backtest.balance_sample_divider": 1}


def chain(uname, coins, period, btc):
    start, end = PERIODS[period]
    d, cap, pieces, exits = pd.Timestamp(start), 1.0, [], []
    t_end = pd.Timestamp(end)
    while d < t_end - pd.Timedelta(days=1):
        if DR.crash_minutes(btc, d.strftime("%Y-%m-%d"), 1, 12, 0.05)[0]:
            d += pd.Timedelta(days=1)
            continue
        tag = f"uc_{abs(hash((uname, period))) % 10**7}_{d:%Y%m%d}"
        res = DS.backtest_curve(dict(OVER), d.strftime("%Y-%m-%d"), end, tag, base=UT.EM.EMA, coins=coins)
        if isinstance(res, str):
            return res
        rel, rd = res
        shutil.rmtree(rd, ignore_errors=True)
        idx = d + pd.to_timedelta(np.arange(len(rel)), unit="min")
        fl = DR.crash_minutes(btc, d.strftime("%Y-%m-%d"), len(rel), 12, 0.05)
        hit = np.flatnonzero(fl)
        if not hit.size:
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        i = int(hit[0])
        out = cap * float(rel[i:i + 3].min()) * (1 - COST)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        exits.append(str(idx[i]))
        cap = out
        d = (idx[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([cap], index=[d]))
    curve = pd.concat(pieces)
    curve = curve[~curve.index.duplicated(keep="last")].sort_index()
    h = curve.resample("1h").last().ffill()
    m30 = (h.shift(-30 * 24) / h).dropna()
    days = (h.index[-1] - h.index[0]).total_seconds() / 86400
    ret = h.iloc[-1] / h.iloc[0] - 1
    return {"ret": round(ret * 100, 1), "month": round(((1 + ret) ** (30 / days) - 1) * 100, 1),
            "dd": round(float((1 - h / h.cummax()).max()) * 100, 1), "worst30": round((float(m30.min()) - 1) * 100, 1),
            "exits": exits}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    U = UT.universes()
    btc = RR.btc_hourly()
    jobs = [(u, p) for u in U for p in PERIODS]
    with ThreadPoolExecutor(a.workers) as ex:
        out = dict(zip(jobs, ex.map(lambda j: chain(j[0], U[j[0]], j[1], btc), jobs)))
    rows = []
    for u in U:
        row = {"name": u, "coins": len(U[u])}
        total = 1.0
        for p in PERIODS:
            r = out[(u, p)]
            row[p] = {"error": r[:200]} if isinstance(r, str) else r
            if not isinstance(r, str):
                total *= 1 + r["ret"] / 100
        row["total"] = round((total - 1) * 100)
        rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "universe_chain.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("EMA Anchor 10 монет × 8, без остановок, выход по BTC с запуском со следующего дня\n")
    for r in rows:
        print(f"{r['name']:<18} ({r['coins']:>3} монет): 15 мес. подряд {r['total']:+d}%")
        for p in PERIODS:
            x = r[p]
            print(f"   {p:<28} " + (x["error"][:150] if "error" in x else
                  f"{x['ret']:+7.1f}% ({x['month']:+5.1f}%/мес.), просадка {x['dd']:5.1f}%, худшие 30 дн. {x['worst30']:+6.1f}%, выходов {len(x['exits'])}"))


if __name__ == "__main__":
    main()
