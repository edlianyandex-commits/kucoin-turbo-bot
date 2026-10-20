"""Турбо: какие правила ставки снижают риск обнуления, сохраняя доходность.

Без новых прогонов: берёт почасовые кривые капитала всех отрезков из turbo/results/episodes*.json
(папки backtests/turbo/vNN_<старт>) и пересчитывает исход ставки при других правилах:
  • цель: фиксация при ×T (1,3 … 3, или без цели);
  • стоп всей ставки: выход, когда капитал ≤ S от старта (по почасовой точке — в обвал выход может быть хуже S);
  • «замок»: после достижения ×1,5 стоп поднимается до ×1,2;
  • фильтр рынка на старте ставки: BTC выше своей средней за N дней (иначе ставку пропускаем).
Результат: turbo/results/rules.json и таблица в консоли.
"""
import glob
import itertools
import json
import math
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "results", "rules.json")
BURN = 0.10
VARIANTS = {"6": "Экспозиция 20, 3 монеты", "5": "Экспозиция 12, 3 монеты", "4": "Экспозиция 8, 3 монеты",
            "1": "Экспозиция 5, 7 монет", "13": "Эксп. 12 + стоп движка −50%",
            "14": "Эксп. 20 + стоп движка −50%", "15": "Эксп. 20 + стоп движка −40%",
            "16": "Эксп. 16 + стоп движка −50%", "17": "Эксп. 12 + стоп движка −40%"}


def btc_daily(since=datetime(2024, 4, 1, tzinfo=timezone.utc)):
    """Дневные закрытия BTC-USDT (KuCoin, публичные данные) с запасом в 300 дней до первого отрезка."""
    s = requests.Session()
    s.trust_env = False
    out = {}
    start = int(since.timestamp())
    end = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    while start < end:
        chunk_end = min(end, start + 1400 * 86400)
        rows = s.get("https://api.kucoin.com/api/v1/market/candles",
                     params={"type": "1day", "symbol": "BTC-USDT", "startAt": start, "endAt": chunk_end}, timeout=20).json()["data"]
        for r in rows:
            out[int(r[0])] = float(r[2])
        start = chunk_end
    ser = pd.Series(out).sort_index()
    ser.index = pd.to_datetime(ser.index, unit="s", utc=True).tz_localize(None).normalize()
    return ser


def curve(vi, start):
    runs = sorted(glob.glob(os.path.join(ROOT, "backtests", "turbo", f"v{int(vi):02d}_{start}", "*", "*")),
                  key=os.path.getmtime)
    for r in reversed(runs):   # последний успешный прогон отрезка
        f = os.path.join(r, "balance_and_equity.csv.gz")
        if os.path.exists(f):
            eq = pd.read_csv(f)["usd_total_equity"].astype(float).values
            return eq / eq[0]
    return None


def outcome(rel, target, stop, lock):
    """Итог ставки (доля): rel — капитал относительно старта по часам."""
    floor = stop
    for x in rel:
        if lock and x >= lock[0]:
            floor = max(floor or 0, lock[1])
        if target and x >= target:
            return target - 1, "target"
        if x <= BURN:
            return x - 1, "burn"
        if floor and x <= floor:
            return x - 1, "stop"
    last = rel[-1]
    if len(rel) < 30 * 24 * 0.95 and last < 0.5:   # движок остановил прогон раньше срока — ликвидация
        return -0.95, "burn"
    return last - 1, "end"


def growth(res, f):
    return sum(math.log(max(1e-9, 1 + f * r)) for r in res) / len(res)


