"""Финал широкого Turbo (turbo/wide_turbo.py): ставки подряд «как в жизни» — после выхода по BTC или стопа следующая
со следующего дня на весь капитал (turbo/ema_chain.py, month_paths.Engine). Прогон passivbot со старта каждого дня
2026-01-01…09-28 и 2025-09-10…11-08 (обвал 10.10.2025). Цель пользователя — +1000% в год (≈ +22% в месяц).

    venv\\Scripts\\python turbo\\wide_chain.py [--workers 8]
Итог: turbo/results/wide_chain.json и таблица.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import ema_chain as EC  # noqa: E402
import month_paths as MP  # noqa: E402
import risk_rules as RR  # noqa: E402
import wide_turbo as WT  # noqa: E402  (регистрирует варианты 10/15 монет в engine_month.VARIANTS)

KEYS = ("ema20n10", "ema30n15", "ema20")
RULES = {"без цели, стоп −40% + выход по BTC (как 4.0)": {"mode": "target", "T": None, "S": 0.6},
         "без стопа, только выход по BTC": {"mode": "none"},
         "×2: фиксация + стоп −40% + выход по BTC": {"mode": "target", "T": 2.0, "S": 0.6}}
WINDOWS = {"2026": CP.grid("2026-01-01", "2026-08-31", 1), "I пол. 2026": CP.grid("2026-01-01", "2026-05-31", 1),
           "II пол. 2026": CP.grid("2026-07-01", "2026-08-31", 1), "обвал 10.10.2025": CP.grid("2025-09-10", "2025-10-10", 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    btc = RR.btc_hourly()
    rows = []
    for key in KEYS:
        E = MP.Engine(0.001 * WT.TW[key], EC.bets(key, EC.build(key, a.workers), btc))
        for rname, rule in RULES.items():
            row = {"key": key, "name": WT.EM.VARIANTS[key][0], "rule": rname}
            for w, starts in WINDOWS.items():
                row[w] = WT.stats([E.month(s, rule)[0] for s in starts])
            # «год»: ставки подряд с 1–15 января по 28 сентября 2026 (почти 9 месяцев), пересчёт на 12 месяцев
            ys = np.array([E.month(s, rule, int((np.datetime64("2026-09-28") - np.datetime64(s)).astype(int)))[0]
                           for s in CP.grid("2026-01-01", "2026-01-15", 1)])
            months = (np.datetime64("2026-09-28") - np.datetime64("2026-01-08")).astype(int) / 30.4
            row["year"] = {"chain_median": round(float(np.median(ys)), 2), "chain_min": round(float(ys.min()), 2),
                           "chain_max": round(float(ys.max()), 2),
                           "annual_median": round((float(np.median(ys)) ** (12 / months) - 1) * 100)}
            rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "wide_chain.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("Ставки подряд, прибыль в обороте, $200; месяц = 30 дней со старта каждого дня; цель +22%/мес (+1000%/год)\n")
    for r in rows:
        x, c, y = r["2026"], r["обвал 10.10.2025"], r["year"]
        print(f"{r['name'][:38]:<38} {r['rule'][:44]:<44}\n"
              f"   2026: геом. {x['geo']:+5.1f}%/мес, средн. {x['mean']:+5.1f}%, медиана {x['median']:+5.1f}%, ≥+22% {x['p_goal']:4.1f}%, "
              f"×2 {x['p_x2']:4.1f}%, в минусе {x['p_loss']:4.1f}%, ≤½ {x['p_half']:4.1f}%, худш. {x['worst']:+4d}%, лучш. {x['best']:+4d}%\n"
              f"   I пол. {r['I пол. 2026']['geo']:+5.1f}%/мес, II пол. {r['II пол. 2026']['geo']:+5.1f}%/мес | обвал 2025: геом. "
              f"{c['geo']:+5.1f}%, худш. {c['worst']:+4d}% | 01.01→28.09.2026 подряд: ×{y['chain_median']} (×{y['chain_min']}…×{y['chain_max']}), "
              f"в пересчёте на год {y['annual_median']:+d}%")


if __name__ == "__main__":
    main()
