r"""Стоп passivbot по каждой монете (live.hsl_signal_mode = "coin") — аналог «снижения риска» (derisk) NostalgiaForInfinity
(пользователь, 01.10.2026: «поискать на гитхаб готовых ботов… может что-то взять для нас»).

В режиме coin у каждой монеты свой контроллер: просадка монеты = (пик реализованной прибыли за lookback − последняя
реализованная + текущий нереализованный убыток) / слот (баланс / n_positions). ORANGE — режим монеты (graceful_stop или
tp_only_with_active_entry_cancellation — без докупок, только тейк), RED — закрыть эту монету и пауза; остальные торгуют.
Сейчас у ботов общий стоп 35% по всему счёту (unified), он за 15 месяцев ни разу не сработал; главный хвостовой риск —
обвал одной монеты (ZEC 05.06.2026: Turbo −43% капитала в худшую минуту).

Оба бота, 84 монеты, $300, «реалистично», выход по BTC −5% за 12 ч с запуском со следующего дня, три периода
(как turbo/exit_rules.py; прогоны кэшируются в turbo/results/coin_hsl/).

    venv\Scripts\python turbo\coin_hsl.py [--workers 6]
"""
import argparse
import glob
import json
import os
import shutil
import sys
import threading
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

OUT = os.path.join(MV.RES, "coin_hsl")
H = "bot.long.hsl."


def coin(red, orange_mode="tp_only_with_active_entry_cancellation", orange=0.5, yellow=0.3, ema=60):
    return {"live.hsl_signal_mode": "coin", H + "enabled": True, H + "red_threshold": red,
            H + "restart_after_red_policy": "always", H + "cooldown_minutes_after_red": 4320.0,
            H + "tier_ratios": {"yellow": yellow, "orange": orange}, H + "orange_tier_mode": orange_mode,
            H + "ema_span_minutes": float(ema)}


VARIANTS = {
    "сейчас: общий стоп 35%": {},
    "по монете: красный 1 слот, без докупок с 0,5": coin(1.0),
    # красный уровень в режиме coin — не больше 1 слота (проверка конфига passivbot)
    "по монете: красный 1 слот, без докупок с 0,75": coin(1.0, orange=0.75, yellow=0.5),
    "по монете: красный 1 слот, мягкая остановка с 0,5": coin(1.0, "graceful_stop"),
    "по монете: красный 1 слот, без докупок с 0,5, сглаживание 427 мин": coin(1.0, ema=427),
    "по монете: красный 0,8 слота, без докупок с 0,5": coin(0.8),
}
LOCKS, GUARD = {}, threading.Lock()


