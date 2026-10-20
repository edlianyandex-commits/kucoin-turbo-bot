"""Turbo без «×2 и остановка»: как получить +100% за месяц другим путём — сложный процент в пределах месяца.

Из настоящих прогонов passivbot со старта каждого дня 2026 (turbo/chain_rules.py, движок 3.0, прогон до 60 дней,
капитал поминутно) — месяц торговли со всей прибылью в обороте: ставка → выход по правилу → следующая со следующего дня
на весь капитал (после стопа passivbot «без перезапуска» — пауза 30 дней, т. е. до конца месяца). Итог месяца —
капитал на 30-й день / капитал на старте (открытая ставка — по текущему капиталу).
Правила выхода (+ везде сгорание, стоп passivbot и выход при обвале BTC −5% за 12 ч, как в 3.0):
  target  — цель ×T и стоп ×S от старта ставки, затем новая ставка (лесенка; T=2, S=0.7 — нынешний 3.0);
  ratchet — бот не останавливается на цели: каждый раз, когда капитал вырос в T раз от ступени, ступень поднимается,
            стоп = ступень × S (только вверх); выход — только по стопу;
  trail   — стоп на x ниже максимума капитала ставки (с начала — не ниже ×S);
  none    — без правил панели.
Выход — худшая из 2 следующих минут минус расходы (2% капитала, как во всех расчётах Turbo).

    venv\\Scripts\\python turbo\\month_paths.py [--cost 0.02]
Итог: turbo/results/month_paths.json и таблица в консоли.
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chain_rules as C  # noqa: E402
import drawdown_study as DS  # noqa: E402

DELAY, BURN = DS.DELAY, 0.10
MONTH = 30
HALVES = {"I пол. 2026": ("2026-01-01", "2026-06-30"), "II пол. 2026": ("2026-07-01", C.DATA_END)}


def policies():
    out = {"3.0 сейчас: ×2, стоп −30%": {"mode": "target", "T": 2.0, "S": 0.7}}
    for T in (1.1, 1.2, 1.3, 1.5):
        for S in (0.85, 0.9, 0.8, 0.7):
            if S < 0.85 or T <= 1.3:
                out[f"лесенка: +{round((T - 1) * 100)}%, стоп −{round((1 - S) * 100)}%"] = {"mode": "target", "T": T, "S": S}
    for T in (1.1, 1.2, 1.3):
        for S in (0.7, 0.8, 0.85, 0.9):
            out[f"храповик: ступень +{round((T - 1) * 100)}%, стоп −{round((1 - S) * 100)}% от ступени"] = {"mode": "ratchet", "T": T, "S": S}
    for x in (0.15, 0.2, 0.3):
        out[f"плавающий стоп −{round(x * 100)}% от максимума"] = {"mode": "trail", "x": x, "S": 1 - x}
    out["без правил панели (только passivbot)"] = {"mode": "none"}
    return out


class Engine:
    def __init__(self, cost, bets=None):
        """bets — кривые ставок по дням (по умолчанию движок 3.0, прогон до 60 дней из chain_rules)."""
        self.cost = cost
        self.B = bets or C.Bets(json.load(open(C.panic_file(200, 60))), 200, 60)
        self.memo = {}

    def exit(self, day, p):
        """(минута выхода или None, множитель капитала, причина, стоп passivbot до выхода)."""
        k = (day, json.dumps(p, sort_keys=True))
        if k in self.memo:
            return self.memo[k]
        rel, n = self.B.rel[day], len(self.B.rel[day])
        ev = {"burn": DS.first(rel <= BURN), "crash": DS.first(self.B.crash[day]) if p.get("crash", True) else None}
        pm = self.B.panic.get(day)
        if pm is not None:
            ev["hsl"] = pm
        mode = p["mode"]
        if mode == "target":
            ev["target"] = DS.first(rel >= p["T"]) if p.get("T") else None
            ev["stop"] = DS.first(rel <= p["S"]) if p.get("S") else None
        elif mode == "ratchet":
            base, floor, i = 1.0, p["S"], 0
            while True:
                up, dn = DS.first(rel >= base * p["T"], i), DS.first(rel <= floor, i)
                if dn is not None and (up is None or dn <= up):
                    ev["stop"] = dn
                    break
                if up is None:
                    break
                base *= p["T"]
                floor, i = max(floor, base * p["S"]), up
        elif mode == "trail":
            fl = np.maximum(np.maximum.accumulate(rel) * (1 - p["x"]), p["S"])
            ev["stop"] = DS.first(rel <= fl)
        ev = {a: b for a, b in ev.items() if b is not None}
        if not ev:
            res = (None, float(rel[-1]), "end", False)
        else:
            order = ["burn", "target", "stop", "crash", "hsl"]
            kind = min(ev, key=lambda a: (ev[a], order.index(a)))
            i = ev[kind]
            if kind == "burn":
                m = float(rel[i])
            elif kind == "hsl":
                m = float(rel[min(i + DELAY, n - 1)])
            else:
                m = float(rel[i:i + DELAY + 1].min()) * (1 - self.cost)
            res = (i, m, kind, pm is not None and pm <= i)
        self.memo[k] = res
        return res

    def month(self, start, p, days=MONTH):
        """Множитель капитала за days дней с start, число ставок и причины выхода."""
        d = datetime.fromisoformat(start)
        end = d + timedelta(days=days)
        cap, kinds = 1.0, []
        while d < end:
            day = d.strftime("%Y-%m-%d")
            if day not in self.B.rel or self.B.crash[day][0]:
                d += timedelta(days=1)
                continue
            i, m, kind, latched = self.exit(day, p)
            rel = self.B.rel[day]
            end_m = len(rel) - 1 if i is None else i
            if d + timedelta(minutes=end_m) >= end:          # ставка ещё идёт в конце месяца — по текущему капиталу
                cut = int((end - d).total_seconds() // 60)
                cap *= float(rel[min(cut, len(rel) - 1)])
                kinds.append("идёт")
                break
            cap *= m
            kinds.append(kind)
            if cap <= 0.02:
                break
            d = (d + timedelta(minutes=end_m) + timedelta(days=30 if latched else 1)).replace(hour=0, minute=0)
        return cap, kinds


def stats(caps):
    c = np.array(caps)
    return {"n": len(c), "median": round(float(np.median(c)), 3), "mean": round(float(c.mean()), 3),
            "geo": round(math.exp(float(np.mean(np.log(np.maximum(c, 1e-6))))), 3),
            "p_x2": round(float((c >= 2).mean()) * 100, 1), "p_x15": round(float((c >= 1.5).mean()) * 100, 1),
            "p_loss": round(float((c < 1).mean()) * 100, 1), "p_half": round(float((c <= 0.5).mean()) * 100, 1),
            "p10": round(float(np.percentile(c, 10)), 3), "p90": round(float(np.percentile(c, 90)), 3),
            "worst": round(float(c.min()), 3), "best": round(float(c.max()), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=DS.COST)
    a = ap.parse_args()
    E = Engine(a.cost)
    rows = []
    for name, p in policies().items():
        row = {"name": name, "rule": p}
        for half, (first, last) in HALVES.items():
            starts = C.CP.grid(first, (datetime.fromisoformat(last) - timedelta(days=MONTH)).strftime("%Y-%m-%d"), 1)
            res = [E.month(s, p) for s in starts]
            row[half] = stats([r[0] for r in res])
            row[half]["bets"] = round(float(np.mean([len(r[1]) for r in res])), 1)
        rows.append(row)
    out = os.path.join(HERE, "results", "month_paths" + ("" if a.cost == DS.COST else f"_cost{a.cost:g}") + ".json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    H1, H2 = HALVES
    print(f"Месяц торговли со всей прибылью в обороте, старт каждый день; расходы на выход {a.cost * 100:g}% капитала\n"
          "×2 — доля месяцев с +100% и больше; ≤½ — доля месяцев с потерей половины и больше; «средн. геом.» — типичный рост\n")
    for r in sorted(rows, key=lambda r: -(math.log(r[H1]["geo"]) + math.log(r[H2]["geo"]))):
        print(f"{r['name'][:52]:<52} | " + " | ".join(
            f"{h.split()[0]:>2}: медиана ×{x['median']:.2f}, средн. геом. ×{x['geo']:.2f}, ×2 {x['p_x2']:4.1f}%, "
            f"минус {x['p_loss']:4.1f}%, ≤½ {x['p_half']:4.1f}%, ставок {x['bets']:4.1f}"
            for h in (H1, H2) for x in [r[h]]))


if __name__ == "__main__":
    main()
