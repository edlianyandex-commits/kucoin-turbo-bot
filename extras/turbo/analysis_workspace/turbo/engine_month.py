"""Сколько движок даёт за месяц, если бот работает без остановок и вся прибыль в обороте (passivbot сам считает
размер позиций от текущего баланса). Цель пользователя — +100% за месяц без «ставки ×2».

Варианты: движок 3.0 (trailing martingale, 3 монеты, стоп passivbot 40% «без перезапуска», «только тейки» с 20%)
с разной экспозицией и движок EMA Anchor (configs/examples/ema_anchor.json, только лонг). Месяц = 30-дневный прогон
passivbot со $200, капитал поминутно, старт каждые 3 дня: I пол. 01.01…31.05, II пол. 01.07…31.08 (2026).
Правила поверх прогона (без перезапуска внутри месяца): нет; стоп −30% от старта; стоп −30% от максимума.

    venv\\Scripts\\python turbo\\engine_month.py [--workers 8]
Итог: turbo/results/engine_month.json, кривые — turbo/results/engine_month/<вариант>/<старт>.npy.
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
import coin_pick as CP  # noqa: E402
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

L, H = DS.L, DS.H
OUT = os.path.join(HERE, "results", "engine_month")
EMA = os.path.join(DS.ROOT, "configs", "examples", "ema_anchor.json")
NOSHORT = {"bot.short.risk.total_wallet_exposure_limit": 0.0}
VARIANTS = {f"tm{tw}": (f"движок 3.0, экспозиция {tw}", {L + "total_wallet_exposure_limit": float(tw)}, None)
            for tw in (3, 6, 10, 14, 20, 30)}
VARIANTS.update({f"ema{tw}": (f"EMA Anchor, экспозиция {tw}", {L + "total_wallet_exposure_limit": float(tw), **NOSHORT}, EMA)
                 for tw in (5, 10, 20, 30, 40)})
VARIANTS.update({f"ema20n{n}": (f"EMA Anchor, эксп. 20, {n} монет", {L + "total_wallet_exposure_limit": 20.0,
                                                                      L + "n_positions": float(n), **NOSHORT}, EMA)
                 for n in (5, 7)})
HALVES = {"I пол. 2026": CP.grid("2026-01-01", "2026-05-31", 3), "II пол. 2026": CP.grid("2026-07-01", "2026-08-31", 3),
          "обвал 10.10.2025": CP.grid("2025-09-22", "2025-10-10", 3)}      # стресс: месяцы, захватившие обвал
RULES = {"без правил": None, "стоп −30% от старта": ("start", 0.7), "стоп −30% от максимума": ("peak", 0.7)}


def run(key, start):
    path = os.path.join(OUT, key, f"{start}.npy")
    if os.path.exists(path):
        return np.load(path)
    _, over, base = VARIANTS[key]
    end = (datetime.fromisoformat(start) + timedelta(days=30)).strftime("%Y-%m-%d")
    res = DS.backtest_curve({**PW.ENGINE3, **over}, start, end, f"em_{key}_{start}", base=base)
    if isinstance(res, str):
        print("  ошибка", key, start, res[:160], flush=True)
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, res[0])
    shutil.rmtree(res[1], ignore_errors=True)
    return res[0]


def month(rel, start, btc, rule):
    """Множитель капитала за месяц: без правил — конец прогона; со стопом — выход и до конца месяца без торговли."""
    if rule is not None:
        kind, s = rule
        fl = np.maximum.accumulate(rel) * s if kind == "peak" else np.full(len(rel), s)
        i = DS.first((rel <= fl) | DR.crash_minutes(btc, start, len(rel), 12, 0.05))
        if i is not None:                             # стоп или обвал BTC раньше конца прогона
            return float(rel[i:i + DS.DELAY + 1].min()) * (1 - DS.COST)
    if len(rel) < 30 * 1440 * 0.95 and rel[-1] < 0.5:
        return 0.05                                   # движок оборвал прогон — ликвидация
    return float(rel[-1])


def stats(c):
    c = np.array(c)
    return {"n": len(c), "median": round(float(np.median(c)), 3),
            "geo": round(math.exp(float(np.mean(np.log(np.maximum(c, 1e-6))))), 3),
            "p_x2": round(float((c >= 2).mean()) * 100, 1), "p_x15": round(float((c >= 1.5).mean()) * 100, 1),
            "p_loss": round(float((c < 1).mean()) * 100, 1), "p_half": round(float((c <= 0.5).mean()) * 100, 1),
            "worst": round(float(c.min()), 3), "best": round(float(c.max()), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    jobs = [(k, s) for k in VARIANTS for h in HALVES.values() for s in h]
    with ThreadPoolExecutor(a.workers) as ex:
        curves = dict(zip(jobs, ex.map(lambda j: run(*j), jobs)))
    btc = RR.btc_hourly()
    rows = []
    for key, (name, _, _) in VARIANTS.items():
        for rname, rule in RULES.items():
            row = {"key": key, "name": name, "rule": rname}
            for half, starts in HALVES.items():
                row[half] = stats([month(curves[(key, s)], s, btc, rule) for s in starts if curves[(key, s)] is not None])
            rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "engine_month.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("Бот без остановок весь месяц, прибыль в обороте, $200; старт каждые 3 дня\n"
          "средн. геом. — типичный рост за месяц; ×2 — доля месяцев с +100% и больше; ≤½ — потеря половины и больше\n")
    for r in rows:
        print(f"{r['name']:<26} {r['rule']:<24} | " + " | ".join(
            f"{h.split()[0]:>2}: медиана ×{x['median']:.2f}, геом. ×{x['geo']:.2f}, ×2 {x['p_x2']:4.1f}%, ×1,5 {x['p_x15']:4.1f}%, "
            f"минус {x['p_loss']:4.1f}%, ≤½ {x['p_half']:4.1f}%, худший ×{x['worst']:.2f}"
            for h, x in ((h, r[h]) for h in HALVES)))


if __name__ == "__main__":
    main()
