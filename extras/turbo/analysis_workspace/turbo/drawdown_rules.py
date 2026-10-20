"""Турбо, рынок 2025–2026: правила выхода панели поверх поминутных кривых turbo/drawdown_study.py (без новых прогонов).

Правила: цель ×T; стоп ×S от старта; «безубыток» — после ×A стоп поднимается до ×B; выход при обвале BTC
(закрытие часа на X% ниже максимума последних N часов — видно только после закрытия часа).
Выход — худший капитал из двух следующих минут минус расходы (как в drawdown_study.py).
Хорошее правило должно быть лучше текущего (×1,75, стоп ×0,6) и в 2025, и в 2026 — отдельно.

    venv\\Scripts\\python turbo\\drawdown_rules.py [--variant 1] [--scope all|filter]
Итог: turbo/results/drawdown_rules_v<вариант>.json
"""
import argparse
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_study as DS  # noqa: E402
import risk_rules as RR  # noqa: E402

TARGETS = [1.5, 1.75, 2.0]
STOPS = [None, 0.6, 0.65, 0.7, 0.75, 0.8]
LOCKS = [None, (1.3, 1.0), (1.3, 1.1), (1.5, 1.0), (1.5, 1.2)]
CRASHES = [None, (4, 0.03), (4, 0.05), (24, 0.07),
           (6, 0.04), (8, 0.04), (8, 0.05), (12, 0.04), (12, 0.05), (12, 0.06), (16, 0.05), (16, 0.06)]
BASE_RULE = (1.75, 0.6, None, None)


def crash_minutes(btc, start, n, hours, drop):
    """Поминутный флаг: BTC на drop ниже максимума закрытий за hours часов (известно после закрытия часа)."""
    roll = btc.rolling(hours, min_periods=1).max()
    flag = (btc / roll - 1) <= -drop
    flag.index = flag.index + pd.Timedelta(hours=1)          # закрытие свечи известно в конце часа
    idx = pd.Timestamp(start) + pd.to_timedelta(np.arange(n), unit="min")
    flag = flag.astype(float)
    return flag.reindex(flag.index.union(idx)).ffill().reindex(idx).fillna(0.0).values > 0.5


first, outcome = DS.first, DS.outcome


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", type=int, default=1)
    ap.add_argument("--scope", choices=["all", "filter"], default="all")
    a = ap.parse_args()
    btc = RR.btc_hourly()
    wins = [(s, ok) for s, _, ok in DS.starts() if a.scope == "all" or ok]
    curves = {s: np.load(os.path.join(DS.CURVES, str(a.variant), f"{s}.npy")) for s, _ in wins}
    flags = {c: {s: crash_minutes(btc, s, len(curves[s]), *c) for s, _ in wins} for c in CRASHES if c}
    rows = []
    for target, stop, lock, crash in itertools.product(TARGETS, STOPS, LOCKS, CRASHES):
        if lock and lock[0] >= target:
            continue
        per = {"2025": [], "2026": []}
        for s, _ in wins:
            fl = flags[crash][s] if crash else None
            per["2025" if s < DS.YEAR_SPLIT else "2026"].append(outcome(curves[s], target, stop, lock, fl))
        r = {k: DS.summarize(v) for k, v in per.items()}
        r["оба"] = DS.summarize(per["2025"] + per["2026"])
        rows.append({"rule": (target, stop, lock, crash), **r})
    base = next(r for r in rows if r["rule"] == BASE_RULE)
    out = os.path.join(HERE, "results", f"drawdown_rules_v{a.variant}_{a.scope}.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, default=str)

    def label(rule):
        t, s, lk, c = rule
        p = [f"×{t}", f"стоп ×{s}" if s else "без стопа"]
        p += [f"после ×{lk[0]} стоп ×{lk[1]}"] if lk else []
        p += [f"BTC −{c[1] * 100:.0f}%/{c[0]}ч"] if c else []
        return ", ".join(p)

    def line(r):
        a_, b_ = r["2025"], r["2026"]
        return (f"рост {a_['growth_25']:+5.2f}/{b_['growth_25']:+5.2f} | ср. {a_['mean']:+5.1f}/{b_['mean']:+5.1f}% | "
                f"ср. убыток {a_['avg_loss']:+5.1f}/{b_['avg_loss']:+5.1f}% | худшие 20% {a_['worst20']:+5.1f}/{b_['worst20']:+5.1f}% | "
                f"в минусе {a_['p_loss']:4.1f}/{b_['p_loss']:4.1f}% | −50%+ {r['оба']['p_loss50']:4.1f}%")

    print(f"вариант движка №{a.variant}, окна: {a.scope}, ставок 2025/2026: {base['2025']['bets']}/{base['2026']['bets']}")
    print(f"СЕЙЧАС   {label(base['rule'])}: {line(base)}")
    better = [r for r in rows if all(r[y]["worst20"] > base[y]["worst20"] and r[y]["growth_25"] >= base[y]["growth_25"] * 0.9
                                     for y in ("2025", "2026"))]
    better.sort(key=lambda r: -min(r["2025"]["worst20"] - base["2025"]["worst20"], r["2026"]["worst20"] - base["2026"]["worst20"]))
    print(f"\nМеньше убыток в худших 20% ставок в ОБА года, рост не ниже 90% текущего (найдено {len(better)}):")
    for r in better[:12]:
        print(f"  {label(r['rule']):<44} {line(r)}")
    top = sorted(rows, key=lambda r: -min(r["2025"]["growth_25"], r["2026"]["growth_25"]))[:8]
    print("\nЛучшие по росту в худший из двух лет:")
    for r in top:
        print(f"  {label(r['rule']):<44} {line(r)}")
    print("(пары: 2025 / 2026)")


if __name__ == "__main__":
    main()
