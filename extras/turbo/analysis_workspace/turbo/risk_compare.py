"""Сравнение вариантов турбо с защитой от обвала по правилам ставки (цель ×2, фильтр SMA200), 171 месяц 2022–2026.

Варианты — настоящее ядро passivbot (turbo/episodes.py → turbo/results/episodes_risk.json):
  №15 сейчас; №17 экспозиция 12; №18 быстрый стоп (сглаживание 1 ч); №19 потолок на монету; №20 оба.
Поверх — стоп ставки панелью (×0,6) и выход при обвале BTC (−3% за 4 ч).
  venv\\Scripts\\python turbo\\risk_compare.py
"""
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import risk_rules as RR  # noqa: E402

NAMES = {15: "№15 сейчас (эксп. 20, стоп 40%)", 17: "№17 экспозиция 12", 18: "№18 быстрый стоп (1 ч)",
         19: "№19 потолок на монету", 20: "№20 быстрый стоп + потолок"}


def curves(vi, starts):
    import glob
    out = []
    for s in starts:
        runs = sorted(glob.glob(os.path.join(RR.ROOT, "backtests", "turbo", f"v{vi:02d}_{s}", "*", "*")),
                      key=os.path.getmtime)
        for r in reversed(runs):
            f = os.path.join(r, "balance_and_equity.csv.gz")
            if os.path.exists(f):
                df = pd.read_csv(f)
                t = pd.to_datetime(df.iloc[:, 0], utc=True).dt.tz_localize(None)
                eq = df["usd_total_equity"].astype(float).values
                out.append({"start": s, "t": t.values, "rel": eq / eq[0]})
                break
    return out


def main():
    data = json.load(open(os.path.join(RR.RES, "episodes_risk.json"), encoding="utf-8"))
    btc, fund = RR.btc_hourly(), RR.btc_funding()
    starts = sorted({e["start"] for e in data["15"]["episodes_detail"] if "outcome" in e})
    rules = {"×2 + SMA200 (как сейчас)": (None, None),
             "+ стоп ставки панелью −40%": (0.6, None),
             "+ выход при обвале BTC −3%/4ч": (None, (4, 0.03))}
    result = {}
    for vi, name in NAMES.items():
        eps = curves(vi, starts)
        feats = {e["start"]: RR.entry_features(btc, fund, e["start"]) for e in eps}
        for rname, (stop, crash) in rules.items():
            rows = {"2022–24": [], "2025–26": []}
            for e in eps:
                if not feats[e["start"]]["sma"][200]:
                    continue
                fl = RR.crash_flags(btc, e["t"], *crash) if crash else None
                rows["2022–24" if e["start"] < RR.SPLIT else "2025–26"].append(RR.outcome(e["rel"], fl, 2.0, None, stop))
            s = {k: RR.summarize(v) for k, v in rows.items()}
            s["всё"] = RR.summarize(rows["2022–24"] + rows["2025–26"])
            result[f"{name} | {rname}"] = s
    json.dump(result, open(os.path.join(RR.RES, "risk_compare.json"), "w"), ensure_ascii=False, indent=1)
    for k, s in result.items():
        print(f"\n{k}")
        for per in ("2022–24", "2025–26", "всё"):
            x = s[per]
            print(f"   {per:<8} ставок {x['bets']:3d} | рост {x['growth_25']:+5.2f}%/мес | ср. ставка {x['mean_bet']:+6.1f}% | "
                  f"×2 {x['p_target']:4.1f}% | сгорело {x['p_burn']:4.1f}% | потеря прибыли {x['p_giveback']:4.1f}% | "
                  f"−50%+ {x['p_loss50']:4.1f}% | медиана {x['median_bet']:+6.1f}%")


if __name__ == "__main__":
    main()
