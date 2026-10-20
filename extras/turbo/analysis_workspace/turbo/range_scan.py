"""Отскок от границ диапазона в боковике — проверка идеи вне passivbot (пользователь, 01.10.2026: «торговля в боковике,
верный отход от границ уровней, больше сделок»). Часовые свечи Binance USDⓈ-M (публичные данные), 35 монет Turbo.

Правила (на свече t — только то, что известно к её началу):
  границы — максимум/минимум цены за N часов; середина — их среднее; ATR(14);
  боковик — ширина диапазона (верх − низ) / середина не больше W (W=0 — без фильтра);
  лонг — лимитный вход на нижней границе (если свеча её касается; открытие ниже — по открытию);
  шорт (если включён) — на верхней границе;
  выход — цель: середина (или 75% пути к противоположной границе), стоп: граница ∓ k·ATR, или через T часов по закрытию.
  Одна позиция на монету; позиций одновременно не больше M; каждая — e × капитал (M·e ≤ 10 — плечо 10×).
  Стоп и цель в одной свече — считается стоп (осторожно). Расходы — 0,08% на сторону (комиссия + проскальзывание).
Отбор — I пол. 2026, проверка — II пол. 2026 и обвал 10.10.2025 (15.09–15.11.2025). На часовых свечах проскальзывание
стопа в минутный обвал занижено.

    venv\\Scripts\\python turbo\\range_scan.py
Итог: turbo/results/range_scan.json; свечи — turbo/results/ohlc_1h_2025-08.npz.
"""
import itertools
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_algos as CA  # noqa: E402
import risk_rules as RR  # noqa: E402

CACHE = os.path.join(HERE, "results", "ohlc_1h_2025-08.npz")
COST = 0.0008
PERIODS = {"I пол. 2026 (отбор)": ("2026-01-01", "2026-07-01"), "II пол. 2026 (проверка)": ("2026-07-01", "2026-09-30"),
           "обвал 10.10.2025": ("2025-09-15", "2025-11-15")}


