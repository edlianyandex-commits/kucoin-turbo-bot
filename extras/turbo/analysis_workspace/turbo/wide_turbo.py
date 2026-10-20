"""Turbo на 10+ монетах с меньшей ставкой на монету (пользователь, 01.10.2026): цель — удвоение за месяц, +1000% в год
(≈ +22% в месяц со сложным процентом).

Шаг 1 — отбор (как turbo/engine_month.py): месяц = один 30-дневный прогон passivbot со $200 (бот без остановок, прибыль
в обороте), старт каждые 3 дня: I пол. 2026, II пол. 2026, месяцы с обвалом 10.10.2025. EMA Anchor (как Turbo 4.0),
10 и 15 монет, общая экспозиция 20 / 30 / 40 (на монету 1,3–4 против 6,7 у Turbo 4.0 на 3 монетах).
Правила поверх прогона: нет; 4.0 (стоп −40% от старта + выход при обвале BTC); ×2 (фиксация при удвоении + то же).
Расходы на выход — 0,1% капитала на единицу экспозиции (у экспозиции 20 — 2%, как во всех расчётах Turbo).

    venv\\Scripts\\python turbo\\wide_turbo.py [--workers 8]
Итог: turbo/results/wide_turbo.json и таблица; кривые — turbo/results/engine_month/<вариант>/.
"""
import argparse
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import engine_month as EM  # noqa: E402
import risk_rules as RR  # noqa: E402

L = DS.L
TW = {"ema20": 20, "ema30": 30}                     # Turbo 4.0 (3 монеты) и экспозиция 30 — для сравнения
for n in (10, 15):
    for tw in (20, 30, 40):
        key = f"ema{tw}n{n}"
        EM.VARIANTS[key] = (f"EMA Anchor, {n} монет, эксп. {tw}", {L + "total_wallet_exposure_limit": float(tw),
                                                                    L + "n_positions": float(n), **EM.NOSHORT}, EM.EMA)
        TW[key] = tw
EM.VARIANTS["ema20"] = ("Turbo 4.0: EMA Anchor, 3 монеты, эксп. 20", *EM.VARIANTS["ema20"][1:])
EM.VARIANTS["ema30"] = ("EMA Anchor, 3 монеты, эксп. 30", *EM.VARIANTS["ema30"][1:])
RULES = {"без правил": None, "4.0: стоп −40% + выход по BTC": (0.6, None), "×2: фиксация + стоп −40% + выход по BTC": (0.6, 2.0)}
GOAL = 11.0 ** (1 / 12)                              # +1000% в год = ×11 → ×1,221 в месяц


def month(rel, start, btc, rule, cost):
    """Множитель капитала за месяц; со стопом/целью — выход и до конца месяца без торговли."""
    if rule is not None:
        stop, target = rule
        hit = (rel <= stop) | DR.crash_minutes(btc, start, len(rel), 12, 0.05)
        if target:
            hit |= rel >= target
        i = DS.first(hit)
        if i is not None:
            return float(rel[i:i + DS.DELAY + 1].min()) * (1 - cost)
    if len(rel) < 30 * 1440 * 0.95 and rel[-1] < 0.5:
        return 0.05                                   # движок оборвал прогон — ликвидация
    return float(rel[-1])


def stats(c):
    c = np.array(c)
    geo = math.exp(float(np.mean(np.log(np.maximum(c, 1e-6)))))
    return {"n": len(c), "mean": round((float(c.mean()) - 1) * 100, 1), "median": round((float(np.median(c)) - 1) * 100, 1),
            "geo": round((geo - 1) * 100, 1), "year": round((geo ** 12 - 1) * 100), "p_x2": round(float((c >= 1.98).mean()) * 100, 1),
            "p_goal": round(float((c >= GOAL).mean()) * 100, 1), "p_loss": round(float((c < 1).mean()) * 100, 1),
            "p_half": round(float((c <= 0.5).mean()) * 100, 1), "worst": round((float(c.min()) - 1) * 100), "best": round((float(c.max()) - 1) * 100)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    keys = list(TW)
    jobs = [(k, s) for k in keys for h in EM.HALVES.values() for s in h]
    with ThreadPoolExecutor(a.workers) as ex:
        curves = dict(zip(jobs, ex.map(lambda j: EM.run(*j), jobs)))
    btc = RR.btc_hourly()
    rows = []
    for k in keys:
        for rname, rule in RULES.items():
            row = {"key": k, "name": EM.VARIANTS[k][0], "rule": rname}
            all26 = []
            for half, starts in EM.HALVES.items():
                m = [month(curves[(k, s)], s, btc, rule, 0.001 * TW[k]) for s in starts if curves[(k, s)] is not None]
                row[half] = stats(m)
                if "2026" in half:
                    all26 += m
            row["2026"] = stats(all26)
            rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "wide_turbo.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Месяц = 30 дней с прибылью в обороте, $200; цель +1000%/год = ×{GOAL:.3f} в месяц (+{(GOAL - 1) * 100:.1f}%)\n"
          "геом. — типичный месяц; «год» — если так каждый месяц; ≥цели — доля месяцев с +22% и больше\n")
    H = list(EM.HALVES)
    for r in sorted(rows, key=lambda r: -r["2026"]["geo"]):
        x, a1, a2, c = r["2026"], r[H[0]], r[H[1]], r[H[2]]
        print(f"{r['name'][:38]:<38} {r['rule'][:24]:<24} | 2026: геом. {x['geo']:+5.1f}%/мес (год {x['year']:+6d}%), "
              f"средн. {x['mean']:+5.1f}%, ≥цели {x['p_goal']:4.1f}%, ×2 {x['p_x2']:4.1f}%, в минусе {x['p_loss']:4.1f}%, "
              f"≤½ {x['p_half']:4.1f}%, худш. {x['worst']:+4d}% | I {a1['geo']:+5.1f}% II {a2['geo']:+5.1f}% | обвал 2025: "
              f"геом. {c['geo']:+5.1f}%, худш. {c['worst']:+4d}%")


if __name__ == "__main__":
    main()
