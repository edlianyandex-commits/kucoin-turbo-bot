"""Портфель «шорт новых листингов» (Turbo, 01.10.2026) на данных turbo/listings_data.py (Binance, 323 листинга 12.2024–09.2026).

Сделка: шорт на открытии часа D×24 после первой свечи; стоп — если максимум часа ≥ вход × (1+S): выход по
max(стоп, открытие) × 1,01; иначе — через hold дней по закрытию. Размер — e × капитал на момент входа; позиций
одновременно не больше M (иначе листинг пропускается); объём e × M ≤ 8 (плечо ≤ 10×). Комиссия 0,15% на сторону,
финансирование — настоящие ставки Binance (шорт получает положительные, платит отрицательные) с размера позиции.
Капитал по часам (сложный процент). Месяц = 30 дней со старта каждого дня.
Отбор — 2025 I, проверка — 2025 II и 2026; проверка «без 5 самых прибыльных сделок».

    venv\\Scripts\\python turbo\\listing_port.py
Итог: turbo/results/listing_port.json и таблица.
"""
import itertools
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
H = 3_600_000
COST = 0.0015


def load():
    z = np.load(os.path.join(HERE, "results", "listings_binance.npz"))
    meta = json.load(open(os.path.join(HERE, "results", "listings_meta.json")))
    return [(m["symbol"], z[f"k_{m['symbol']}"], z[f"f_{m['symbol']}"]) for m in meta]


