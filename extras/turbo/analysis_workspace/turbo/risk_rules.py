"""Турбо: правила, которые снижают риск потерять прибыль, сохраняя максимум доходности.

Без новых прогонов: почасовые кривые капитала всех 160 месячных ставок варианта 15 («Умный ва-банк»,
backtests/turbo/v15_<старт>) + рыночные признаки BTC (часовые свечи и ставка финансирования Binance).

Правила входа (на момент старта ставки): тренд BTC (выше средней N дней), волатильность BTC (7 дней к обычной),
перегрев плеча (средняя ставка финансирования за 3 дня), падение BTC за 7 дней.
Правила выхода (по часам внутри ставки): цель ×T, выход при обвале BTC (−X% за N часов, выход с часовой задержкой),
защита прибыли (после ×A выход при откате на G от пика), стоп ставки (капитал ≤ S).

Отбор — на 2022–2024, проверка — на 2025–2026 (отбор этих лет не видел).
  venv\\Scripts\\python turbo\\risk_rules.py
Итог: turbo/results/risk_rules.json и таблицы в консоли.
"""
import glob
import itertools
import json
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RES = os.path.join(HERE, "results")
BURN = 0.10
GIVEBACK_AT = 1.30          # «была прибыль»: капитал хоть раз ≥ ×1,3
SPLIT = "2025-01-01"        # отбор до этой даты, проверка — после


def http():
    s = requests.Session()
    s.trust_env = False
    return s