def main():
    btc = btc_daily()
    eps = {}
    for fname in ("episodes_2025.json", "episodes.json"):
        data = json.load(open(os.path.join(HERE, "results", fname), encoding="utf-8"))
        for vi in VARIANTS:
            for e in data.get(vi, {}).get("episodes_detail", []):
                if "outcome" in e:
                    rel = curve(vi, e["start"])
                    if rel is not None:
                        eps.setdefault(vi, []).append((e["start"], rel))
    targets = [None, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0]
    stops = [None, 0.7, 0.6, 0.5, 0.4]
    locks = [None, (1.5, 1.2)]
    regimes = [None, 50, 100, 200]
    rows = []
    for vi, items in eps.items():
        for target, stop, lock, reg in itertools.product(targets, stops, locks, regimes):
            if lock and target and target <= lock[0]:
                continue
            res, kinds, taken = [], [], 0
            for start, rel in items:
                d = pd.Timestamp(start)
                if reg:
                    hist = btc[btc.index < d]
                    if len(hist) < reg or hist.iloc[-1] <= hist.iloc[-reg:].mean():
                        continue
                r, k = outcome(rel, target, stop, lock)
                res.append(r)
                kinds.append(k)
            n = len(res)
            if n < 10:
                continue
            fbest = max((i / 100 for i in range(1, 101)), key=lambda f: growth(res, f))
            rows.append({
                "variant": vi, "name": VARIANTS[vi], "target": target, "stop": stop, "lock": lock, "regime_sma": reg,
                "bets": n, "months": len(items), "p_target": round(kinds.count("target") / n * 100, 1),
                "p_burn": round(kinds.count("burn") / n * 100, 1),
                "p_loss50": round(sum(r <= -0.5 for r in res) / n * 100, 1),
                "mean_bet": round(sum(res) / n * 100, 1),
                "mean_month": round(sum(res) / len(items) * 100, 1),   # пропущенные месяцы = 0
                "median_bet": round(float(pd.Series(res).median()) * 100, 1),
                "worst": round(min(res) * 100, 1),
                "kelly_f": fbest, "kelly_growth": round((math.exp(growth(res, fbest)) - 1) * 100, 2),
                "growth_25": round((math.exp(growth(res, 0.25)) - 1) * 100, 2),
                "growth_all_in": round((math.exp(growth(res, 1.0)) - 1) * 100, 2),
            })
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    def show(title, rs, k=12):
        print(f"\n=== {title}")
        for r in rs[:k]:
            lk = "замок 1,5→1,2" if r["lock"] else ""
            print(f"{r['name'][:26]:<26} цель {str(r['target'] or '—'):<5} стоп {str(r['stop'] or '—'):<4} {lk:<14} "
                  f"фильтр {('SMA' + str(r['regime_sma'])) if r['regime_sma'] else '—':<7} | ставок {r['bets']:2d}/{r['months']} | "
                  f"цель {r['p_target']:5.1f}% | сгорел {r['p_burn']:5.1f}% | −50%+ {r['p_loss50']:5.1f}% | "
                  f"ср.ставка {r['mean_bet']:+6.1f}% | ср.мес {r['mean_month']:+6.1f}% | рост при 25% {r['growth_25']:+5.2f}% | "
                  f"всё-в-ставку {r['growth_all_in']:+6.2f}% | Келли {r['kelly_f']:.2f}→{r['kelly_growth']:+.2f}%")

    base = [r for r in rows if r["target"] == 2.0 and not r["stop"] and not r["lock"] and not r["regime_sma"]]
    show("Как сейчас (цель ×2, без стопа и фильтра)", sorted(base, key=lambda r: -r["mean_bet"]))
    cal = [r for r in rows if r["variant"] in ("5", "13") and r["target"] == 2.0 and not r["lock"] and not r["regime_sma"]
           and r["stop"] in (None, 0.5)]
    show("Проверка эмуляции стопа: №5 с эмулированным −50% против №13 со стопом движка", cal)
    safe = [r for r in rows if r["p_burn"] <= 10 and r["p_loss50"] <= 25]
    show("Лучшие по среднему итогу ставки, где сгорает ≤10% и −50%+ ≤25%", sorted(safe, key=lambda r: -r["mean_bet"]))
    show("Лучшие по росту капитала при ставке 25% капитала в месяц", sorted(rows, key=lambda r: -r["growth_25"]))
    show("Лучшие для «всё в ставку» каждый месяц (рост капитала)", sorted(rows, key=lambda r: -r["growth_all_in"]))
    print("\nГотово:", OUT)


if __name__ == "__main__":
    main()
