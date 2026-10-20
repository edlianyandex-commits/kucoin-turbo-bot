"""Turbo «в состоянии дать 100+% в месяц» при плече ≤ 10× (пользователь, 01.10.2026): что вообще даёт ×2 за месяц и какой ценой.
Часовые свечи 35 монет (turbo/results/ohlc_1h_2025-08.npz), расходы 0,08% на сторону, финансирование 0,01% за 8 ч с объёма.

Кандидаты (объём — к капиталу, ≤ 10):
  лидеры — только лонг на n монет с наибольшим ростом за Lb часов, пересмотр раз в R часов;
  лидеры + фильтр — то же, но без позиций, пока BTC ниже своей средней за 168 ч;
  продолжение — лонг лидеров / шорт отстающих (turbo/xsec_check.py);
  пробой — turbo/range_scan.py --breakout с позицией e × капитал, до M позиций.
Месяц = 30 дней со старта каждого дня 2026-01-01…08-31 (I пол. — старты до 31.05, II пол. — с 01.07); стресс —
старты 15.09–10.10.2025. Внутри часа обвал не виден: 10.10.2025 альты падали на 50–80% за минуты — при объёме 10×
в лонге это потеря всего, сколько бы ни показывали часовые свечи.

    venv\\Scripts\\python turbo\\lottery_scan.py
Итог: turbo/results/lottery_scan.json и таблица.
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

COST, FUND = 0.0008, 0.0001


def run_mom(c, a, b, Lb, R, n, G, long_only, regime=None):
    ret = np.zeros_like(c)
    ret[1:] = c[1:] / c[:-1] - 1
    ret = np.nan_to_num(ret, nan=0.0)
    w = np.zeros(c.shape[1])
    eq = np.empty(b - a)
    cap = 1.0
    for k, i in enumerate(range(a, b)):
        cap *= 1 + float(w @ ret[i]) - FUND / 8 * float(np.abs(w).sum())
        if (i - a) % R == 0 and i - Lb >= 0:
            past = c[i] / c[i - Lb] - 1
            ok = ~np.isnan(past)
            new = np.zeros_like(w)
            if ok.sum() >= 2 * n and (regime is None or regime[i]):
                order = np.argsort(np.where(ok, past, np.inf))[:ok.sum()]
                if long_only:
                    new[order[-n:]] = G / n
                else:
                    new[order[-n:]] = G / (2 * n)
                    new[order[:n]] = -G / (2 * n)
            cap *= 1 - float(np.abs(new - w).sum()) * COST
            w = new
        eq[k] = max(cap, 1e-9)
        if cap <= 0.02:
            eq[k:] = 1e-9
            break
    return eq


def months(eq, t, a, starts):
    """30-дневные множители по дням старта (часовой капитал eq начинается с часа a)."""
    out = []
    for s in starts:
        i = int(np.searchsorted(t, s)) - a
        j = i + 30 * 24
        if 0 <= i and j < len(eq):
            out.append(eq[j] / eq[i])
    return np.array(out)


def summary(m):
    if not len(m):
        return None
    return {"n": len(m), "p_x2": round(float((m >= 2).mean()) * 100, 1), "p_x15": round(float((m >= 1.5).mean()) * 100, 1),
            "p_half": round(float((m <= 0.5).mean()) * 100, 1), "p_ruin": round(float((m <= 0.05).mean()) * 100, 1),
            "median": round((float(np.median(m)) - 1) * 100, 1),
            "geo": round((float(np.exp(np.mean(np.log(np.maximum(m, 1e-9))))) - 1) * 100, 1),
            "best": round((float(m.max()) - 1) * 100), "worst": round((float(m.min()) - 1) * 100)}


def main():
    t, o, h, l, c, coins = RS.ohlc()
    def ms(d):
        ts = pd.Timestamp(d)
        return int((ts if ts.tzinfo else ts.tz_localize("UTC")).value // 1_000_000)
    a26, b26 = int(np.searchsorted(t, ms("2026-01-01"))) - 400, int(np.searchsorted(t, ms("2026-10-01")))
    a25, b25 = int(np.searchsorted(t, ms("2025-09-15"))) - 400, int(np.searchsorted(t, ms("2025-11-15")))
    days = pd.date_range("2026-01-01", "2026-08-31", freq="D", tz="UTC")
    st_I = [ms(d) for d in days if d < pd.Timestamp("2026-06-01", tz="UTC")]
    st_II = [ms(d) for d in days if d >= pd.Timestamp("2026-07-01", tz="UTC")]
    st_cr = [ms(d) for d in pd.date_range("2025-09-15", "2025-10-10", freq="D", tz="UTC")]
    btc = c[:, coins.index("BTC")]
    regime = pd.Series(btc).rolling(168, min_periods=168).mean().values < btc       # BTC выше средней за неделю
    cands = []
    for Lb, R, n, G in itertools.product((24, 72, 168), (12, 24), (1, 2, 3), (5.0, 10.0)):
        cands.append((f"лидеры: {n} мон., рост за {Lb} ч, раз в {R} ч, объём {G:g}×", dict(kind="mom", Lb=Lb, R=R, n=n, G=G, lo=True, reg=False)))
        cands.append((f"лидеры + фильтр BTC: {n} мон., {Lb} ч, раз в {R} ч, {G:g}×", dict(kind="mom", Lb=Lb, R=R, n=n, G=G, lo=True, reg=True)))
    for Lb, n, G in itertools.product((72, 336), (3, 5), (6.0, 10.0)):
        cands.append((f"продолжение лонг/шорт: {Lb} ч, раз в 12 ч, по {n}, объём {G:g}×", dict(kind="mom", Lb=Lb, R=12, n=n, G=G, lo=False, reg=False)))
    for e, M_ in ((1.0, 10), (2.0, 5)):
        cands.append((f"пробой 96 ч, цель 0,5 ширины, ≤24 ч, лонг, {M_} поз. по {e:g}×", dict(kind="brk", e=e, M=M_)))
    ind96 = RS.indicators(h, l, c, 96)
    rows = []
    for name, p in cands:
        res = {}
        for per, (a, b, sts) in {"2026": (a26, b26, None), "обвал 2025": (a25, b25, st_cr)}.items():
            if p["kind"] == "mom":
                eq = run_mom(c, a, b, p["Lb"], p["R"], p["n"], p["G"], p["lo"], regime if p["reg"] else None)
            else:
                bp = dict(N=96, W=0, m=0.5, T=24, short=False, M=p["M"], e=p["e"])
                eq, _ = RS.simulate_breakout(t, o, h, l, c, ind96, bp, a, b)
            if per == "2026":
                res["I пол."] = summary(months(eq, t, a, st_I))
                res["II пол."] = summary(months(eq, t, a, st_II))
            else:
                res[per] = summary(months(eq, t, a, sts))
        rows.append({"name": name, "params": p, **res})
    json.dump(rows, open(os.path.join(HERE, "results", "lottery_scan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("Доля месяцев с ×2 и больше / −50% и хуже / сгорел (≤ −95%); типичный месяц (геом.). Плечо ≤ 10×.\n")
    for r in sorted(rows, key=lambda r: -((r["I пол."] or {}).get("p_x2", 0) + (r["II пол."] or {}).get("p_x2", 0)))[:20]:
        x, y, z = r["I пол."], r["II пол."], r["обвал 2025"]
        print(f"{r['name'][:60]:<60} | I: ×2 {x['p_x2']:4.1f}%, ≤½ {x['p_half']:4.1f}%, сгорел {x['p_ruin']:4.1f}%, типичн. {x['geo']:+6.1f}% | "
              f"II: ×2 {y['p_x2']:4.1f}%, ≤½ {y['p_half']:4.1f}%, сгорел {y['p_ruin']:4.1f}%, типичн. {y['geo']:+6.1f}% | "
              f"обвал: ×2 {z['p_x2']:4.1f}%, ≤½ {z['p_half']:4.1f}%, типичн. {z['geo']:+6.1f}%")


if __name__ == "__main__":
    main()