def btc_hourly():
    path = os.path.join(RES, "btc_1h.json")
    if os.path.exists(path):
        d = json.load(open(path))
    else:
        s, d, t = http(), {}, int(datetime(2021, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        end = int(time.time() * 1000)
        while t < end:
            rows = s.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": "BTCUSDT", "interval": "1h", "startTime": t, "limit": 1500}, timeout=20).json()
            if not rows:
                break
            for r in rows:
                d[str(r[0])] = float(r[4])
            t = rows[-1][0] + 3_600_000
        json.dump(d, open(path, "w"))
    ser = pd.Series({int(k): v for k, v in d.items()}).sort_index()
    ser.index = pd.to_datetime(ser.index, unit="ms", utc=True).tz_localize(None)
    return ser


def btc_funding():
    path = os.path.join(RES, "btc_funding.json")
    if os.path.exists(path):
        d = json.load(open(path))
    else:
        s, d, t = http(), {}, int(datetime(2021, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
        end = int(time.time() * 1000)
        while t < end:
            rows = s.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": "BTCUSDT", "startTime": t, "limit": 1000}, timeout=20).json()
            if not rows:
                break
            for r in rows:
                d[str(r["fundingTime"])] = float(r["fundingRate"])
            t = rows[-1]["fundingTime"] + 1
        json.dump(d, open(path, "w"))
    ser = pd.Series({int(k): v for k, v in d.items()}).sort_index()
    ser.index = pd.to_datetime(ser.index, unit="ms", utc=True).tz_localize(None)
    return ser


def episodes():
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "backtests", "turbo", "v15_*"))):
        start = os.path.basename(path)[4:]
        runs = sorted(glob.glob(os.path.join(path, "*", "*")), key=os.path.getmtime)
        for r in reversed(runs):
            f = os.path.join(r, "balance_and_equity.csv.gz")
            if os.path.exists(f):
                df = pd.read_csv(f)
                t = pd.to_datetime(df.iloc[:, 0], utc=True).dt.tz_localize(None)
                eq = df["usd_total_equity"].astype(float).values
                out.append({"start": start, "t": t.values, "rel": eq / eq[0]})
                break
    return out


def entry_features(btc, fund, start):
    d = pd.Timestamp(start)
    h = btc[btc.index < d]
    daily = h.resample("1D").last().dropna()
    ret = np.log(h).diff().dropna()
    vol7 = ret.iloc[-168:].std()
    roll = ret.rolling(168).std().iloc[-24 * 90:]
    f3 = fund[(fund.index < d) & (fund.index >= d - pd.Timedelta(days=3))]
    return {"sma": {n: bool(daily.iloc[-1] > daily.iloc[-n:].mean()) for n in (50, 100, 200)},
            "vol_ratio": float(vol7 / roll.median()), "ret7": float(h.iloc[-1] / h.iloc[-169] - 1),
            "fund3": float(f3.mean()) if len(f3) else 0.0}


def crash_flags(btc, t, hours, drop):
    """По часам ставки: BTC упал на drop от максимума за последние hours часов."""
    idx = pd.DatetimeIndex(t)
    s = btc.reindex(btc.index.union(idx)).ffill()
    roll_max = s.rolling(hours, min_periods=1).max()
    return ((s / roll_max - 1) <= -drop).reindex(idx).fillna(False).values


def outcome(rel, crash, target, trail, stop):
    """Итог ставки, вид исхода, пик. Выход по обвалу — с задержкой в час (не лучше следующей точки)."""
    peak, floor = 1.0, stop
    n = len(rel)
    for i in range(n):
        x = rel[i]
        peak = max(peak, x)
        if x <= BURN:
            return x - 1, "burn", peak
        if target and x >= target:
            return target - 1, "target", peak
        if trail and peak >= trail[0]:
            floor = max(floor or 0.0, peak * (1 - trail[1]))
        if floor and x <= floor:
            return x - 1, "protect" if trail and peak >= trail[0] else "stop", peak
        if crash is not None and crash[i]:
            y = min(x, rel[i + 1]) if i + 1 < n else x
            return y - 1, "crash", peak
    last = rel[-1]
    if n < 30 * 24 * 0.95 and last < 0.5:          # движок оборвал прогон — ликвидация
        return -0.95, "burn", peak
    return last - 1, "end", peak


def summarize(items):
    res = [r for r, *_ in items]
    n = len(res)
    if n == 0:
        return None
    kinds = [k for _, k, _ in items]
    had = [(r, p) for r, _, p in items if p >= GIVEBACK_AT]
    g = lambda f: sum(math.log(max(1e-9, 1 + f * r)) for r in res) / n  # noqa: E731
    return {"bets": n, "p_target": round(kinds.count("target") / n * 100, 1),
            "p_burn": round(kinds.count("burn") / n * 100, 1),
            "p_loss50": round(sum(r <= -0.5 for r in res) / n * 100, 1),
            "p_giveback": round(sum(r < 0 for r, _ in had) / len(had) * 100, 1) if had else 0.0,
            "mean_bet": round(sum(res) / n * 100, 1), "median_bet": round(float(np.median(res)) * 100, 1),
            "worst": round(min(res) * 100, 1), "growth_25": round((math.exp(g(0.25)) - 1) * 100, 2)}


def main():
    btc, fund, eps = btc_hourly(), btc_funding(), episodes()
    print(f"ставок: {len(eps)} ({eps[0]['start']} — {eps[-1]['start']}), BTC часов: {len(btc)}, ставок финансирования: {len(fund)}")
    for e in eps:
        e["f"] = entry_features(btc, fund, e["start"])
    crashes = [None, (4, 0.03), (4, 0.05), (12, 0.05), (24, 0.07), (24, 0.10)]
    flags = {c: [crash_flags(btc, e["t"], *c) if c else None for e in eps] for c in crashes}
    targets = [None, 1.5, 1.75, 2.0, 2.5]
    trails = [None, (1.3, 0.2), (1.3, 0.3), (1.5, 0.2), (1.5, 0.3)]
    stops = [None, 0.7, 0.6]
    exits = list(itertools.product(targets, crashes, trails, stops))
    per = {}
    for ex in exits:
        target, crash, trail, stop = ex
        if trail and target and target <= trail[0]:
            continue
        per[ex] = [outcome(e["rel"], flags[crash][i], target, trail, stop) for i, e in enumerate(eps)]
    print(f"правил выхода: {len(per)}")
    entries = list(itertools.product([None, 50, 100, 200], [None, 1.5, 2.0], [None, 0.0003, 0.0005], [None, -0.08]))

    def take(e, en):
        sma, vol, fnd, r7 = en
        f = e["f"]
        return ((not sma or f["sma"][sma]) and (not vol or f["vol_ratio"] <= vol)
                and (fnd is None or f["fund3"] <= fnd) and (r7 is None or f["ret7"] >= r7))

    rows = []
    for en in entries:
        mask = [take(e, en) for e in eps]
        for ex, res in per.items():
            tr = [res[i] for i, e in enumerate(eps) if mask[i] and e["start"] < SPLIT]
            te = [res[i] for i, e in enumerate(eps) if mask[i] and e["start"] >= SPLIT]
            a, b = summarize(tr), summarize(te)
            if not a or not b or a["bets"] < 25 or b["bets"] < 10:
                continue
            rows.append({"entry": en, "exit": ex, "train": a, "test": b,
                         "all": summarize(tr + te), "months_train": sum(e["start"] < SPLIT for e in eps),
                         "months_test": sum(e["start"] >= SPLIT for e in eps)})
    json.dump(rows, open(os.path.join(RES, "risk_rules.json"), "w"), ensure_ascii=False, default=str)

    def label(r):
        (sma, vol, fnd, r7), (target, crash, trail, stop) = r["entry"], r["exit"]
        parts = [f"цель ×{target}" if target else "без цели"]
        parts += [f"тренд SMA{sma}"] if sma else []
        parts += [f"волатильность ≤{vol}×"] if vol else []
        parts += [f"финансирование ≤{fnd * 100:.2f}%"] if fnd is not None else []
        parts += ["BTC за 7 дн. ≥−8%"] if r7 is not None else []
        parts += [f"выход при обвале BTC −{crash[1] * 100:.0f}% за {crash[0]} ч"] if crash else []
        parts += [f"защита прибыли: после ×{trail[0]} выход при откате {trail[1] * 100:.0f}% от пика"] if trail else []
        parts += [f"стоп ставки ×{stop}"] if stop else []
        return ", ".join(parts)

    def line(tag, s, months):
        return (f"{tag}: ставок {s['bets']}/{months} | ×цель {s['p_target']:4.1f}% | сгорело {s['p_burn']:4.1f}% | "
                f"потеря прибыли {s['p_giveback']:4.1f}% | −50%+ {s['p_loss50']:4.1f}% | ср. ставка {s['mean_bet']:+6.1f}% | "
                f"медиана {s['median_bet']:+6.1f}% | худшая {s['worst']:+6.1f}% | рост при 25% {s['growth_25']:+5.2f}%/мес")

    def show(title, rs, k=6):
        print(f"\n=== {title}")
        for r in rs[:k]:
            print("  " + label(r))
            print("     " + line("отбор 2022–24", r["train"], r["months_train"]))
            print("     " + line("ПРОВЕРКА 25–26", r["test"], r["months_test"]))

    base = [r for r in rows if r["entry"] == (200, None, None, None) and r["exit"] == (2.0, None, None, None)]
    show("СЕЙЧАС («Умный ва-банк»: цель ×2, тренд SMA200)", base)
    best_growth = sorted(rows, key=lambda r: -r["train"]["growth_25"])
    show("Лучшие по росту капитала (отбор по 2022–24)", best_growth)
    b = base[0]["train"] if base else None
    safer = [r for r in rows if b and r["train"]["p_giveback"] <= b["p_giveback"] * 0.6
             and r["train"]["p_burn"] <= b["p_burn"] * 0.6 and r["train"]["growth_25"] >= b["growth_25"]]
    show("Не хуже по росту, но риск потерять прибыль и сгореть ниже на 40%+ (отбор по 2022–24)",
         sorted(safer, key=lambda r: -r["train"]["growth_25"]))
    print("\nГотово:", os.path.join(RES, "risk_rules.json"))


if __name__ == "__main__":
    main()
