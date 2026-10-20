r"""Правило выхода при обвале: BTC, ETH или индекс наших 84 монет (пользователь, 01.10.2026: «сомнения в целесообразности
привязки к биткоину, можем посмотреть на эфир?» и «найдём крипту, от которой боты зависят больше всего» — turbo/coin_link.py:
сильнее всего боты связаны с равновзвешенным индексом своих 84 монет, BTC — на 55–60-м месте из 82).

Оба бота (Turbo 5.0 7 × 9 и основной v4 10 × 8), 84 монеты, $300, условия «реалистично» (как turbo/real_test.py), три периода.
Правило: закрытие часа ряда на X% ниже максимума закрытий за N часов → закрыть всё (худшая минута из 5 после сигнала,
минус открытая экспозиция × 0,36%), пауза до 00:00 UTC следующего дня, пока сигнал горит — ещё сутки, затем новый запуск.
Ряды: BTC и ETH (Binance 1h, с 06.2021), ИНДЕКС — равновзвешенный по 84 монетам (turbo/coin_link.py, с 25.06.2025).
Сочетания: «и» — нужны оба сигнала, «или» — любой.
Прогоны passivbot кэшируются по (бот, дата старта, конец периода) — первый отрезок у всех правил общий: turbo/results/exit_rules/.
История 2021–2026 (только BTC/ETH-правила): дней с сигналом по годам и когда правило срабатывало на крупных обвалах эфира
(ETH ≥ 15% ниже максимума за 24 ч).

    venv\Scripts\python turbo\exit_rules.py [--workers 6]
Итог: turbo/results/exit_rules.json и таблицы.
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
import coin_link as CL  # noqa: E402
import drawdown_study as DS  # noqa: E402
import main_variants as MV  # noqa: E402
import real_test as RT  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

OUT = os.path.join(MV.RES, "exit_rules")
LEVEL = "реалистично"
EXIT_MIN, EXIT_COST = 5, 0.0006 + 0.003          # как «реалистично» в real_test: худшая из 5 минут, комиссия + проскальзывание
BOT_KEYS = {"Turbo 5.0 (7 × 9)": "turbo", "Основной v4 (10 × 8)": "v4"}
RULES = {
    "без выхода": None,
    "BTC −5% за 12 ч (сейчас)": ("any", [("BTC", 0.05, 12)]),
    "BTC −6% за 12 ч": ("any", [("BTC", 0.06, 12)]),
    "ETH −8% за 12 ч": ("any", [("ETH", 0.08, 12)]),
    "ETH −9% за 12 ч": ("any", [("ETH", 0.09, 12)]),
    "ETH −10% за 12 ч": ("any", [("ETH", 0.10, 12)]),
    "индекс −7% за 12 ч": ("any", [("IDX", 0.07, 12)]),
    "индекс −9% за 12 ч": ("any", [("IDX", 0.09, 12)]),
    "индекс −10% за 12 ч": ("any", [("IDX", 0.10, 12)]),
    "BTC −5% и ETH −8%": ("all", [("BTC", 0.05, 12), ("ETH", 0.08, 12)]),
}


def eth_hourly():
    d = json.load(open(os.path.join(MV.RES, "eth_1h.json")))
    s = pd.Series({int(k): v for k, v in d.items()}).sort_index()
    s.index = pd.to_datetime(s.index, unit="ms")
    return s


def series():
    px = CL.prices()
    hr = px.pct_change(fill_method=None)
    idx = (1 + hr.drop(columns=["BTC", "ETH"]).mean(axis=1, skipna=True).fillna(0)).cumprod() * 100
    return {"BTC": RR.btc_hourly(), "ETH": eth_hourly(), "IDX": idx}


def hourly_flag(rule, S):
    """Сигнал правила по закрытиям часов; индекс — время закрытия часа (открытие + 1 ч)."""
    mode, terms = rule
    flags = []
    for name, drop, hours in terms:
        s = S[name]
        f = (s / s.rolling(hours, min_periods=1).max() - 1) <= -drop
        f.index = f.index + pd.Timedelta(hours=1)
        flags.append(f)
    df = pd.concat(flags, axis=1).fillna(False).astype(bool)
    return df.all(axis=1) if mode == "all" else df.any(axis=1)


def minute_flags(flag, start, n):
    idx = pd.Timestamp(start) + pd.to_timedelta(np.arange(n), unit="min")
    f = flag.astype(float)
    return f.reindex(f.index.union(idx)).ffill().reindex(idx).fillna(0.0).values > 0.5


def flag_at(flag, t):
    v = flag.asof(t)
    return bool(v) if v == v and v is not None else False


SEG_LOCKS, SEG_GUARD = {}, threading.Lock()


def segment(bot, start, end, coins):
    """Прогон passivbot с даты start до конца периода: поминутный капитал (доля старта) и экспозиция после каждой сделки."""
    key = f"{BOT_KEYS[bot]}_{start}_{end}"
    path = os.path.join(OUT, f"seg_{key}.npz")
    with SEG_GUARD:
        lock = SEG_LOCKS.setdefault(key, threading.Lock())
    with lock:
        if not os.path.exists(path):
            over = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **RT.BOTS[bot], **RT.LEVELS[LEVEL]["over"]}
            res = DS.backtest_curve(over, start, end, f"er_{key}".replace("-", ""), base=UT.EM.EMA, coins=coins)
            if isinstance(res, str):
                return res
            rel, rd = res
            fp = sorted(glob.glob(os.path.join(rd, "*", "*", "fills.csv*")), key=os.path.getmtime)
            f = pd.read_csv(fp[-1]) if fp else pd.DataFrame(columns=["timestamp", "twe_long"])
            shutil.rmtree(rd, ignore_errors=True)
            fm = ((pd.to_datetime(f["timestamp"]) - pd.Timestamp(start)).dt.total_seconds() // 60).astype("int64").values
            os.makedirs(OUT, exist_ok=True)
            np.savez_compressed(path, rel=rel, fill_min=fm, fill_twe=f["twe_long"].astype("float32").values)
        z = np.load(path)
        return z["rel"], z["fill_min"], z["fill_twe"]


def chain(bot, rule_name, period, coins, flag):
    start, end = UC.PERIODS[period]
    d, cap, pieces, exits = pd.Timestamp(start), 1.0, [], []
    t_end = pd.Timestamp(end)
    while d < t_end - pd.Timedelta(days=1):
        if flag is not None and flag_at(flag, d):
            d += pd.Timedelta(days=1)
            continue
        res = segment(bot, d.strftime("%Y-%m-%d"), end, coins)
        if isinstance(res, str):
            return res
        rel, fmin, ftwe = res
        idx = d + pd.to_timedelta(np.arange(len(rel)), unit="min")
        hit = np.flatnonzero(minute_flags(flag, d, len(rel))) if flag is not None else np.array([], dtype=int)
        if not hit.size:
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        i = int(hit[0])
        k = np.searchsorted(fmin, i, side="right") - 1
        twe = float(ftwe[k]) if k >= 0 else 0.0
        out = cap * float(rel[i:i + EXIT_MIN].min()) * (1 - twe * EXIT_COST)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        exits.append(str(idx[i])[:16])
        cap = out
        d = (idx[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([cap], index=[d]))
    c = pd.concat(pieces)
    c = c[~c.index.duplicated(keep="last")].sort_index()
    v = c.values
    return {"ret": (v[-1] / v[0] - 1) * 100, "dd_min": float((1 - v / np.maximum.accumulate(v)).max()) * 100,
            "exits": exits, "pause_days": None}


def history(S):
    """2021–2026, только BTC/ETH: дней с сигналом по годам и срабатывание на крупных обвалах эфира."""
    eth = S["ETH"]
    dd24 = eth / eth.rolling(24, min_periods=1).max() - 1
    hits = dd24[dd24 <= -0.15].index
    events, cur = [], []
    for t in hits:
        if cur and t - cur[-1] > pd.Timedelta(hours=48):
            events.append(cur)
            cur = []
        cur.append(t)
    if cur:
        events.append(cur)
    ev = []
    for e in events:
        w = eth[(eth.index >= e[0] - pd.Timedelta(hours=24)) & (eth.index <= e[-1] + pd.Timedelta(hours=24))]
        low_t = w.idxmin()
        high = eth[(eth.index >= e[0] - pd.Timedelta(hours=24)) & (eth.index <= e[0])].max()
        ev.append({"first": e[0], "low_t": low_t, "high": high, "low": float(eth[low_t])})
    out = {"events": [], "per_year": {}}
    for name, rule in RULES.items():
        if rule is None or any(s == "IDX" for s, _, _ in rule[1]):
            continue
        f = hourly_flag(rule, S)
        f = f[(f.index >= "2021-06-02") & (f.index < "2026-09-30")]
        days = pd.Series(sorted({t.date() for t in f[f].index}))
        out["per_year"][name] = {str(y): int((pd.to_datetime(days).dt.year == y).sum()) for y in range(2021, 2027)}
    for e in ev:
        row = {"date": f"{e['first']:%d.%m.%Y}", "eth_drop_pct": round((e["low"] / e["high"] - 1) * 100, 1),
               "low": f"{e['low_t'] + pd.Timedelta(hours=1):%d.%m %H:%M}"}
        for name, rule in RULES.items():
            if rule is None or any(s == "IDX" for s, _, _ in rule[1]):
                continue
            f = hourly_flag(rule, S)
            win = f[(f.index >= e["first"] - pd.Timedelta(hours=24)) & (f.index <= e["low_t"] + pd.Timedelta(hours=1))]
            t = win[win].index.min() if win.any() else None
            if t is None:
                row[name] = "нет"
            else:
                at = float(eth.asof(t - pd.Timedelta(hours=1)))
                row[name] = f"{(at / e['high'] - 1) * 100:+.0f}%"
        out["events"].append(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    DS.EP.STAKE = MV.STAKE
    coins = UT.universes()["оборот ≥ 5 млн $"]
    S = series()
    flags = {n: (hourly_flag(r, S) if r else None) for n, r in RULES.items()}
    jobs = [(b, n, p) for n in RULES for b in RT.BOTS for p in UC.PERIODS]

    def job(j):
        r = chain(j[0], j[1], j[2], coins, flags[j[1]])
        print(f"  готово {j[1]} | {BOT_KEYS[j[0]]} | {j[2][:12]}: "
              + (r[:120] if isinstance(r, str) else f"{r['ret']:+.1f}%, выходов {len(r['exits'])}"), flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(job, jobs)))
    hist = history(S)
    table = {}
    for n in RULES:
        for b in RT.BOTS:
            parts = [res[(b, n, p)] for p in UC.PERIODS]
            if any(isinstance(x, str) for x in parts):
                table[f"{n} | {b}"] = {"error": [x for x in parts if isinstance(x, str)][0][:300]}
                continue
            tot = float(np.prod([1 + x["ret"] / 100 for x in parts]) - 1) * 100
            table[f"{n} | {b}"] = {"rule": n, "bot": b, "periods": {p: round(x["ret"], 1) for p, x in zip(UC.PERIODS, parts)},
                                   "total": round(tot, 0), "dd_min": round(max(x["dd_min"] for x in parts), 1),
                                   "exits": sum(len(x["exits"]) for x in parts),
                                   "exit_times": [t for x in parts for t in x["exits"]]}
    json.dump({"table": table, "history": hist}, open(os.path.join(MV.RES, "exit_rules.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    for b in RT.BOTS:
        print(f"\n=== {b}, условия «{LEVEL}»: II пол. 2025 (обвал 10.10) / I пол. 2026 / II пол. 2026 → 15 мес., "
              f"минутная просадка, выходов")
        for n in RULES:
            x = table[f"{n} | {b}"]
            if "error" in x:
                print(f"  {n:<26} ошибка: {x['error'][:100]}")
                continue
            pv = list(x["periods"].values())
            print(f"  {n:<26} {pv[0]:+7.1f}% / {pv[1]:+6.1f}% / {pv[2]:+6.1f}% → {x['total']:+6.0f}%   "
                  f"просадка {x['dd_min']:5.1f}%   выходов {x['exits']}")
    print("\nИстория 2021–2026: дней с сигналом по годам")
    for n, y in hist["per_year"].items():
        print(f"  {n:<26} " + ", ".join(f"{k}: {v}" for k, v in y.items()))
    print("\nКрупные обвалы ETH (≥15% за 24 ч): ETH к моменту сигнала (от максимума за сутки до обвала) / «нет» — не сработало до дна")
    names = [n for n in hist["per_year"]]
    print("  " + f"{'дата':<12}{'дно ETH':>9}  " + "  ".join(f"{n[:16]:>16}" for n in names))
    for e in hist["events"]:
        print("  " + f"{e['date']:<12}{e['eth_drop_pct']:>8.0f}%  " + "  ".join(f"{e[n]:>16}" for n in names))


if __name__ == "__main__":
    main()
