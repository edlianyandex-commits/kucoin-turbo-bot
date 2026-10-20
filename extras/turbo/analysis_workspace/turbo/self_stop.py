r"""Что если убрать зависимость от биткоина (пользователь, 01.10.2026): защита ботов без чужих монет — по собственному капиталу.

Правило «стоп по капиталу»: капитал бота (поминутно, как панель видит снимок passivbot) на X% ниже своего максимума
за N часов → закрыть всё (худшая минута из 5 после сигнала, минус открытая экспозиция × 0,36%, как «реалистично»),
пауза до 00:00 UTC следующего дня, затем новый запуск. Сравнение: без выхода, выход по BTC −5% за 12 ч (сейчас)
и сочетание «BTC −5% или капитал −X%» (выход по BTC + защита от обвала отдельной монеты).
Оба бота (Turbo 5.0 7 × 9 и основной v4 10 × 8), 84 монеты, $300, три периода; прогоны passivbot — общий кэш
turbo/results/exit_rules/ (turbo/exit_rules.py).

    venv\Scripts\python turbo\self_stop.py [--workers 6]
Итог: turbo/results/self_stop.json и таблица.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_study as DS  # noqa: E402
import exit_rules as ER  # noqa: E402
import main_variants as MV  # noqa: E402
import real_test as RT  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

# правило: (стоп по капиталу (падение, окно в часах) или None, выход по BTC да/нет)
RULES = {
    "без выхода": (None, False),
    "BTC −5% за 12 ч (сейчас)": (None, True),
    "капитал −8% за 12 ч": ((0.08, 12), False),
    "капитал −10% за 12 ч": ((0.10, 12), False),
    "капитал −12% за 12 ч": ((0.12, 12), False),
    "капитал −15% за 12 ч": ((0.15, 12), False),
    "капитал −20% за 24 ч": ((0.20, 24), False),
    "BTC −5% или капитал −15%": ((0.15, 12), True),
    "BTC −5% или капитал −20%": ((0.20, 24), True),
}


def equity_flag(rel, drop, hours):
    """Первая минута, когда капитал на drop ниже своего максимума за hours часов (окно — внутри отрезка)."""
    s = pd.Series(rel)
    peak = s.rolling(int(hours * 60), min_periods=1).max().values
    hit = np.flatnonzero(rel <= (1 - drop) * peak)
    return int(hit[0]) if hit.size else None


def chain(bot, rule_name, period, coins, btc_flag):
    eq_rule, use_btc = RULES[rule_name]
    start, end = UC.PERIODS[period]
    d, cap, pieces, exits = pd.Timestamp(start), 1.0, [], []
    t_end = pd.Timestamp(end)
    while d < t_end - pd.Timedelta(days=1):
        if use_btc and ER.flag_at(btc_flag, d):
            d += pd.Timedelta(days=1)
            continue
        res = ER.segment(bot, d.strftime("%Y-%m-%d"), end, coins)
        if isinstance(res, str):
            return res
        rel, fmin, ftwe = res
        idx = d + pd.to_timedelta(np.arange(len(rel)), unit="min")
        cand = []
        if use_btc:
            hit = np.flatnonzero(ER.minute_flags(btc_flag, d, len(rel)))
            if hit.size:
                cand.append((int(hit[0]), "BTC"))
        if eq_rule:
            i = equity_flag(rel, *eq_rule)
            if i is not None:
                cand.append((i, "капитал"))
        if not cand:
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        i, why = min(cand)
        k = np.searchsorted(fmin, i, side="right") - 1
        twe = float(ftwe[k]) if k >= 0 else 0.0
        worst = float(rel[i:i + ER.EXIT_MIN].min())
        out = cap * worst * (1 - twe * ER.EXIT_COST)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        exits.append({"t": str(idx[i])[:16], "why": why, "drop_pct": round((out / (cap * float(np.max(rel[:i + 1]))) - 1) * 100, 1)})
        cap = out
        d = (idx[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([cap], index=[d]))
    c = pd.concat(pieces)
    c = c[~c.index.duplicated(keep="last")].sort_index()
    v = c.values
    return {"ret": (v[-1] / v[0] - 1) * 100, "dd_min": float((1 - v / np.maximum.accumulate(v)).max()) * 100, "exits": exits}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    DS.EP.STAKE = MV.STAKE
    coins = UT.universes()["оборот ≥ 5 млн $"]
    btc_flag = ER.hourly_flag(ER.RULES["BTC −5% за 12 ч (сейчас)"], ER.series())
    jobs = [(b, n, p) for n in RULES for b in RT.BOTS for p in UC.PERIODS]

    def job(j):
        r = chain(j[0], j[1], j[2], coins, btc_flag)
        print(f"  готово {j[1]} | {ER.BOT_KEYS[j[0]]} | {j[2][:12]}: "
              + (r[:120] if isinstance(r, str) else f"{r['ret']:+.1f}%, выходов {len(r['exits'])}"), flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(job, jobs)))
    table = {}
    for n in RULES:
        for b in RT.BOTS:
            parts = [res[(b, n, p)] for p in UC.PERIODS]
            if any(isinstance(x, str) for x in parts):
                table[f"{n} | {b}"] = {"error": [x for x in parts if isinstance(x, str)][0][:300]}
                continue
            ex_all = [e for x in parts for e in x["exits"]]
            table[f"{n} | {b}"] = {
                "rule": n, "bot": b, "periods": {p: round(x["ret"], 1) for p, x in zip(UC.PERIODS, parts)},
                "total": round(float(np.prod([1 + x["ret"] / 100 for x in parts]) - 1) * 100, 0),
                "dd_min": round(max(x["dd_min"] for x in parts), 1), "exits": ex_all,
                "oct10": [e for e in ex_all if e["t"].startswith("2025-10-10") or e["t"].startswith("2025-10-11")],
                "jun05": [e for e in ex_all if e["t"].startswith("2026-06-05")]}
    json.dump(table, open(os.path.join(MV.RES, "self_stop.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for b in RT.BOTS:
        print(f"\n=== {b}, «реалистично»: II пол. 2025 (обвал 10.10) / I пол. 2026 / II пол. 2026 → 15 мес., минутная просадка, выходы")
        for n in RULES:
            x = table[f"{n} | {b}"]
            if "error" in x:
                print(f"  {n:<28} ошибка: {x['error'][:100]}")
                continue
            pv = list(x["periods"].values())
            by = {}
            for e in x["exits"]:
                by[e["why"]] = by.get(e["why"], 0) + 1
            o = x["oct10"][0] if x["oct10"] else None
            print(f"  {n:<28} {pv[0]:+7.1f}% / {pv[1]:+6.1f}% / {pv[2]:+6.1f}% → {x['total']:+6.0f}%   просадка {x['dd_min']:5.1f}%   "
                  f"выходов {len(x['exits'])} {by if by else ''}"
                  + (f"   10.10: {o['t'][11:]} ({o['why']}, {o['drop_pct']:+.1f}% от пика)" if o else ""))


if __name__ == "__main__":
    main()
