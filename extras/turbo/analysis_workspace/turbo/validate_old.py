"""Проверка «умных» правил турбо-бота на 2022–2024 — годах, которых они не видели.

Правила выбраны по 2025–2026 (analyze_rules.py) и здесь НЕ подбираются заново:
  «Умный ва-банк»      — №15 (экспозиция 20, 3 монеты, стоп движка −40%), фильтр BTC > SMA200, цель ×2;
  «Максимум»           — то же без цели;
  «Ва-банк (прежний)»  — №6 без стопа и фильтра, цель ×2.
Результат: turbo/results/validation_2022_2024.json и таблица по годам.
"""
import json
import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from analyze_rules import btc_daily, curve, outcome, growth  # noqa: E402

SRC = os.path.join(HERE, "results", "episodes_2022_2024.json")
OUT = os.path.join(HERE, "results", "validation_2022_2024.json")
RULES = [("Умный ва-банк (фильтр SMA200, стоп −40%, цель ×2)", "15", 2.0, 200),
         ("Максимум (фильтр SMA200, стоп −40%, без цели)", "15", None, 200),
         ("Стоп −40%, без фильтра, цель ×2", "15", 2.0, None),
         ("Ва-банк прежний (без стопа и фильтра, цель ×2)", "6", 2.0, None)]


def main():
    data = json.load(open(SRC, encoding="utf-8"))
    btc = btc_daily(datetime(2021, 4, 1, tzinfo=timezone.utc))
    rows = []
    for name, vi, target, reg in RULES:
        items = []
        for e in data[vi]["episodes_detail"]:
            if "outcome" not in e:
                continue
            rel = curve(vi, e["start"])
            if rel is None:
                continue
            d = pd.Timestamp(e["start"])
            if reg:
                hist = btc[btc.index < d]
                if len(hist) < reg or hist.iloc[-1] <= hist.iloc[-reg:].mean():
                    items.append((e["start"], None, "skip"))
                    continue
            r, k = outcome(rel, target, None, None)
            items.append((e["start"], r, k))
        for year in ("2022", "2023", "2024", "все"):
            sel = [x for x in items if year == "все" or x[0].startswith(year)]
            bets = [x for x in sel if x[1] is not None]
            res = [x[1] for x in bets]
            n = len(res)
            row = {"rule": name, "year": year, "months": len(sel), "bets": n}
            if n:
                row.update(p_target=round(sum(x[2] == "target" for x in bets) / n * 100, 1),
                           p_burn=round(sum(x[2] == "burn" for x in bets) / n * 100, 1),
                           p_loss50=round(sum(r <= -0.5 for r in res) / n * 100, 1),
                           mean_bet=round(sum(res) / n * 100, 1),
                           mean_month=round(sum(res) / len(sel) * 100, 1),
                           growth_25=round((math.exp(growth(res, 0.25)) - 1) * 100, 2))
            rows.append(row)
            if n:
                print(f"{name[:48]:<48} {year:<4} ставок {n:3d}/{len(sel):<3} | цель {row['p_target']:5.1f}% | сгорел {row['p_burn']:5.1f}% | "
                      f"−50%+ {row['p_loss50']:5.1f}% | ср.ставка {row['mean_bet']:+6.1f}% | ср.мес {row['mean_month']:+6.1f}% | "
                      f"рост при 25% {row['growth_25']:+5.2f}%")
            else:
                print(f"{name[:48]:<48} {year:<4} ставок   0/{len(sel):<3} | фильтр рынка не разрешил ни одной ставки")
        print()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("Готово:", OUT)


if __name__ == "__main__":
    main()
