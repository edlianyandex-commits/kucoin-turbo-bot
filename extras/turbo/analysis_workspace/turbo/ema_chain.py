"""Turbo на движке EMA Anchor без цели ×2: ставки подряд, вся прибыль в обороте (turbo/engine_month.py нашёл,
что EMA Anchor с экспозицией 20–30 и стопом −30% растёт быстрее движка 3.0 и пережил обвал 10.10.2025).

Прогон passivbot ($200, 30 дней, капитал поминутно) со старта каждого дня: 2026-01-01…2026-09-28 и 2025-09-10…2025-11-08
(стресс: обвал 10.10.2025). Дальше — month_paths.Engine: ставка → выход по правилу → следующая со следующего дня
на весь капитал; без цели ставка идёт 30 дней (длина прогона), затем новая — стоп −30% пересчитывается от нового капитала.
Окна: 30 и 90 дней со старта каждого дня.

    venv\\Scripts\\python turbo\\ema_chain.py [--workers 8]
Итог: turbo/results/ema_chain.json; кривые — turbo/results/engine_daily/<вариант>/<день>.npy.
"""
import argparse
import json
import math
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import engine_month as EM  # noqa: E402
import month_paths as MP  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

OUT = os.path.join(HERE, "results", "engine_daily")
KEYS = ("ema20", "ema30", "tm20")
DAYS = CP.grid("2025-09-10", "2025-11-08", 1) + CP.grid("2026-01-01", "2026-09-28", 1)
WINDOWS = {"I пол. 2026": ("2026-01-01", "2026-05-31", "2026-03-31"), "II пол. 2026": ("2026-07-01", "2026-08-31", None),
           "обвал 10.10.2025": ("2025-09-10", "2025-10-10", None)}       # (первый старт, последний 30-дн., последний 90-дн.)
RULES = {
    "ставка ×2, стоп −30% (как 3.0)": {"mode": "target", "T": 2.0, "S": 0.7},
    "без цели, стоп −30% от старта ставки": {"mode": "target", "T": None, "S": 0.7},
    "без цели, стоп −20% от старта ставки": {"mode": "target", "T": None, "S": 0.8},
    "без цели, стоп −40% от старта ставки": {"mode": "target", "T": None, "S": 0.6},
    "без цели, стоп −30% от максимума": {"mode": "trail", "x": 0.3, "S": 0.7},
    "без цели, без стопа (только обвал BTC)": {"mode": "none"},
}


def build_one(key, day):
    path = os.path.join(OUT, key, f"{day}.npy")
    if os.path.exists(path):
        return day, None, "cached"
    _, over, base = EM.VARIANTS[key]
    end = (datetime.fromisoformat(day) + timedelta(days=30)).strftime("%Y-%m-%d")
    end = min(end, "2026-09-30") if day >= "2026" else end
    res = DS.backtest_curve({**PW.ENGINE3, **over}, day, end, f"ed_{key}_{day}", base=base)
    if isinstance(res, str):
        return day, None, res
    rel, run_dir = res
    panic = None
    try:
        import glob
        f = pd.read_csv(sorted(glob.glob(os.path.join(run_dir, "*", "*", "fills.csv")))[-1])
        p = f[f["type"] == "close_panic_long"]
        if len(p):
            m = int((pd.Timestamp(p["timestamp"].iloc[0]) - pd.Timestamp(day)).total_seconds() // 60)
            panic = m if m < len(rel) - 5 else None
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.save(path, rel)
    return day, panic, "ok"


def build(key, workers):
    pf = os.path.join(OUT, f"{key}_panic.json")
    panic = json.load(open(pf)) if os.path.exists(pf) else {}
    todo = [d for d in DAYS if not os.path.exists(os.path.join(OUT, key, f"{d}.npy"))]
    with ThreadPoolExecutor(workers) as ex:
        for day, p, st in ex.map(lambda d: build_one(key, d), todo):
            if st == "ok":
                panic[day] = p
            elif st != "cached":
                print("  ошибка", key, day, st[:150], flush=True)
    json.dump(panic, open(pf, "w"))
    return panic


def bets(key, panic, btc):
    rel = {d: np.load(os.path.join(OUT, key, f"{d}.npy")) for d in DAYS if os.path.exists(os.path.join(OUT, key, f"{d}.npy"))}
    return SimpleNamespace(rel=rel, panic=panic, horizon=30,
                           crash={d: DR.crash_minutes(btc, d, len(r), 12, 0.05) for d, r in rel.items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if not all(os.path.exists(os.path.join(OUT, "tm20", f"{d}.npy")) for d in DAYS if d >= "2026"):
        os.makedirs(os.path.join(OUT, "tm20"), exist_ok=True)        # движок 3.0: кривые $200 из chain_rules
        for d in DAYS:
            src = os.path.join(HERE, "results", "daily_curves", f"{d}.npy")
            if d >= "2026" and os.path.exists(src):
                shutil.copy(src, os.path.join(OUT, "tm20", f"{d}.npy"))
        pf = os.path.join(OUT, "tm20_panic.json")
        if not os.path.exists(pf):
            json.dump(json.load(open(os.path.join(HERE, "results", "daily_panic.json"))), open(pf, "w"))
    btc = RR.btc_hourly()
    rows = []
    for key in KEYS:
        E = MP.Engine(DS.COST, bets(key, build(key, a.workers), btc))
        for rname, rule in RULES.items():
            row = {"key": key, "engine": EM.VARIANTS[key][0], "rule": rname}
            for w, (first, last30, last90) in WINDOWS.items():
                m30 = [E.month(s, rule)[0] for s in CP.grid(first, last30, 1)]
                row[w] = MP.stats(m30)
                if last90:
                    m90 = np.array([E.month(s, rule, 90)[0] for s in CP.grid(first, last90, 1)])
                    row[w]["q_geo"] = round(math.exp(float(np.mean(np.log(np.maximum(m90, 1e-6))))), 2)
                    row[w]["q_x2"] = round(float((m90 >= 2).mean()) * 100, 1)
                    row[w]["q_worst"] = round(float(m90.min()), 2)
            rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "ema_chain.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("Ставки подряд, вся прибыль в обороте, $200; месяц = 30 дней со старта каждого дня (90 дн. — квартал)\n")
    for r in rows:
        print(f"{r['engine'][:24]:<24} {r['rule'][:38]:<38} | " + " | ".join(
            f"{w.split()[0]:>5}: мес. мед ×{x['median']:.2f} геом ×{x['geo']:.2f} ×2 {x['p_x2']:4.1f}% ≤½ {x['p_half']:4.1f}% "
            f"худш ×{x['worst']:.2f}" + (f"; кв. геом ×{x['q_geo']:.2f} ×2 {x['q_x2']:4.1f}% худш ×{x['q_worst']:.2f}" if "q_geo" in x else "")
            for w, x in ((w, r[w]) for w in WINDOWS)))


if __name__ == "__main__":
    main()
