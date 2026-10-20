"""Турбо «как в жизни» с разных дат начала: итог одной цепочки ставок сильно зависит от дня первой ставки,
поэтому цепочки запускаются с нескольких дат и сравниваются в среднем (turbo/sequence_2026.py).

    venv\\Scripts\\python turbo\\sequence_paths.py [--keys new_cf,new] [--starts 2026-07-01,2026-07-03,…] [--last 2026-09-29]
Итог: turbo/results/sequence_paths.json и таблица в консоли.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import risk_rules as RR  # noqa: E402
import sequence_2026 as SQ  # noqa: E402

OUT = os.path.join(HERE, "results", "sequence_paths.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default="new_cf,new")
    ap.add_argument("--starts", default=",".join(CP.grid("2026-07-01", "2026-07-15", step=2)))
    ap.add_argument("--last", default="2026-09-29")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    keys, starts = a.keys.split(","), a.starts.split(",")
    btc, fund = RR.btc_hourly(), RR.btc_funding()
    jobs = [(k, s) for k in keys for s in starts]
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(lambda j: SQ.run(j[0], btc, fund, first=j[1], last=a.last, tag=f"p{j[1][5:].replace('-', '')}",
                                           verbose=False), jobs))
    out = {k: {} for k in keys}
    for (k, s), r in zip(jobs, res):
        out[k][s] = {"pnl_200": r["pnl_200"], "capital_800": r["capital_800"], "n": r["n"], "wins": r["wins"],
                     "avg_loss": r["avg_loss"], "worst": r["worst"],
                     "bets": [(b["start"], b["kind"], b["result_pct"]) for b in r["bets"]]}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Цепочки ставок с {starts[0]} … {starts[-1]} по {a.last}; итог при ставке $200 по каждой дате начала:")
    for k in keys:
        pnl = [out[k][s]["pnl_200"] for s in starts]
        cap = [out[k][s]["capital_800"] for s in starts]
        print(f"\n  {SQ.SETUPS[k][0]}: в среднем {np.mean(pnl):+.0f} $ (от {min(pnl):+.0f} до {max(pnl):+.0f}), "
              f"капитал $800 → {np.mean(cap):.0f} $ в среднем, цепочек в плюсе {sum(p > 0 for p in pnl)} из {len(pnl)}")
        for s in starts:
            v = out[k][s]
            print(f"    {s}: {v['pnl_200']:+7.1f} $ | ставок {v['n']} (в плюсе {v['wins']}) | "
                  + ", ".join(f"{b[2]:+.0f}" for b in v["bets"]))


if __name__ == "__main__":
    main()
