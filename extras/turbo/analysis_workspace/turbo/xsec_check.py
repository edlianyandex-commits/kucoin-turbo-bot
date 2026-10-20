"""Проверки находки turbo/xsec_scan.py («продолжение»: лонг лидеров / шорт отстающих за Lb часов, пересмотр раз в R часов):
  1) не край ли сетки — Lb до 336 ч, R до 48 ч;
  2) «эффект выжившего» — без 5 лучших монет 2026 и только на «старых» монетах (торговались на Binance до 2024 г.);
  3) плата за финансирование — 0,01% за 8 ч с каждой стороны объёма (осторожная оценка: лидеры обычно платят в лонге,
     отстающие — в шорте);
  4) объём 2× и 4× капитала.

    venv\\Scripts\\python turbo\\xsec_check.py
Итог: turbo/results/xsec_check.json и таблицы.
"""
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import range_scan as RS  # noqa: E402

COST = 0.0008
NEW = {"HYPE", "ASTER", "ENA", "TAO", "WLD", "ONDO", "KAS", "GRAM", "SUI", "APT", "ARB", "RENDER", "ZEC", "XMR"}
# «новые» на Binance USDⓈ-M с 2023 г. или резко ожившие в 2025–26 (ZEC, XMR) — исключаются в проверке на «старых» монетах


def run(c, a, b, Lb, R, n, G, funding=0.0):
    ret = np.zeros_like(c)
    ret[1:] = c[1:] / c[:-1] - 1
    ret = np.nan_to_num(ret, nan=0.0)
    w = np.zeros(c.shape[1])
    eq = np.empty(b - a)
    cap = 1.0
    for k, i in enumerate(range(a, b)):
        cap *= 1 + float(w @ ret[i]) - funding / 8 * float(np.abs(w).sum())
        if (i - a) % R == 0 and i - Lb >= 0:
            past = c[i] / c[i - Lb] - 1
            ok = ~np.isnan(past)
            new = np.zeros_like(w)
            if ok.sum() >= 2 * n:
                order = np.argsort(np.where(ok, past, np.inf))[:ok.sum()]
                new[order[-n:]] = G / (2 * n)          # лидеры — лонг
                new[order[:n]] = -G / (2 * n)          # отстающие — шорт
            cap *= 1 - float(np.abs(new - w).sum()) * COST
            w = new
        eq[k] = max(cap, 1e-9)
        if cap <= 0.02:
            eq[k:] = 1e-9
            break
    return eq


def main():
    t, o, h, l, c, coins = RS.ohlc()
    span = {name: (int(np.searchsorted(t, int(pd.Timestamp(a_, tz="UTC").value // 1_000_000))),
                   int(np.searchsorted(t, int(pd.Timestamp(b_, tz="UTC").value // 1_000_000))))
            for name, (a_, b_) in RS.PERIODS.items()}
    P = list(RS.PERIODS)
    a26, b26 = span[P[0]][0], span[P[1]][1]
    total = c[b26 - 1] / c[a26] - 1
    best5 = [coins[j] for j in np.argsort(np.nan_to_num(total, nan=-9))[-5:]]
    print("лучшие монеты 2026 (01.01–30.09):", ", ".join(f"{coins[j]} {total[j] * 100:+.0f}%" for j in np.argsort(np.nan_to_num(total, nan=-9))[-5:]))
    universes = {"все 35": list(range(len(coins))),
                 "без 5 лучших 2026": [j for j, x in enumerate(coins) if x not in best5],
                 f"только «старые» ({len(coins) - len(NEW & set(coins))})": [j for j, x in enumerate(coins) if x not in NEW]}
    rows = []
    for Lb, R in itertools.product((48, 72, 96, 120, 168, 240, 336), (12, 24, 48)):
        for n in (3, 5):
            row = {"Lb": Lb, "R": R, "n": n}
            for name, (a, b) in span.items():
                row[name] = RS.stats(run(c, a, b, Lb, R, n, 4.0))
            rows.append(row)
    print("\n1) Сетка дальше 72 ч (все монеты, объём 4×, без финансирования): типичный месяц I / II пол. 2026 / обвал 2025, просадка")
    for r in rows:
        x, y, z = r[P[0]], r[P[1]], r[P[2]]
        mark = "★" if x["geo"] > 0 and y["geo"] > 0 else " "
        print(f"{mark} {r['Lb']:>3} ч / раз в {r['R']:>2} ч / по {r['n']}: I {x['geo']:+6.1f}% ({x['dd']:4.0f}%)  II {y['geo']:+6.1f}% ({y['dd']:4.0f}%)  "
              f"обвал {z['geo']:+6.1f}% ({z['dd']:4.0f}%)")
    checks = []
    print("\n2–4) Устойчивость лучших наборов: монеты, финансирование 0,01%/8 ч, объём")
    for Lb, R, n in ((72, 12, 3), (72, 12, 5), (72, 12, 8), (96, 12, 5), (120, 24, 5)):
        for uname, idx in universes.items():
            for fund, G in ((0.0, 4.0), (0.0001, 4.0), (0.0001, 2.0)):
                cc = c[:, idx]
                row = {"Lb": Lb, "R": R, "n": n, "universe": uname, "funding": fund, "G": G}
                for name, (a, b) in span.items():
                    row[name] = RS.stats(run(cc, a, b, Lb, R, n, G, fund))
                checks.append(row)
                x, y, z = row[P[0]], row[P[1]], row[P[2]]
                print(f"  {Lb:>3}/{R:>2}/по {n} | {uname:<22} | финанс. {'да ' if fund else 'нет'} | объём {G:g}× | "
                      f"I {x['geo']:+6.1f}% ({x['dd']:4.0f}%)  II {y['geo']:+6.1f}% ({y['dd']:4.0f}%)  обвал {z['geo']:+6.1f}% ({z['dd']:4.0f}%)")
    json.dump({"grid": rows, "checks": checks, "best5": best5}, open(os.path.join(HERE, "results", "xsec_check.json"), "w",
                                                                     encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
