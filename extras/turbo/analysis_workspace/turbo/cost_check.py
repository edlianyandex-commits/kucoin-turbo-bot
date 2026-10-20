r"""Насколько цифры 2025–2026 зависят от издержек исполнения и от месяца старта (03.10.2026, пользователь: «насколько
эти цифры могут отличаться от реальных?» → «да»). Оба бота в нынешних настройках (основной v4; Turbo 5.0 со стопом по
монете), честный список по месяцам со снятыми с торгов (TB_FULL=1), 01.2025–09.2026, четыре уровня издержек.

    set TB_FULL=1 && venv\Scripts\python turbo\cost_check.py [--workers 11]
Итог: results/cost_check.json и таблицы в журнале.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

os.environ["TB_FULL"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import turnover_rule as T  # noqa: E402

FIRST, LAST = "2025-01", "2026-09"
LEVELS = {
    "как в бэктесте (почти без проскальзывания)": {"backtest.limit_order_fill_buffer_pct": 0.0, "backtest.market_order_slippage_pct": 0.0005,
                                                    "__exit_min": 3, "__exit_cost": 0.0006},
    "реалистично (как в цифрах)": None,
    "средне (запас 0,05%, проскальзывание 0,3%)": {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003,
                                                   "__exit_min": 7, "__exit_cost": 0.0066},
    "пессимистично (запас 0,1%, 0,5%, комиссии ×2)": {"backtest.limit_order_fill_buffer_pct": 0.001, "backtest.market_order_slippage_pct": 0.005,
                                                      "backtest.maker_fee_override": 0.0004, "backtest.taker_fee_override": 0.001,
                                                      "__exit_min": 10, "__exit_cost": 0.011},
}


def variants():
    fv, pv = T.full_variants(), T.protect_variants()
    v4, l4 = fv["Основной v4 (как сейчас)"]
    tu, lt = pv["Turbo 5.0 + быстрый стоп по монете"]
    out = {}
    for bot, base, lst in (("Основной v4", v4, l4), ("Turbo 5.0", tu, lt)):
        for lv, extra in LEVELS.items():
            if extra is None:
                over = base                                   # как в прогоне — из кэша
            else:
                over = {**{k: v for k, v in base.items() if k != "__fills"}, **extra}
            out[(bot, lv)] = (over, lst)
    return out


def main(a):
    V = variants()
    starts = {m: v for m, v in T.month_starts().items() if FIRST <= m <= LAST}
    lists = {lm: T.month_lists(lm) for _, lm in V.values()}
    jobs = [(k, m) for k in V for m in starts]

    def job(item):
        k, m = item
        over, lm = V[k]
        t, me = starts[m]
        r = T.month_job(over, lists[lm][m], f"cost|{k[0]}|{k[1]}|{m}", t, me)
        if isinstance(r, str):
            print(f"  ОШИБКА {k} {m}: {r[:150]}", flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(job, jobs)))
    out = {}
    for k in V:
        parts = [res[(k, m)] for m in starts]
        if any(isinstance(p, str) or p is None for p in parts):
            out[" | ".join(k)] = None
            continue
        cap, pieces, monthly = 1.0, [], []
        for p in parts:
            c = p["curve"] * cap
            pieces.append(c)
            end = float(c.iloc[-1]) * p["close"]
            monthly.append((end / cap - 1) * 100)
            cap = end
        c = pd.concat(pieces)
        c = c[~c.index.duplicated(keep="last")].sort_index()
        v = c.values
        ddv = 1 - v / np.maximum.accumulate(v)
        mk = list(starts)
        g = np.array([1 + x / 100 for x in monthly])
        by_start = {m: (float(np.prod(g[i:])) - 1) * 100 for i, m in enumerate(mk[:-3])}   # старт в начале месяца → 09.2026
        years = {}
        for m, x in zip(mk, monthly):
            years[m[:4]] = years.get(m[:4], 1.0) * (1 + x / 100)
        out[" | ".join(k)] = {"total": (cap - 1) * 100, "geo": (cap ** (1 / len(monthly)) - 1) * 100,
                              "years": {y: (x - 1) * 100 for y, x in years.items()}, "halves": T.halves_of(mk, monthly),
                              "monthly": dict(zip(mk, [round(x, 1) for x in monthly])), "worst_month": min(monthly),
                              "neg": sum(x < 0 for x in monthly), "dd": float(ddv.max()) * 100, "by_start": by_start}
    json.dump(out, open(os.path.join(HERE, "results", "cost_check.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for bot in ("Основной v4", "Turbo 5.0"):
        print(f"\n=== {bot}, 01.2025–09.2026")
        for lv in LEVELS:
            r = out.get(f"{bot} | {lv}")
            if not r:
                print(f"  {lv}: есть ошибки — перезапустите")
                continue
            bs = list(r["by_start"].values())
            print(f"  {lv:48s} итог {r['total']:+6.0f}% (в мес. {r['geo']:+5.1f}%) | 2025 {r['years']['2025']:+5.0f}% / 2026 {r['years']['2026']:+5.0f}% | "
                  f"в минусе {r['neg']} мес., худший {r['worst_month']:+.0f}% | худшая минута −{r['dd']:.0f}% | по месяцу старта: "
                  f"мин {min(bs):+.0f}%, медиана {np.median(bs):+.0f}%, макс {max(bs):+.0f}%, в минусе {sum(x < 0 for x in bs)} из {len(bs)}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=11)
    main(ap.parse_args())
