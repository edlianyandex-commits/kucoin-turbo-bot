"""Поиск прибыльных вариантов Turbo при плече ≤ 10× (пользователь, 01.10.2026): больше сделок, торговля в боковике,
правильный отход от границ (уровней). Всё внутри passivbot — найденное можно сразу запускать.

База: EMA Anchor (пример автора passivbot), только лонг, 10 монет, экспозиция 9 (запас маржи при плече 10), стоп passivbot
40% «без перезапуска». Перебор по одному параметру вокруг базы:
  offset — отступ входа от средней (меньше → больше сделок), ema_span_0/1 — скорость средних, offset_volatility_*_weight —
  ширина полос по волатильности («уровни»), base_qty_pct — первый вход, entry_double_down_factor — докупка,
  n_positions, веса выбора монет (forager), unstuck (частичный выход из зависших позиций), и прежний движок trailing martingale.
Окна: 30-дневный прогон со $200, старт каждые 10 дней — I пол. 2026 (отбор), II пол. 2026 (проверка), обвал 10.10.2025.

    venv\\Scripts\\python turbo\\param_search.py [--workers 8]
Итог: turbo/results/param_search.json; кривые и число сделок — turbo/results/param_search/<вариант>/<старт>.npy|.json.
"""
import argparse
import glob
import json
import math
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import engine_month as EM  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

OUT = os.path.join(HERE, "results", "param_search")
L, A = "bot.long.risk.", "bot.long.strategy.ema_anchor."
BASE = {L + "total_wallet_exposure_limit": 9.0, L + "n_positions": 10.0, **EM.NOSHORT}
VARIANTS = {"база: EMA Anchor, 10 монет, эксп. 9": (BASE, EM.EMA)}


def v(name, over, base=EM.EMA):
    VARIANTS[name] = ({**BASE, **over}, base)


for x in (0.005, 0.01, 0.03, 0.045):
    v(f"отступ входа {x * 100:g}% (база 1,93%)", {A + "offset": x})
for s0, s1 in ((100, 200), (200, 400), (800, 1600), (1500, 3000)):
    v(f"средние {s0}/{s1} мин (база 415/793)", {A + "ema_span_0": float(s0), A + "ema_span_1": float(s1)})
for w in (0.0, 6.0, 26.0, 40.0):
    v(f"полосы по волатильности 1ч ×{w:g} (база ×13)", {A + "offset_volatility_1h_weight": w})
v("без поправки на волатильность 1м (база 0,4)", {A + "offset_volatility_1m_weight": 0.0})
v("поправка на волатильность 1м ×1,5", {A + "offset_volatility_1m_weight": 1.5})
for x in (1.0, 1.5, 2.5):
    v(f"докупка ×{x:g} (база ×1,96)", {A + "entry_double_down_factor": x})
for x in (0.03, 0.1, 0.15):
    v(f"первый вход {x * 100:g}% (база 6%)", {A + "base_qty_pct": x})
v("7 монет", {L + "n_positions": 7.0})
v("15 монет", {L + "n_positions": 15.0})
v("монеты по волатильности", {"bot.long.forager.score_weights": {"ema_readiness": 0.0, "volatility": 1.0, "volume": 0.0}})
v("монеты по объёму", {"bot.long.forager.score_weights": {"ema_readiness": 0.0, "volatility": 0.0, "volume": 1.0}})
v("с разгрузкой зависших позиций (unstuck)", {"bot.long.unstuck.enabled": True})
v("trailing martingale (движок 3.0), 10 монет, эксп. 9", {}, DS.MC.BASE)
v("trailing martingale (движок 3.0), 15 монет, эксп. 9", {L + "n_positions": 15.0}, DS.MC.BASE)

SETS = {"I пол. 2026 (отбор)": CP.grid("2026-01-01", "2026-05-31", 10),
        "II пол. 2026 (проверка)": CP.grid("2026-07-01", "2026-08-31", 10),
        "обвал 10.10.2025": ["2025-09-25", "2025-10-05"]}