def segment(bot, vname, start, end, coins):
    key = f"{ER.BOT_KEYS[bot]}_{list(VARIANTS).index(vname)}_{start}_{end}"
    path = os.path.join(OUT, f"seg_{key}.npz")
    if not VARIANTS[vname]:                                   # базовый вариант — общий кэш turbo/exit_rules
        return ER.segment(bot, start, end, coins)
    with GUARD:
        lock = LOCKS.setdefault(key, threading.Lock())
    with lock:
        if not os.path.exists(path):
            over = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **RT.BOTS[bot], **RT.LEVELS[ER.LEVEL]["over"],
                    **VARIANTS[vname]}
            res = DS.backtest_curve(over, start, end, f"ch_{key}".replace("-", ""), base=UT.EM.EMA, coins=coins)
            if isinstance(res, str):
                return res
            rel, rd = res
            fp = sorted(glob.glob(os.path.join(rd, "*", "*", "fills.csv*")), key=os.path.getmtime)
            f = pd.read_csv(fp[-1]) if fp else pd.DataFrame(columns=["timestamp", "twe_long", "type"])
            shutil.rmtree(rd, ignore_errors=True)
            fm = ((pd.to_datetime(f["timestamp"]) - pd.Timestamp(start)).dt.total_seconds() // 60).astype("int64").values
            panics = int(f["type"].astype(str).str.contains("panic").sum()) if len(f) else 0
            os.makedirs(OUT, exist_ok=True)
            np.savez_compressed(path, rel=rel, fill_min=fm, fill_twe=f["twe_long"].astype("float32").values,
                                panics=np.array([panics]))
        z = np.load(path)
        return z["rel"], z["fill_min"], z["fill_twe"]


def chain(bot, vname, period, coins, flag):
    start, end = UC.PERIODS[period]
    d, cap, pieces, exits = pd.Timestamp(start), 1.0, [], 0
    t_end = pd.Timestamp(end)
    while d < t_end - pd.Timedelta(days=1):
        if ER.flag_at(flag, d):
            d += pd.Timedelta(days=1)
            continue
        res = segment(bot, vname, d.strftime("%Y-%m-%d"), end, coins)
        if isinstance(res, str):
            return res
        rel, fmin, ftwe = res
        idx = d + pd.to_timedelta(np.arange(len(rel)), unit="min")
        hit = np.flatnonzero(ER.minute_flags(flag, d, len(rel)))
        if not hit.size:
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        i = int(hit[0])
        k = np.searchsorted(fmin, i, side="right") - 1
        twe = float(ftwe[k]) if k >= 0 else 0.0
        out = cap * float(rel[i:i + ER.EXIT_MIN].min()) * (1 - twe * ER.EXIT_COST)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        exits += 1
        cap = out
        d = (idx[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([cap], index=[d]))
    c = pd.concat(pieces)
    c = c[~c.index.duplicated(keep="last")].sort_index()
    v = c.values
    jun = c[(c.index >= "2026-06-05") & (c.index < "2026-06-06")]
    return {"ret": (v[-1] / v[0] - 1) * 100, "dd_min": float((1 - v / np.maximum.accumulate(v)).max()) * 100, "exits": exits,
            "jun05_dd": float((1 - jun / c[c.index < "2026-06-06"].cummax().reindex(jun.index)).max() * 100) if len(jun) else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    DS.EP.STAKE = MV.STAKE
    coins = UT.universes()["оборот ≥ 5 млн $"]
    flag = ER.hourly_flag(ER.RULES["BTC −5% за 12 ч (сейчас)"], ER.series())
    jobs = [(b, v, p) for v in VARIANTS for b in RT.BOTS for p in UC.PERIODS]

    def job(j):
        r = chain(*j, coins, flag)
        print(f"  готово {j[1]} | {ER.BOT_KEYS[j[0]]} | {j[2][:12]}: " + (r[:100] if isinstance(r, str) else f"{r['ret']:+.1f}%"), flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(job, jobs)))
    out = {}
    for b in RT.BOTS:
        print(f"\n=== {b}, «реалистично» + выход по BTC: II пол. 2025 / I пол. 2026 / II пол. 2026 → 15 мес., минутная просадка")
        for v in VARIANTS:
            parts = [res[(b, v, p)] for p in UC.PERIODS]
            if any(isinstance(x, str) for x in parts):
                print(f"  {v:<50} ошибка {[x for x in parts if isinstance(x, str)][0][:100]}")
                continue
            tot = (np.prod([1 + x["ret"] / 100 for x in parts]) - 1) * 100
            out[f"{v} | {b}"] = {"periods": [round(x["ret"], 1) for x in parts], "total": round(tot), "dd_min": round(max(x["dd_min"] for x in parts), 1),
                                 "jun05_dd": parts[1]["jun05_dd"]}
            print(f"  {v:<50} " + " / ".join(f"{x['ret']:+6.1f}%" for x in parts) + f" → {tot:+5.0f}%   просадка {max(x['dd_min'] for x in parts):5.1f}%"
                  + (f"   05.06.2026 ZEC: {parts[1]['jun05_dd']:.1f}%" if parts[1]["jun05_dd"] is not None else ""))
    json.dump(out, open(os.path.join(MV.RES, "coin_hsl.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
