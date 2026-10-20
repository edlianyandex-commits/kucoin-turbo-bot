"""Турбо 3.0 на второй половине 2026: на каких монетах ставка достигает цели ×2 — и можно ли выбрать их заранее.

1) Каждая монета отдельно: движок 3.0 (эксп. 20, 3 позиции) с одной разрешённой монетой — вклад одного «слота»
   в счёт; 30-дневные окна со стартом каждые 5 дней с 02.05.2026. Оценка окна — итог слота с правилами ставки 3.0.
2) Честная проверка (walk-forward): перед ставкой S (01.07 … 30.08, каждые 5 дней) монеты ранжируются по окнам,
   которые закончились до S (старт от S−60 до S−30 дней); ставка S — на K лучших монетах, 30 дней, правила 3.0.
3) С подглядыванием: K лучших монет по окнам самой второй половины 2026 — на тех же ставках (в жизни так нельзя).
4) Для сравнения: 3.0 на всех 35 монетах.
Капитал поминутно; свечей за текущий день в бэктесте нет (данные по вчерашний день).

    venv\\Scripts\\python turbo\\coin_pick.py [--workers 6]
Итог: turbo/results/coin_pick.json и таблицы в консоли.
"""
import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

MC = DS.MC
RES = os.path.join(HERE, "results")
CURVES = os.path.join(RES, "coin_pick")
DATA_END = "2026-09-30"                 # свечи по 29.09 включительно
TARGET, STOP, CRASH = PW.RULES3
KS = (3, 5)


def grid(first, last, step=5):
    d, out = datetime.fromisoformat(first), []
    while d <= datetime.fromisoformat(last):
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=step)
    return out


COIN_STARTS = grid("2026-05-02", "2026-08-30")
BET_STARTS = grid("2026-07-01", "2026-08-30")


def end_of(start):
    return min(datetime.fromisoformat(start) + timedelta(days=30), datetime.fromisoformat(DATA_END)).strftime("%Y-%m-%d")


def curve(key, start, coins):
    """Поминутная кривая (капитал / старт) из кэша или прогона passivbot; None — ошибка."""
    path = os.path.join(CURVES, key, f"{start}.npy")
    if not os.path.exists(path):
        res = DS.backtest_curve(dict(PW.ENGINE3), start, end_of(start), f"cp_{key}_{start}", coins=coins)
        if isinstance(res, str):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.save(path, res[0])
        shutil.rmtree(res[1], ignore_errors=True)
    return np.load(path)


def bet(rel, start, btc):
    return DS.outcome(rel, TARGET, STOP, None, DR.crash_minutes(btc, start, len(rel), *CRASH))


def summary(items):
    s = DS.summarize(items)
    return s and {k: s[k] for k in ("bets", "mean", "median", "p_target", "p_loss", "avg_loss", "worst", "growth_25")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    btc = RR.btc_hourly()
    coins = MC.default_coins()
    t0 = time.time()
    jobs = [(c, s) for c in coins for s in COIN_STARTS]
    with ThreadPoolExecutor(a.workers) as ex:
        per = list(ex.map(lambda cs: curve(f"coin_{cs[0]}", cs[1], [cs[0]]), jobs))
    print(f"монеты по одной: {len(jobs)} прогонов, {time.time() - t0:.0f} с", flush=True)
    score, traded = {}, {}
    for (c, s), rel in zip(jobs, per):
        if rel is None:
            continue
        score[(c, s)] = bet(rel, s, btc)[0]
        traded[(c, s)] = bool(np.ptp(rel) > 1e-6)          # минимальный ордер мог не пустить монету в торговлю

    def rank(window_starts):
        rows = []
        for c in coins:
            xs = [score[(c, s)] for s in window_starts if (c, s) in score and traded[(c, s)]]
            if len(xs) >= max(2, len(window_starts) // 2):
                rows.append((c, float(np.mean(xs)), len(xs)))
        return sorted(rows, key=lambda r: -r[1])

    # что было бы с подглядыванием: лучшие монеты по окнам самой второй половины 2026
    h2 = [s for s in COIN_STARTS if s >= BET_STARTS[0]]
    hind = rank(h2)
    plans = {"all": {s: None for s in BET_STARTS}}
    for k in KS:
        plans[f"walk{k}"] = {}
        for s in BET_STARTS:
            lo = datetime.fromisoformat(s) - timedelta(days=60)
            hi = datetime.fromisoformat(s) - timedelta(days=30)
            look = [x for x in COIN_STARTS if lo <= datetime.fromisoformat(x) <= hi]
            plans[f"walk{k}"][s] = [c for c, _, _ in rank(look)[:k]]
        plans[f"hind{k}"] = {s: [c for c, _, _ in hind[:k]] for s in BET_STARTS}
    jobs = [(name, s, cs) for name, p in plans.items() for s, cs in p.items()]
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        rels = list(ex.map(lambda j: curve(f"{j[0]}_{'-'.join(j[2]) if j[2] else 'all'}", j[1], j[2]), jobs))
    print(f"ставки: {len(jobs)} прогонов, {time.time() - t0:.0f} с", flush=True)
    res = {name: [] for name in plans}
    for (name, s, cs), rel in zip(jobs, rels):
        if rel is not None:
            r, kind = bet(rel, s, btc)
            res[name].append({"start": s, "coins": cs, "result_pct": round(r * 100, 1), "kind": kind})
    out = {"bet_starts": BET_STARTS, "hindsight_rank": [{"coin": c, "mean_slot_pct": round(m * 100, 1), "windows": n}
                                                       for c, m, n in hind],
           "plans": {name: {"summary": summary([(b["result_pct"] / 100, b["kind"]) for b in bs]), "bets": bs}
                     for name, bs in res.items()}}
    json.dump(out, open(os.path.join(RES, "coin_pick.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\nМонеты во второй половине 2026 (слот 3.0, средний итог окна с правилами ставки):")
    print("  " + ", ".join(f"{c} {m * 100:+.1f}%" for c, m, _ in hind))
    names = {"all": "3.0 на всех 35 монетах", "walk3": "3 лучшие по прошлым 2 мес. (честно)",
             "walk5": "5 лучших по прошлым 2 мес. (честно)", "hind3": "3 лучшие по H2 2026 (подглядывание)",
             "hind5": "5 лучших по H2 2026 (подглядывание)"}
    print(f"\nСтавки {BET_STARTS[0]} … {BET_STARTS[-1]} (каждые 5 дней), правила 3.0:")
    for name, v in out["plans"].items():
        s = v["summary"]
        print(f"  {names[name]:<40} ставок {s['bets']:2d} | ×2 {s['p_target']:5.1f}% | в минусе {s['p_loss']:5.1f}% | "
              f"ср. итог {s['mean']:+6.1f}% | медиана {s['median']:+6.1f}% | ср. убыток {s['avg_loss']:+5.1f}% | "
              f"худшая {s['worst']:+6.1f}% | рост при 25% {s['growth_25']:+5.2f}%/мес")
    print("\nЧестный выбор монет по датам:")
    for s in BET_STARTS:
        b = next(x for x in res["walk3"] if x["start"] == s)
        print(f"  {s}: {', '.join(b['coins']):<22} → {b['result_pct']:+6.1f}% ({b['kind']})")


if __name__ == "__main__":
    main()
