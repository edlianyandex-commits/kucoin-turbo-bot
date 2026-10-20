"""Turbo 3.0: не брать альткоины, которые падают после пампа.

На старте ставки монета исключается, если за последние L дней у неё был памп — рост за 7 дней на P и больше —
и сейчас цена ниже максимума этих L дней на D и больше. Остальные монеты выбирает passivbot, список держится до
конца ставки. Ставки — правила 3.0 (цель ×2, стоп ставки ×0,7, выход при обвале BTC), капитал поминутно.
Проверка в обеих половинах 2026: I — старты 01.01…26.06 каждые 5 дней, II — 01.07…30.08 каждые 2 дня.

    venv\\Scripts\\python turbo\\pump_filter.py [--workers 6]
Итог: turbo/results/pump_filter.json; цены монет — turbo/results/coin_prices_1h_from_2025-11.json.
"""
import argparse
import hashlib
import itertools
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_algos as CA  # noqa: E402
import coin_pick as CP  # noqa: E402
import risk_rules as RR  # noqa: E402

PRICES = os.path.join(CA.RES, "coin_prices_1h_from_2025-11.json")
H1 = CP.grid("2026-01-01", "2026-06-26", step=5)
H2 = CA.BET_STARTS
VARIANTS = [(L, P, D) for L, P, D in itertools.product((14, 30), (0.3, 0.5), (0.1, 0.2))]


def prices():
    """Часовые закрытия 35 монет с 01.11.2025 (Binance USDⓈ-M, публичные данные)."""
    if os.path.exists(PRICES):
        d = json.load(open(PRICES))
    else:
        s, d = RR.http(), {}
        t_end = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
        for c in CA.MC.default_coins():
            t, rows_all = int(datetime(2025, 11, 1, tzinfo=timezone.utc).timestamp() * 1000), {}
            while t < t_end:
                rows = s.get("https://fapi.binance.com/fapi/v1/klines",
                             params={"symbol": f"{c}USDT", "interval": "1h", "startTime": t, "limit": 1500}, timeout=20).json()
                if not isinstance(rows, list) or not rows:
                    break
                for r in rows:
                    rows_all[str(r[0])] = float(r[4])
                t = rows[-1][0] + 3_600_000
                time.sleep(0.05)
            d[c] = rows_all
        json.dump(d, open(PRICES, "w"))
    df = pd.DataFrame({c: pd.Series({int(k): v for k, v in rows.items()}) for c, rows in d.items() if rows}).sort_index()
    df.index = pd.to_datetime(df.index, unit="ms")
    return df


def excluded(px, start, L, P, D):
    """Монеты, которые падают после пампа на момент старта ставки (только данные до старта)."""
    h = px[px.index < pd.Timestamp(start)]
    daily = h.resample("1D").last().iloc[-(L + 7):]
    out = []
    for c in h.columns:
        col = daily[c].dropna()
        if len(col) < L + 7:
            continue
        r7 = col / col.shift(7) - 1
        pumped = r7.iloc[-L:].max() >= P
        peak = h[c].iloc[-24 * L:].max()
        if pumped and h[c].iloc[-1] <= (1 - D) * peak:
            out.append(c)
    return out


def key(coins):
    return "pump_" + ("all" if coins is None else hashlib.md5(",".join(coins).encode()).hexdigest()[:10])


def run_bet(start, coins):
    path = os.path.join(CA.CURVES, key(coins), f"{start}.npy")
    if not os.path.exists(path):
        res = CA.DS.backtest_curve(dict(CA.PW.ENGINE3), start, CP.end_of(start), f"pf_{key(coins)}_{start}", coins=coins)
        if isinstance(res, str):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.save(path, res[0])
        CA.shutil.rmtree(res[1], ignore_errors=True)
    return np.load(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    px, btc = prices(), RR.btc_hourly()
    allc = CA.MC.default_coins()
    plans = {"все монеты (3.0 сейчас)": {s: None for s in H1 + H2}}
    for L, P, D in VARIANTS:
        plans[f"памп ≥{P * 100:.0f}% за 7 дн. в последние {L} дн., сейчас ≤ −{D * 100:.0f}% от вершины"] = {
            s: (sorted(set(allc) - set(ex)) if (ex := excluded(px, s, L, P, D)) else None) for s in H1 + H2}
    jobs = sorted({(s, tuple(c) if c else None) for p in plans.values() for s, c in p.items()}, key=str)
    with ThreadPoolExecutor(a.workers) as ex:
        curves = dict(zip(jobs, ex.map(lambda j: run_bet(j[0], list(j[1]) if j[1] else None), jobs)))
    out = {}
    for name, plan in plans.items():
        per = {"I пол. 2026": [], "II пол. 2026": []}
        cut = []
        for s, c in plan.items():
            rel = curves[(s, tuple(c) if c else None)]
            if rel is None:
                continue
            per["I пол. 2026" if s in H1 else "II пол. 2026"].append(CP.bet(rel, s, btc))
            cut.append(len(allc) - len(c) if c else 0)
        out[name] = {k: CP.summary(v) for k, v in per.items()}
        out[name]["avg_excluded"] = round(float(np.mean(cut)), 1)
        out[name]["share_bets_changed"] = round(float(np.mean([x > 0 for x in cut])) * 100, 1)
    json.dump(out, open(os.path.join(CA.RES, "pump_filter.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    base = out["все монеты (3.0 сейчас)"]
    for name, v in out.items():
        a_, b_ = v["I пол. 2026"], v["II пол. 2026"]
        better = name != "все монеты (3.0 сейчас)" and a_["mean"] > base["I пол. 2026"]["mean"] and b_["mean"] > base["II пол. 2026"]["mean"]
        print(f"{'★' if better else ' '} {name:<62} отсекает в среднем {v['avg_excluded']:4.1f} монет, меняет {v['share_bets_changed']:5.1f}% ставок\n"
              f"     I пол.: ср. {a_['mean']:+6.1f}%, ×2 {a_['p_target']:4.1f}%, в минусе {a_['p_loss']:4.1f}%, рост {a_['growth_25']:+5.2f} | "
              f"II пол.: ср. {b_['mean']:+6.1f}%, ×2 {b_['p_target']:4.1f}%, в минусе {b_['p_loss']:4.1f}%, рост {b_['growth_25']:+5.2f}")
    print("★ — лучше «все монеты» в обеих половинах 2026; рост — капитала при ставке 25%, % за ставку")


if __name__ == "__main__":
    main()
