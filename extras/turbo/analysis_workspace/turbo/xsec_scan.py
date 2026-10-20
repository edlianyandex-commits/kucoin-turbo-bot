"""Рыночно-нейтральный возврат к среднему между монетами (пользователь, 01.10.2026: «торговля в боковике, больше сделок,
максимальная прибыль» при плече ≤ 10×). Часовые свечи Binance USDⓈ-M 35 монет Turbo (turbo/results/ohlc_1h_2025-08.npz).

Каждые R часов: доход каждой монеты за последние Lb часов (по закрытию); лонг n самых отставших, шорт n самых
обогнавших («возврат»; знак −1 — наоборот, «продолжение»), веса ±G/(2n), держать до следующей перестановки.
G — общий объём лонг + шорт к капиталу (≤ 10 — плечо 10×). Расходы — 0,08% на сторону с оборота при каждой перестановке.
Монеты без цены в часе — не торгуются. Капитал — по часам, сложный процент.
Отбор — I пол. 2026, проверка — II пол. 2026 и обвал 10.10.2025 (15.09–15.11.2025). Внутри часа обвал не виден
(10.10.2025 альты падали на 50–80% за минуты) — для живого бота нужен запас.

    venv\\Scripts\\python turbo\\xsec_scan.py
Итог: turbo/results/xsec_scan.json и таблица.
"""
import itertools
import json
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import range_scan as RS  # noqa: E402

COST = 0.0008


def run(c, a, b, Lb, R, n, G, sign):
    """Капитал по часам с a по b."""
    ret = np.zeros_like(c)
    ret[1:] = c[1:] / c[:-1] - 1
    ret = np.nan_to_num(ret, nan=0.0)
    w = np.zeros(c.shape[1])
    eq = np.empty(b - a)
    cap = 1.0
    for k, i in enumerate(range(a, b)):
        cap *= 1 + float(w @ ret[i])                                  # доход за час i при весах с прошлого часа
        if (i - a) % R == 0 and i - Lb >= 0:
            past = c[i] / c[i - Lb] - 1
            ok = ~np.isnan(past)
            new = np.zeros_like(w)
            if ok.sum() >= 2 * n:
                order = np.argsort(np.where(ok, past, np.nan))[:ok.sum()]   # от худших к лучшим среди доступных
                lo_idx, hi_idx = order[:n], order[-n:]
                new[lo_idx] = sign * G / (2 * n)
                new[hi_idx] = -sign * G / (2 * n)
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
    grid = [dict(Lb=Lb, R=R, n=n, G=G, sign=sign) for Lb, R, n, G, sign in
            itertools.product((1, 4, 12, 24, 72), (1, 4, 12, 24), (3, 5, 8), (4.0,), (1, -1)) if R <= max(Lb, 4)]
    rows = []
    for p in grid:
        row = {"params": p}
        for name, (a, b) in span.items():
            eq = run(c, a, b, p["Lb"], p["R"], p["n"], p["G"], p["sign"])
            row[name] = RS.stats(eq)
        rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "xsec_scan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    P = list(RS.PERIODS)
    both = [r for r in rows if r[P[0]]["geo"] > 0 and r[P[1]]["geo"] > 0]
    print(f"Наборов {len(rows)}; в плюсе и в I, и во II пол. 2026: {len(both)} (объём лонг + шорт 4× капитала)\n")
    for r in sorted(rows, key=lambda r: -min(r[P[0]]["geo"], r[P[1]]["geo"]))[:15]:
        p, x, y, z = r["params"], r[P[0]], r[P[1]], r[P[2]]
        print(f"{'возврат' if p['sign'] > 0 else 'продолж.'}: доход за {p['Lb']:>2} ч, перестановка раз в {p['R']:>2} ч, по {p['n']} монет | "
              f"I: {x['geo']:+5.1f}%/мес, просадка {x['dd']:5.1f}%, в минусе {x['p_loss']:4.1f}% | II: {y['geo']:+5.1f}%/мес, "
              f"просадка {y['dd']:5.1f}% | обвал 2025: {z['geo']:+5.1f}%/мес, просадка {z['dd']:5.1f}%")


if __name__ == "__main__":
    main()