def key_of(name):
    return f"v{list(VARIANTS).index(name):02d}"


def run(name, start):
    d = os.path.join(OUT, key_of(name))
    path = os.path.join(d, f"{start}.npy")
    if os.path.exists(path):
        return np.load(path), json.load(open(path.replace(".npy", ".json")))
    over, base = VARIANTS[name]
    end = (datetime.fromisoformat(start) + timedelta(days=30)).strftime("%Y-%m-%d")
    res = DS.backtest_curve({**PW.ENGINE3, **over}, start, end, f"ps_{key_of(name)}_{start}", base=base)
    if isinstance(res, str):
        print("  ошибка", name, start, res[:150], flush=True)
        return None, None
    rel, rd = res
    f = pd.read_csv(sorted(glob.glob(os.path.join(rd, "*", "*", "fills.csv")))[-1])
    info = {"fills": int(len(f)), "coins": int(f["coin"].nunique()) if len(f) else 0,
            "dd": round(float((1 - rel / np.maximum.accumulate(rel)).max()) * 100, 1)}
    shutil.rmtree(rd, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    np.save(path, rel)
    json.dump(info, open(path.replace(".npy", ".json"), "w"))
    return rel, info


def month(rel, start, btc, exit_btc, cost=0.009):
    if exit_btc:
        i = DS.first(DR.crash_minutes(btc, start, len(rel), 12, 0.05))
        if i is not None:
            return float(rel[i:i + DS.DELAY + 1].min()) * (1 - cost)
    if len(rel) < 30 * 1440 * 0.95 and rel[-1] < 0.5:
        return 0.05
    return float(rel[-1])


def geo(ms):
    return round((math.exp(float(np.mean(np.log(np.maximum(np.array(ms), 1e-6))))) - 1) * 100, 1) if ms else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    jobs = [(n, s) for n in VARIANTS for ss in SETS.values() for s in ss]
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(lambda j: run(*j), jobs)))
    btc = RR.btc_hourly()
    rows = []
    for n in VARIANTS:
        row = {"name": n, "key": key_of(n)}
        for w, ss in SETS.items():
            ok = [(s, res[(n, s)]) for s in ss if res[(n, s)][0] is not None]
            row[w] = {"geo": geo([month(r, s, btc, False) for s, (r, _) in ok]),
                      "geo_exit": geo([month(r, s, btc, True) for s, (r, _) in ok]),
                      "worst": round(min(month(r, s, btc, False) for s, (r, _) in ok) * 100 - 100) if ok else None,
                      "fills_day": round(float(np.mean([i["fills"] for _, (_, i) in ok])) / 30, 1) if ok else None,
                      "dd": round(float(np.mean([i["dd"] for _, (_, i) in ok])), 1) if ok else None}
        rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", "param_search.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    W = list(SETS)
    b = rows[0]
    print("Месяц = 30 дней с прибылью в обороте, $200, плечо ≤ 10 (экспозиция 9); геом. — типичный месяц;\n"
          "«с выходом» — выход при обвале BTC и до конца месяца без торговли; ★ — лучше базы и в отборе, и в проверке\n")
    for r in sorted(rows, key=lambda r: -(r[W[0]]["geo"] or -99)):
        x, y, c = r[W[0]], r[W[1]], r[W[2]]
        star = "★" if r is not b and x["geo"] > b[W[0]]["geo"] and y["geo"] > b[W[1]]["geo"] else " "
        print(f"{star} {r['name'][:46]:<46} | I (отбор) {x['geo']:+5.1f}% (с выходом {x['geo_exit']:+5.1f}%), худш. {x['worst']:+4d}%, "
              f"сделок/день {x['fills_day']:5.1f} | II (проверка) {y['geo']:+5.1f}% (с выходом {y['geo_exit']:+5.1f}%) | "
              f"обвал 2025: {c['geo']:+5.1f}% (с выходом {c['geo_exit']:+5.1f}%)")


if __name__ == "__main__":
    main()