def plan(data, D, hold, S, exclude=()):
    """Сделки (без учёта размера): символ, вход, выход, цена входа, путь цен по часам, финансирование по часам."""
    out = []
    for sym, k, f in data:
        if sym in exclude:
            continue
        i = int(D * 24)
        j = i + int(hold * 24)
        if i >= len(k):
            continue
        j = min(j, len(k) - 1)
        e = k[i, 1]
        exit_i, exit_px = j, k[j, 4]
        if S:
            hit = np.flatnonzero(k[i:j + 1, 2] >= e * (1 + S))
            if hit.size:
                exit_i = i + int(hit[0])
                exit_px = max(e * (1 + S), k[exit_i, 1]) * 1.01
        fund = {}
        if len(f):
            for ft, rate in f:
                if k[i, 0] <= ft <= k[exit_i, 0]:
                    fund[int(ft // H * H)] = fund.get(int(ft // H * H), 0.0) + rate
        path = {int(k[x, 0]) + H: k[x, 4] for x in range(i, exit_i)}
        out.append({"sym": sym, "t_in": int(k[i, 0]), "t_out": int(k[exit_i, 0]) + H, "px_in": e, "px_out": exit_px,
                    "path": path, "fund": fund})
    return sorted(out, key=lambda x: x["t_in"])


def simulate(trades, e, M, t_from, t_to):
    """Капитал по часам: открытые шорты оцениваются по закрытиям, финансирование — в часы выплат."""
    hours = np.arange(t_from, t_to, H)
    eq = np.empty(len(hours))
    cash, open_, k, done = 1.0, [], 0, []
    for n, t in enumerate(hours):
        while k < len(trades) and trades[k]["t_in"] < t:          # вход на открытии часа t_in (до закрытия t_in + H)
            tr = trades[k]
            k += 1
            if tr["t_in"] < t_from or len(open_) >= M:
                continue
            equity = cash + sum(o["notional"] * -(o["last"] / o["px_in"] - 1) for o in open_)
            notional = e * max(equity, 0)
            cash -= notional * COST
            open_.append(dict(tr, notional=notional, last=tr["px_in"]))
        still = []
        for o in open_:
            fr = o["fund"].get(int(t - H), 0.0)
            cash += o["notional"] * fr
            if t >= o["t_out"]:
                pnl = o["notional"] * -(o["px_out"] / o["px_in"] - 1) - o["notional"] * (o["px_out"] / o["px_in"]) * COST
                cash += pnl
                done.append(pnl)
            else:
                o["last"] = o["path"].get(int(t), o["last"])
                still.append(o)
        open_ = still
        val = cash + sum(o["notional"] * -(o["last"] / o["px_in"] - 1) for o in open_)
        eq[n] = max(val, 1e-9)
        if val <= 0.02:
            eq[n:] = 1e-9
            break
    return hours, eq, done


def window_stats(hours, eq, a_ms, b_ms):
    d = [(t, v) for t, v in zip(hours[::24], eq[::24]) if a_ms <= t < b_ms]
    if len(d) < 31:
        return None
    v = np.array([x for _, x in d])
    m = v[30:] / v[:-30]
    return {"geo": round((math.exp(float(np.mean(np.log(np.maximum(m, 1e-9))))) - 1) * 100, 1),
            "total": round((v[-1] / v[0] - 1) * 100, 1), "p_x15": round(float((m >= 1.5).mean()) * 100, 1),
            "p_x2": round(float((m >= 2).mean()) * 100, 1), "p_loss": round(float((m < 1).mean()) * 100, 1),
            "p_half": round(float((m <= 0.5).mean()) * 100, 1), "worst": round((float(m.min()) - 1) * 100),
            "best": round((float(m.max()) - 1) * 100),
            "dd": round(float((1 - v / np.maximum.accumulate(v)).max()) * 100, 1)}


def ms(s):
    return int(np.datetime64(s, "ms").astype("int64"))


PERIODS = {"2025 I (отбор)": ("2025-01-01", "2025-07-01"), "2025 II": ("2025-07-01", "2026-01-01"),
           "2026": ("2026-01-01", "2026-10-01")}


def main():
    data = load()
    t_from, t_to = ms("2024-12-15"), ms("2026-10-01")
    rows = []
    for D, hold, S, (e, M) in itertools.product((3, 5, 7), (14, 21, 30), (0.4, 0.6, 1.0), ((0.2, 20), (0.4, 20), (0.4, 10))):
        trades = plan(data, D, hold, S)
        hours, eq, done = simulate(trades, e, M, t_from, t_to)
        row = {"D": D, "hold": hold, "S": S, "e": e, "M": M, "trades": len(done)}
        for p, (a, b) in PERIODS.items():
            row[p] = window_stats(hours, eq, ms(a), ms(b))
        rows.append(row)
    P = list(PERIODS)
    good = [r for r in rows if all(r[p] and r[p]["geo"] > 0 for p in P)]
    print(f"Наборов {len(rows)}; типичный месяц в плюсе во всех трёх периодах: {len(good)}\n")
    for r in sorted(rows, key=lambda r: -min(r[p]["geo"] for p in P))[:15]:
        print(f"день {r['D']}, {r['hold']:>2} дн., стоп +{int(r['S'] * 100):>3}%, позиция {r['e']:g}×, до {r['M']} поз. | " + " | ".join(
            f"{p[:7]}: типичн. {r[p]['geo']:+5.1f}%, ×1,5 {r[p]['p_x15']:4.1f}%, ×2 {r[p]['p_x2']:4.1f}%, минус {r[p]['p_loss']:4.1f}%, "
            f"≤½ {r[p]['p_half']:4.1f}%, просадка {r[p]['dd']:4.0f}%" for p in P))
    # без 5 самых прибыльных сделок у лучшего по отбору набора
    best = max(rows, key=lambda r: r[P[0]]["geo"])
    trades = plan(data, best["D"], best["hold"], best["S"])
    hours, eq, done = simulate(trades, best["e"], best["M"], t_from, t_to)
    pnl_by = {}
    for tr in trades:
        pnl_by[tr["sym"]] = -(tr["px_out"] / tr["px_in"] - 1)
    top5 = [s for s, _ in sorted(pnl_by.items(), key=lambda x: -x[1])[:5]]
    hours2, eq2, _ = simulate(plan(data, best["D"], best["hold"], best["S"], exclude=top5), best["e"], best["M"], t_from, t_to)
    print(f"\nЛучший по отбору: день {best['D']}, {best['hold']} дн., стоп +{int(best['S'] * 100)}%, позиция {best['e']:g}×, до {best['M']} поз.")
    for p, (a, b) in PERIODS.items():
        x, y = window_stats(hours, eq, ms(a), ms(b)), window_stats(hours2, eq2, ms(a), ms(b))
        print(f"  {p}: типичн. {x['geo']:+5.1f}% (итог {x['total']:+.0f}%) | без 5 лучших сделок ({', '.join(top5)}): "
              f"типичн. {y['geo']:+5.1f}% (итог {y['total']:+.0f}%)")
    json.dump(rows, open(os.path.join(HERE, "results", "listing_port.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