def ohlc():
    """Часовые свечи 35 монет с 01.08.2025: (время [ms], массивы o, h, l, c формы [часы, монеты]), монеты."""
    if os.path.exists(CACHE):
        z = np.load(CACHE, allow_pickle=True)
        return z["t"], z["o"], z["h"], z["l"], z["c"], list(z["coins"])
    s, frames = RR.http(), {}
    t0 = int(datetime(2025, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)
    t1 = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
    for c in CA.MC.default_coins():
        t, rows = t0, []
        while t < t1:
            r = s.get("https://fapi.binance.com/fapi/v1/klines",
                      params={"symbol": f"{c}USDT", "interval": "1h", "startTime": t, "limit": 1500}, timeout=20).json()
            if not isinstance(r, list) or not r:
                break
            rows += [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r]
            t = r[-1][0] + 3_600_000
            time.sleep(0.05)
        if rows:
            frames[c] = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"]).drop_duplicates("t").set_index("t")
    idx = sorted(set().union(*[f.index for f in frames.values()]))
    coins = sorted(frames)
    arr = {k: np.full((len(idx), len(coins)), np.nan) for k in "ohlc"}
    for j, c in enumerate(coins):
        f = frames[c].reindex(idx)
        for k in "ohlc":
            arr[k][:, j] = f[k].values
    np.savez(CACHE, t=np.array(idx), coins=np.array(coins), **arr)
    return np.array(idx), arr["o"], arr["h"], arr["l"], arr["c"], coins


def indicators(h, l, c, n):
    """Границы за n часов, ATR(14) — на начало каждой свечи (сдвиг на 1)."""
    H, Lw, C = pd.DataFrame(h), pd.DataFrame(l), pd.DataFrame(c)
    up = H.rolling(n, min_periods=n).max().shift(1).values
    lo = Lw.rolling(n, min_periods=n).min().shift(1).values
    tr = np.maximum(H - Lw, np.maximum((H - C.shift(1)).abs(), (Lw - C.shift(1)).abs()))
    atr = tr.rolling(14, min_periods=14).mean().shift(1).values
    return up, lo, atr


def simulate(t, o, h, l, c, ind, p, a, b):
    """Портфель с a по b (индексы часов): поминутно нет — по часам; капитал по закрытиям, компаундинг."""
    up, lo, atr = ind
    mid = (up + lo) / 2
    n_coins = c.shape[1]
    cap, eq = 1.0, np.empty(b - a)
    pos = {}                                   # монета → (направление, цена входа, номинал, цель, стоп, час входа)
    trades = 0
    for k, i in enumerate(range(a, b)):
        for j in list(pos):
            d, px, notional, tp, sl, ti = pos[j]
            if np.isnan(c[i, j]):
                continue
            ex = None
            if d > 0:
                if l[i, j] <= sl:
                    ex = min(sl, o[i, j])
                elif h[i, j] >= tp:
                    ex = max(tp, o[i, j])
            else:
                if h[i, j] >= sl:
                    ex = max(sl, o[i, j])
                elif l[i, j] <= tp:
                    ex = min(tp, o[i, j])
            if ex is None and i - ti >= p["T"]:
                ex = c[i, j]
            if ex is not None:
                cap += notional * (d * (ex / px - 1) - 2 * COST)
                del pos[j]
                trades += 1
        for j in range(n_coins):
            if len(pos) >= p["M"] or j in pos or np.isnan(up[i, j]) or np.isnan(atr[i, j]) or np.isnan(c[i, j]):
                continue
            width = (up[i, j] - lo[i, j]) / mid[i, j]
            if p["W"] and width > p["W"]:
                continue
            notional = p["e"] * cap
            if l[i, j] <= lo[i, j]:
                px = min(lo[i, j], o[i, j])
                tp = px + p["tp"] * (up[i, j] - px) if p["tp"] < 1 else mid[i, j]
                sl = lo[i, j] - p["k"] * atr[i, j]
                if l[i, j] <= sl:                                       # та же свеча дошла до стопа
                    cap += notional * ((min(sl, px) / px - 1) - 2 * COST)
                    trades += 1
                else:
                    pos[j] = (1, px, notional, max(tp, px * 1.001), sl, i)
            elif p["short"] and h[i, j] >= up[i, j]:
                px = max(up[i, j], o[i, j])
                tp = px - p["tp"] * (px - lo[i, j]) if p["tp"] < 1 else mid[i, j]
                sl = up[i, j] + p["k"] * atr[i, j]
                if h[i, j] >= sl:
                    cap += notional * (-(max(sl, px) / px - 1) - 2 * COST)
                    trades += 1
                else:
                    pos[j] = (-1, px, notional, min(tp, px * 0.999), sl, i)
        unreal = sum(nt * d * (c[i, j] / px - 1) for j, (d, px, nt, _, _, _) in pos.items() if not np.isnan(c[i, j]))
        eq[k] = max(cap + unreal, 1e-6)
        if cap + unreal <= 0.02:
            eq[k:] = 1e-6
            break
    return eq, trades


def stats(eq, hours_per_day=24):
    """Типичный 30-дневный рост (геом. по стартам каждого дня), итог, худшая просадка, худшие 30 дней."""
    d = eq[::hours_per_day]
    m = d[30:] / d[:-30] if len(d) > 30 else np.array([eq[-1] / eq[0]])
    geo = math.exp(float(np.mean(np.log(np.maximum(m, 1e-9))))) - 1
    return {"geo": round(geo * 100, 1), "total": round((eq[-1] / eq[0] - 1) * 100, 1),
            "dd": round(float((1 - eq / np.maximum.accumulate(eq)).max()) * 100, 1),
            "worst30": round((float(m.min()) - 1) * 100, 1), "p_loss": round(float((m < 1).mean()) * 100, 1)}


def main():
    t, o, h, l, c, coins = ohlc()
    print(f"свечи: {len(coins)} монет, {len(t)} часов, "
          f"{datetime.fromtimestamp(t[0] / 1000, timezone.utc):%d.%m.%Y}–{datetime.fromtimestamp(t[-1] / 1000, timezone.utc):%d.%m.%Y}", flush=True)
    grid = [dict(N=N, W=W, tp=tp, k=k, T=T, short=sh, M=10, e=1.0)
            for N, W, tp, k, T, sh in itertools.product((24, 48, 96), (0, 0.06, 0.12), (1.0, 0.75), (1.0, 2.0), (24, 72), (False, True))]
    ind = {N: indicators(h, l, c, N) for N in (24, 48, 96)}
    span = {name: (int(np.searchsorted(t, int(pd.Timestamp(a_, tz="UTC").value // 1_000_000))),
                   int(np.searchsorted(t, int(pd.Timestamp(b_, tz="UTC").value // 1_000_000))))
            for name, (a_, b_) in PERIODS.items()}
    rows = []
    t_start = time.time()
    for q, p in enumerate(grid):
        row = {"params": p}
        for name, (a, b) in span.items():
            eq, n = simulate(t, o, h, l, c, ind[p["N"]], p, a, b)
            row[name] = {**stats(eq), "trades_day": round(n / max(1, (b - a) / 24), 1)}
        rows.append(row)
        if q % 24 == 0:
            print(f"  {q + 1}/{len(grid)} наборов, {time.time() - t_start:.0f} с", flush=True)
    json.dump(rows, open(os.path.join(HERE, "results", "range_scan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    P = list(PERIODS)
    both = [r for r in rows if r[P[0]]["geo"] > 0 and r[P[1]]["geo"] > 0]
    print(f"\nНаборов: {len(rows)}; в плюсе и в отборе (I пол.), и в проверке (II пол.): {len(both)}\n"
          "геом. — типичный 30-дневный рост; плечо: до 10 позиций по 1× капитала\n")
    for r in sorted(rows, key=lambda r: -r[P[0]]["geo"])[:15]:
        p, x, y, z = r["params"], r[P[0]], r[P[1]], r[P[2]]
        print(f"N={p['N']:>2} ч, боковик ≤{p['W'] * 100:>2.0f}%, цель {'середина' if p['tp'] == 1 else '75%'}, стоп {p['k']:g}·ATR, "
              f"≤{p['T']} ч, {'лонг+шорт' if p['short'] else 'лонг   '} | I: {x['geo']:+5.1f}%/мес, просадка {x['dd']:5.1f}%, "
              f"сделок/день {x['trades_day']:5.1f} | II: {y['geo']:+5.1f}%/мес, просадка {y['dd']:5.1f}% | обвал 2025: {z['geo']:+5.1f}%/мес, "
              f"просадка {z['dd']:5.1f}%")


def simulate_breakout(t, o, h, l, c, ind, p, a, b):
    """Обратная идея: вход на пробое границы диапазона (стоп-ордер на верхней границе — лонг, на нижней — шорт, если
    включён); выход — цель на m ширин диапазона от входа, стоп — середина диапазона, или через T часов по закрытию.
    Остальное — как simulate (одна позиция на монету, до M позиций по e × капитал, стоп раньше цели в одной свече)."""
    up, lo, atr = ind
    mid = (up + lo) / 2
    n_coins = c.shape[1]
    cap, eq = 1.0, np.empty(b - a)
    pos = {}
    trades = 0
    for k, i in enumerate(range(a, b)):
        for j in list(pos):
            d, px, notional, tp, sl, ti = pos[j]
            if np.isnan(c[i, j]):
                continue
            ex = None
            if d > 0:
                if l[i, j] <= sl:
                    ex = min(sl, o[i, j])
                elif h[i, j] >= tp:
                    ex = max(tp, o[i, j])
            else:
                if h[i, j] >= sl:
                    ex = max(sl, o[i, j])
                elif l[i, j] <= tp:
                    ex = min(tp, o[i, j])
            if ex is None and i - ti >= p["T"]:
                ex = c[i, j]
            if ex is not None:
                cap += notional * (d * (ex / px - 1) - 2 * COST)
                del pos[j]
                trades += 1
        for j in range(n_coins):
            if len(pos) >= p["M"] or j in pos or np.isnan(up[i, j]) or np.isnan(c[i, j]):
                continue
            rng = up[i, j] - lo[i, j]
            if p["W"] and rng / mid[i, j] > p["W"]:
                continue
            notional = p["e"] * cap
            if h[i, j] >= up[i, j]:
                px = max(up[i, j], o[i, j])
                tp, sl = px + p["m"] * rng, mid[i, j]
                if l[i, j] <= sl:
                    cap += notional * ((min(sl, px) / px - 1) - 2 * COST)
                    trades += 1
                else:
                    pos[j] = (1, px, notional, tp, sl, i)
            elif p["short"] and l[i, j] <= lo[i, j]:
                px = min(lo[i, j], o[i, j])
                tp, sl = px - p["m"] * rng, mid[i, j]
                if h[i, j] >= sl:
                    cap += notional * (-(max(sl, px) / px - 1) - 2 * COST)
                    trades += 1
                else:
                    pos[j] = (-1, px, notional, tp, sl, i)
        unreal = sum(nt * d * (c[i, j] / px - 1) for j, (d, px, nt, _, _, _) in pos.items() if not np.isnan(c[i, j]))
        eq[k] = max(cap + unreal, 1e-6)
        if cap + unreal <= 0.02:
            eq[k:] = 1e-6
            break
    return eq, trades


def breakout_main():
    t, o, h, l, c, coins = ohlc()
    grid = [dict(N=N, W=W, m=m, T=T, short=sh, M=10, e=e)
            for N, W, m, T, sh, e in itertools.product((24, 48, 96), (0, 0.06, 0.12), (0.5, 1.0, 2.0), (24, 72), (False, True), (0.3,))]
    ind = {N: indicators(h, l, c, N) for N in (24, 48, 96)}
    span = {name: (int(np.searchsorted(t, int(pd.Timestamp(a_, tz="UTC").value // 1_000_000))),
                   int(np.searchsorted(t, int(pd.Timestamp(b_, tz="UTC").value // 1_000_000))))
            for name, (a_, b_) in PERIODS.items()}
    rows = []
    for p in grid:
        row = {"params": p}
        for name, (a, b) in span.items():
            eq, n = simulate_breakout(t, o, h, l, c, ind[p["N"]], p, a, b)
            row[name] = {**stats(eq), "trades_day": round(n / max(1, (b - a) / 24), 1)}
        rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "breakout_scan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    P = list(PERIODS)
    both = [r for r in rows if r[P[0]]["geo"] > 0 and r[P[1]]["geo"] > 0]
    print(f"Пробой: наборов {len(rows)}; в плюсе и в I, и во II пол.: {len(both)} (до 10 позиций по 0,3× капитала)\n")
    for r in sorted(rows, key=lambda r: -min(r[P[0]]["geo"], r[P[1]]["geo"]))[:12]:
        p, x, y, z = r["params"], r[P[0]], r[P[1]], r[P[2]]
        print(f"N={p['N']:>2} ч, сжатие ≤{p['W'] * 100:>2.0f}%, цель {p['m']:g} ширины, ≤{p['T']} ч, {'лонг+шорт' if p['short'] else 'лонг   '} | "
              f"I: {x['geo']:+5.1f}%/мес, просадка {x['dd']:5.1f}%, сделок/день {x['trades_day']:5.1f} | II: {y['geo']:+5.1f}%/мес, "
              f"просадка {y['dd']:5.1f}% | обвал 2025: {z['geo']:+5.1f}%/мес, просадка {z['dd']:5.1f}%")


if __name__ == "__main__":
    breakout_main() if "--breakout" in sys.argv else main()
