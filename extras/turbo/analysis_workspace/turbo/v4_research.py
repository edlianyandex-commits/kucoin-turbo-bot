r"""Основной бот TradeBot v4 (EMA Anchor 10 × 8, стоп по монете, выход при обвале BTC): что ещё можно улучшить
(02.10.2026, пользователь: «давай подумаем над оптимизацией… протестируй, пока не добавляй»). Только исследование.

  resume    — возврат после выхода по BTC: сразу, как сигнал погас / через 2, 4, 6, 12 ч вместо 00:00 следующего дня;
  cooldown  — пауза между докупками в одной монете (bot.long.risk.entry_cooldown_minutes): 30 мин, 2 ч, 6 ч;
  params    — своя настройка EMA Anchor: каждый параметр ×0,6 / ×0,8 / ×1,25 / ×1,5 на подборе (II пол. 2025 +
              I пол. 2026); лучшие и их сочетание — на проверке (II пол. 2026, 07–09), устойчивость — половина и полтора
              шага изменения, без 5 самых прибыльных монет;
  universe  — монеты с оборотом от 3 млн $ (102) против нынешних 84 (от 5 млн $).
Условия «реалистично» (turbo/real_test.py), $300, выход BTC −5% за 12 ч: худшая из 5 минут − экспозиция × 0,36%.
Отрезок passivbot идёт от старта до следующего сигнала BTC (+ остаток суток): сигнал от бота не зависит, поэтому
отрезки общие у вариантов с одним правилом возврата. Кэш — turbo/results/v4_research/, итог — v4_research.json.

    venv\Scripts\python turbo\v4_research.py resume|cooldown|params|universe [--workers 6]
"""
import argparse
import glob
import hashlib
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
import coin_hsl as CH  # noqa: E402
import drawdown_study as DS  # noqa: E402
import exit_rules as ER  # noqa: E402
import main_variants as MV  # noqa: E402
import real_test as RT  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

OUT = os.path.join(MV.RES, "v4_research")
RESULT = os.path.join(MV.RES, "v4_research.json")
EA = "bot.long.strategy.ema_anchor."
# нынешний основной бот: EMA Anchor 10 × 8 (UC.OVER), стоп по монете (coin_hsl.coin(1.0)), «реалистично»
BASE = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **RT.LEVELS["реалистично"]["over"], **CH.coin(1.0)}
PERIODS = list(UC.PERIODS)                      # II пол. 2025, I пол. 2026, II пол. 2026
TRAIN, TEST = PERIODS[:2], PERIODS[2:]
TOP5 = ["USELESS", "ZEC", "MYX", "FARTCOIN", "ENA"]   # самые прибыльные монеты 2026 (turbo/main_variants.py)
FLAG = ER.hourly_flag(("any", [("BTC", 0.05, 12)]), {"BTC": RR.btc_hourly()})
ON = FLAG[FLAG].index
LOCKS, GUARD = {}, threading.Lock()


def coins84():
    return UT.universes()["оборот ≥ 5 млн $"]


def coins102():
    rows = json.load(open(os.path.join(HERE, "results", "kucoin_contracts_2026-10-01.json")))
    return [r["coin"] for r in rows if r["binance"] and r["coin"] not in UT.NOT_CRYPTO and r["turnover"] >= 3e6]


def seg(over, start, end, coins):
    """Прогон passivbot start → end: поминутный капитал (доля старта), минуты сделок и экспозиция после них."""
    key = hashlib.md5(json.dumps([over, start, end, sorted(coins)], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    path = os.path.join(OUT, f"seg_{key}.npz")
    with GUARD:
        lock = LOCKS.setdefault(key, threading.Lock())
    with lock:
        if not os.path.exists(path):
            if os.environ.get("TB_FULL") == "1":        # снятые с торгов контракты — в кэш рынков движка
                import delisted_markets as DM
                DM.ensure()
            keep = bool(over.get("__fills"))             # служебные ключи «__…» в TB Core не передаются
            res = DS.backtest_curve({k: v for k, v in over.items() if not k.startswith("__")}, start, end, f"v4r_{key}",
                                    base=UT.EM.EMA, coins=coins)
            if isinstance(res, str):
                return res
            rel, rd = res
            fp = sorted(glob.glob(os.path.join(rd, "*", "*", "fills.csv*")), key=os.path.getmtime)
            f = pd.read_csv(fp[-1]) if fp else pd.DataFrame(columns=["timestamp", "twe_long", "coin", "pnl", "fee_paid"])
            shutil.rmtree(rd, ignore_errors=True)
            fm = ((pd.to_datetime(f["timestamp"]) - pd.Timestamp(start)).dt.total_seconds() // 60).astype("int64").values
            os.makedirs(OUT, exist_ok=True)
            extra = {}
            if keep:                                     # вклад монет: монета и прибыль ($ от ставки) каждой сделки
                extra = {"fill_coin": f["coin"].astype(str).to_numpy(dtype="U40"), "fill_pnl": f["pnl"].astype("float64").values}
            np.savez_compressed(path, rel=rel, fill_min=fm, fill_twe=f["twe_long"].astype("float32").values, **extra)
        z = np.load(path)
        return z["rel"], z["fill_min"], z["fill_twe"]


def next_exit(t, on=None):
    on = ON if on is None else on
    i = on.searchsorted(t, side="right")
    return on[i] if i < len(on) else None


_SIG = {}


def signal_times(key):
    """Выходы: сигнал обвала BTC по закрытию часа (как в панели) + минутный сигнал key из minute_signals.json."""
    if key not in _SIG:
        d = json.load(open(os.path.join(MV.RES, "minute_signals.json"), encoding="utf-8"))
        extra = pd.DatetimeIndex(pd.to_datetime(d[key]))
        _SIG[key] = ON.union(extra).sort_values()
    return _SIG[key]


def resume_at(e, rule):
    """Когда снова запускать бота после выхода в момент e: "day" — 00:00 UTC следующего дня (и дальше по суткам, пока
    сигнал горит), как сейчас в панели; число — через столько часов (и дальше по часу, пока сигнал горит)."""
    if rule == "day":
        r = (e + pd.Timedelta(days=1)).normalize()
        while ER.flag_at(FLAG, r):
            r += pd.Timedelta(days=1)
        return r
    r = e + pd.Timedelta(hours=rule)
    while ER.flag_at(FLAG, r):
        r += pd.Timedelta(hours=1)
    return r


def chain(over, period, coins, rule="day", exit_min=ER.EXIT_MIN, exit_cost=ER.EXIT_COST, shift_days=0, want_curve=False):
    start, end = UC.PERIODS[period]
    t, t_end = pd.Timestamp(start) + pd.Timedelta(days=shift_days), pd.Timestamp(end)
    cap, pieces, exits, paused, expo = 1.0, [], [], 0.0, []
    coin_pnl = {}
    on = signal_times(over["__signal"]) if over.get("__signal") else None
    # «умный» минутный выход: минутный сигнал закрывает всё, только если загрузка (объём позиций / баланс) не меньше
    # порога «__exit_twe»; иначе сигнал пропускается и прогон продолжается без перерыва (часовой выход по BTC — всегда)
    thr = float(over.get("__exit_twe") or 0)
    minute_sig = set(on.difference(ON)) if (thr and on is not None) else set()
    while t < t_end - pd.Timedelta(hours=6):
        e = next_exit(t, on)
        while True:
            stop = t_end if e is None or e >= t_end else min(t_end, (e + pd.Timedelta(days=1)).normalize())
            res = seg(over, t.strftime("%Y-%m-%dT%H:%M"), stop.strftime("%Y-%m-%d"), coins)
            if isinstance(res, str):
                return res
            rel, fmin, ftwe = res
            i = int((e - t) / pd.Timedelta(minutes=1)) if e is not None and e < t_end else None
            if i is None or i >= len(rel) or e not in minute_sig:
                break
            k = np.searchsorted(fmin, i, side="right") - 1
            if (float(ftwe[k]) if k >= 0 else 0.0) >= thr:
                break
            e = next_exit(e, on)
        idx = t + pd.to_timedelta(np.arange(len(rel)), unit="min")
        if over.get("__fills"):                          # вклад монет до выхода по BTC, в долях капитала цепочки
            key = hashlib.md5(json.dumps([over, t.strftime("%Y-%m-%dT%H:%M"), stop.strftime("%Y-%m-%d"), sorted(coins)],
                                         sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            z = np.load(os.path.join(OUT, f"seg_{key}.npz"))
            if "fill_coin" in z:
                m = z["fill_min"] <= (i if i is not None else len(rel))
                for c, p in zip(z["fill_coin"][m], z["fill_pnl"][m]):
                    coin_pnl[str(c)] = coin_pnl.get(str(c), 0.0) + float(p) / MV.STAKE * cap
        if want_curve:                     # загрузка счёта (экспозиция) после каждой сделки — ступенчатый ряд
            expo.append(pd.Series(ftwe, index=t + pd.to_timedelta(fmin, unit="min")))
        if i is None or i >= len(rel):
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        k = np.searchsorted(fmin, i, side="right") - 1
        twe = float(ftwe[k]) if k >= 0 else 0.0
        out = cap * float(rel[i:i + exit_min].min()) * (1 - twe * exit_cost)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        if want_curve:
            expo[-1] = pd.concat([expo[-1][expo[-1].index < e], pd.Series([0.0], index=[e])])
        exits.append(str(e)[:16])
        cap = out
        r = resume_at(e, rule)
        paused += (min(r, t_end) - e) / pd.Timedelta(hours=1)
        pieces.append(pd.Series([cap], index=[r]))
        t = r
    c = pd.concat(pieces)
    c = c[~c.index.duplicated(keep="last")].sort_index()
    v = c.values
    out = {"ret": (v[-1] / v[0] - 1) * 100, "dd_min": float((1 - v / np.maximum.accumulate(v)).max()) * 100,
           "exits": len(exits), "paused_h": round(paused, 1)}
    if coin_pnl:
        out["coin_pnl"] = coin_pnl
    if want_curve:
        out["curve"] = c
        x = pd.concat(expo) if expo else pd.Series(dtype=float)
        out["exposure"] = x[~x.index.duplicated(keep="last")].sort_index()
    return out


def run_jobs(jobs, workers):
    """jobs: {(вариант, период): (over, coins, rule[, параметры chain])} → {(вариант, период): итог}."""
    def job(k):
        over, coins, rule, *kw = jobs[k]
        r = chain(over, k[1], coins, rule, **(kw[0] if kw else {}))
        print(f"  готово {k[0]} | {k[1][:14]}: " + (r[:120] if isinstance(r, str) else
                                                    f"{r['ret']:+.1f}%, просадка {r['dd_min']:.1f}%, выходов {r['exits']}"), flush=True)
        return r
    keys = list(jobs)
    with ThreadPoolExecutor(workers) as ex:
        return dict(zip(keys, ex.map(job, keys)))


def summary(res, name, periods):
    parts = [res.get((name, p)) for p in periods]
    if any(x is None or isinstance(x, str) for x in parts):
        return None
    return {"periods": [round(x["ret"], 1) for x in parts],
            "total": round((np.prod([1 + x["ret"] / 100 for x in parts]) - 1) * 100, 1),
            "dd_min": round(max(x["dd_min"] for x in parts), 1), "exits": sum(x["exits"] for x in parts),
            "paused_h": round(sum(x["paused_h"] for x in parts), 1)}


def table(title, res, names, periods):
    print(f"\n=== {title}\n" + " " * 44 + " / ".join(p[:12] for p in periods) + " → итог, минутная просадка, выходов, пауз ч")
    out = {}
    for n in names:
        s = summary(res, n, periods)
        if s is None:
            errs = [res.get((n, p)) for p in periods if isinstance(res.get((n, p)), str)]
            print(f"  {n:<42} ошибка {errs[0][:100] if errs else ''}")
            continue
        out[n] = s
        print(f"  {n:<42} " + " / ".join(f"{x:+6.1f}%" for x in s["periods"])
              + f" → {s['total']:+6.1f}%  {s['dd_min']:5.1f}%  {s['exits']:3d}  {s['paused_h']:7.1f}")
    return out


def save(section, data):
    allr = json.load(open(RESULT, encoding="utf-8")) if os.path.exists(RESULT) else {}
    allr[section] = data
    json.dump(allr, open(RESULT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


RESUME = {"сейчас: с 00:00 следующего дня": "day", "сразу, как сигнал погас": 0, "через 2 ч": 2, "через 4 ч": 4,
          "через 6 ч": 6, "через 12 ч": 12}
COOLDOWN = {"без паузы (сейчас)": 0.0, "пауза 30 мин": 30.0, "пауза 2 ч": 120.0, "пауза 6 ч": 360.0}


def do_resume(a):
    c = coins84()
    jobs = {(n, p): (BASE, c, rule) for n, rule in RESUME.items() for p in PERIODS}
    save("resume", table("Возврат после выхода по BTC", run_jobs(jobs, a.workers), list(RESUME), PERIODS))


def do_cooldown(a):
    c = coins84()
    jobs = {(n, p): ({**BASE, "bot.long.risk.entry_cooldown_minutes": m}, c, "day") for n, m in COOLDOWN.items() for p in PERIODS}
    save("cooldown", table("Пауза между докупками в одной монете", run_jobs(jobs, a.workers), list(COOLDOWN), PERIODS))


def do_universe(a):
    u = {"84 монеты (оборот от 5 млн $, сейчас)": coins84(), f"{len(coins102())} монеты (оборот от 3 млн $)": coins102()}
    jobs = {(n, p): (BASE, c, "day") for n, c in u.items() for p in PERIODS}
    res = {}
    for p in PERIODS:              # по периоду за раз: новые монеты скачиваются один раз, без гонок за кэш свечей
        res.update(run_jobs({k: v for k, v in jobs.items() if k[1] == p}, min(a.workers, 2)))
    out = table("Набор монет", res, list(u), PERIODS)
    out["_new_coins"] = sorted(set(coins102()) - set(coins84()))
    save("universe", out)


def base_params():
    return json.load(open(UT.EM.EMA, encoding="utf-8"))["bot"]["long"]["strategy"]["ema_anchor"]


def pval(name, v):
    return float(round(v)) if "span" in name else float(round(v, 6))


def do_params(a):
    c = coins84()
    bp = base_params()
    variants = {"сейчас (настройки автора движка)": {}}
    for k, v in bp.items():
        for m in (0.6, 0.8, 1.25, 1.5):
            variants[f"{k} ×{m:g}"] = {EA + k: pval(k, v * m)}
    jobs = {(n, p): ({**BASE, **ov}, c, "day") for n, ov in variants.items() for p in TRAIN}
    res = run_jobs(jobs, a.workers)
    tr = table("Подбор: каждый параметр отдельно (II пол. 2025 + I пол. 2026)", res, list(variants), TRAIN)
    base = tr["сейчас (настройки автора движка)"]
    # лучшие: итог подбора выше нынешнего, ни одно полугодие не хуже, просадка не больше нынешней + 3 п.п.
    good = {n: s for n, s in tr.items() if n != "сейчас (настройки автора движка)" and s["total"] > base["total"]
            and all(x >= y for x, y in zip(s["periods"], base["periods"])) and s["dd_min"] <= base["dd_min"] + 3}
    best_per_param = {}
    for n, s in sorted(good.items(), key=lambda kv: -kv[1]["total"]):
        k = n.split(" ×")[0]
        best_per_param.setdefault(k, (n, s))
    picks = dict(sorted(best_per_param.values(), key=lambda kv: -kv[1]["total"])[:3])
    combo = {}
    for n in best_per_param.values():
        combo.update(variants[n[0]])
    check = {"сейчас (настройки автора движка)": {}}
    for n in picks:
        check[n] = variants[n]
    if len(combo) >= 2:
        check["сочетание лучших"] = combo
        check["сочетание: полшага"] = {k: pval(k, bp[k.split(".")[-1]] + (v - bp[k.split(".")[-1]]) * 0.5) for k, v in combo.items()}
        check["сочетание: полтора шага"] = {k: pval(k, bp[k.split(".")[-1]] + (v - bp[k.split(".")[-1]]) * 1.5) for k, v in combo.items()}
    jobs = {(n, p): ({**BASE, **ov}, c, "day") for n, ov in check.items() for p in PERIODS}
    res2 = run_jobs(jobs, a.workers)
    full = table("Проверка: подбор + II пол. 2026 (не видели при подборе)", res2, list(check), PERIODS)
    c5 = [x for x in c if x not in TOP5]
    jobs = {(n, p): ({**BASE, **ov}, c5, "day") for n, ov in check.items() for p in PERIODS}
    no5 = table("Без 5 самых прибыльных монет (USELESS, ZEC, MYX, FARTCOIN, ENA)", run_jobs(jobs, a.workers), list(check), PERIODS)
    save("params", {"train": tr, "picks": list(picks), "combo": combo, "check": full, "without_top5": no5})


def do_robust(a):
    """Устойчивость найденного сочетания: пессимистичные условия, старт периодов на 10 дней позже, без 5 лучших монет,
    102 монеты."""
    bp, combo = base_params(), json.load(open(RESULT, encoding="utf-8"))["params"]["combo"]
    step = lambda f: {k: pval(k, bp[k.split(".")[-1]] + (v - bp[k.split(".")[-1]]) * f) for k, v in combo.items()}  # noqa: E731
    variants = {"сейчас (настройки автора движка)": {}, "сочетание: полшага": step(0.5), "сочетание лучших": step(1.0),
                "сочетание: полтора шага": step(1.5)}
    pess = RT.LEVELS["пессимистично"]
    base_p = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **pess["over"], **CH.coin(1.0)}
    c84, c102 = coins84(), coins102()
    c5 = [x for x in c84 if x not in TOP5]
    checks = {
        "Пессимистично (проскальзывание 0,5%, комиссии 0,04/0,10%, выход — худшая из 10 мин − экспозиция × 1,1%)":
            lambda ov: (({**base_p, **ov}), c84, "day", {"exit_min": pess["exit_min"], "exit_cost": pess["exit_cost"]}),
        "Старт каждого периода на 10 дней позже": lambda ov: ({**BASE, **ov}, c84, "day", {"shift_days": 10}),
        "Без 5 самых прибыльных монет (USELESS, ZEC, MYX, FARTCOIN, ENA)": lambda ov: ({**BASE, **ov}, c5, "day"),
        f"{len(c102)} монеты (оборот от 3 млн $)": lambda ov: ({**BASE, **ov}, c102, "day"),
    }
    out = {}
    for title, mk in checks.items():
        jobs = {(n, p): mk(ov) for n, ov in variants.items() for p in PERIODS}
        out[title] = table(title, run_jobs(jobs, a.workers), list(variants), PERIODS)
        save("robust", out)


def do_middle(a):
    """Промежуточные условия между «реалистично» и «пессимистично»: комиссии KuCoin как есть (0,02/0,06%), исполнение
    лимитных ордеров с запасом 0,05%, проскальзывание рыночных 0,3%, выход — худшая из 7 минут − экспозиция × 0,6%;
    плюс сколько сделок в сутки у вариантов (реалистично)."""
    bp, combo = base_params(), json.load(open(RESULT, encoding="utf-8"))["params"]["combo"]
    step = lambda f: {k: pval(k, bp[k.split(".")[-1]] + (v - bp[k.split(".")[-1]]) * f) for k, v in combo.items()}  # noqa: E731
    variants = {"сейчас (настройки автора движка)": {}, "сочетание: полшага": step(0.5), "сочетание лучших": step(1.0)}
    mid = {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003}
    base_m = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **mid, **CH.coin(1.0)}
    c84 = coins84()
    jobs = {(n, p): ({**base_m, **ov}, c84, "day", {"exit_min": 7, "exit_cost": 0.0006 + 0.006})
            for n, ov in variants.items() for p in PERIODS}
    out = {"middle": table("Промежуточно (комиссии KuCoin, запас исполнения 0,05%, проскальзывание 0,3%)",
                           run_jobs(jobs, a.workers), list(variants), PERIODS)}
    fills = {}
    for n, ov in variants.items():          # сделок в сутки: первые отрезки трёх периодов, «реалистично» (из кэша)
        cnt = days = 0
        for p in PERIODS:
            start, end = UC.PERIODS[p]
            t = pd.Timestamp(start)
            e = next_exit(t)
            stop = pd.Timestamp(end) if e is None or e >= pd.Timestamp(end) else min(pd.Timestamp(end), (e + pd.Timedelta(days=1)).normalize())
            r = seg({**BASE, **ov}, t.strftime("%Y-%m-%dT%H:%M"), stop.strftime("%Y-%m-%d"), c84)
            if not isinstance(r, str):
                cnt += len(r[1])
                days += len(r[0]) / 1440
        fills[n] = round(cnt / max(days, 1e-9), 1)
    print("\nСделок в сутки (реалистично): " + ", ".join(f"{n} {v}" for n, v in fills.items()))
    out["fills_per_day"] = fills
    save("middle", out)


def do_turbo(a):
    """Та же своя настройка EMA Anchor (подобрана для основного бота) на Turbo 5.0: 7 монет × экспозиция 9, стоп движка
    35% по всему счёту с перезапуском через 3 дня, выход по BTC с паузой до следующего дня, без стопа по монете.
    Условия: реалистично, старт на 10 дней позже, без 5 лучших монет, промежуточно, пессимистично; сделок в сутки."""
    bp, combo = base_params(), json.load(open(RESULT, encoding="utf-8"))["params"]["combo"]
    step = lambda f: {k: pval(k, bp[k.split(".")[-1]] + (v - bp[k.split(".")[-1]]) * f) for k, v in combo.items()}  # noqa: E731
    variants = {"Turbo сейчас (настройки автора движка)": {}, "сочетание: полшага": step(0.5), "сочетание (как у v4)": step(1.0)}
    turbo = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **RT.BOTS["Turbo 5.0 (7 × 9)"]}
    real, pess = RT.LEVELS["реалистично"], RT.LEVELS["пессимистично"]
    mid = {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003}
    c84 = coins84()
    c5 = [x for x in c84 if x not in TOP5]
    checks = {
        "Turbo: реалистично": lambda ov: ({**turbo, **real["over"], **ov}, c84, "day"),
        "Turbo: старт каждого периода на 10 дней позже": lambda ov: ({**turbo, **real["over"], **ov}, c84, "day", {"shift_days": 10}),
        "Turbo: без 5 самых прибыльных монет": lambda ov: ({**turbo, **real["over"], **ov}, c5, "day"),
        "Turbo: промежуточно (комиссии KuCoin, исполнение хуже)":
            lambda ov: ({**turbo, **mid, **ov}, c84, "day", {"exit_min": 7, "exit_cost": 0.0006 + 0.006}),
        "Turbo: пессимистично": lambda ov: ({**turbo, **pess["over"], **ov}, c84, "day",
                                             {"exit_min": pess["exit_min"], "exit_cost": pess["exit_cost"]}),
    }
    out = {}
    for title, mk in checks.items():
        jobs = {(n, p): mk(ov) for n, ov in variants.items() for p in PERIODS}
        out[title] = table(title, run_jobs(jobs, a.workers), list(variants), PERIODS)
        save("turbo", out)
    fills = {}
    for n, ov in variants.items():
        cnt = days = 0
        for p in PERIODS:
            start, end = UC.PERIODS[p]
            t = pd.Timestamp(start)
            e = next_exit(t)
            stop = pd.Timestamp(end) if e is None or e >= pd.Timestamp(end) else min(pd.Timestamp(end), (e + pd.Timedelta(days=1)).normalize())
            r = seg({**turbo, **real["over"], **ov}, t.strftime("%Y-%m-%dT%H:%M"), stop.strftime("%Y-%m-%d"), c84)
            if not isinstance(r, str):
                cnt += len(r[1])
                days += len(r[0]) / 1440
        fills[n] = round(cnt / max(days, 1e-9), 1)
    print("\nСделок в сутки (реалистично): " + ", ".join(f"{n} {v}" for n, v in fills.items()))
    out["fills_per_day"] = fills
    save("turbo", out)


def bots_over():
    """Основной бот (со стопом по монете) и Turbo 5.0 — «реалистично»."""
    return {"основной v4": BASE,
            "Turbo 5.0": {**UC.OVER, "backtest.starting_balance": MV.STAKE, **RT.BOTS["Turbo 5.0 (7 × 9)"],
                          **RT.LEVELS["реалистично"]["over"]}}


def do_walk(a):
    """Проверка «вперёд по времени»: параметры выбраны ТОЛЬКО по II пол. 2025 (тем же правилом: по каждому параметру лучший
    множитель, если он лучше нынешнего и просадка не больше нынешней + 3 п.п.), затем I и II пол. 2026 — оба не видел
    выбор. Оба бота; для сравнения — сочетание, выбранное по году (как сейчас у основного бота)."""
    c = coins84()
    bp = base_params()
    p0 = PERIODS[0]
    variants = {"сейчас": {}}
    for k, v in bp.items():
        for m in (0.6, 0.8, 1.25, 1.5):
            variants[f"{k} ×{m:g}"] = {EA + k: pval(k, v * m)}
    res = run_jobs({(n, p0): ({**BASE, **ov}, c, "day") for n, ov in variants.items()}, a.workers)   # из кэша
    base = res[("сейчас", p0)]
    best = {}
    for n, r in res.items():
        if n[0] == "сейчас" or isinstance(r, str):
            continue
        if r["ret"] > base["ret"] and r["dd_min"] <= base["dd_min"] + 3:
            k = n[0].split(" ×")[0]
            if k not in best or r["ret"] > best[k][1]:
                best[k] = (n[0], r["ret"])
    combo_wf = {}
    for k, (n, _) in best.items():
        combo_wf.update(variants[n])
    print("\nВыбрано по II пол. 2025: " + ", ".join(f"{n} ({ret:+.1f}%)" for n, ret in sorted(best.values(), key=lambda x: -x[1])))
    combo_year = json.load(open(RESULT, encoding="utf-8"))["params"]["combo"]
    check = {"сейчас (настройки автора движка)": {}, "выбор по II пол. 2025": combo_wf, "выбор по году (как в боте)": combo_year}
    out = {"picked": {k: v[0] for k, v in best.items()}, "combo": combo_wf}
    for bot, over in bots_over().items():
        jobs = {(n, p): ({**over, **ov}, c, "day") for n, ov in check.items() for p in PERIODS[1:]}
        out[bot] = table(f"{bot}: I и II пол. 2026 — выбор их не видел", run_jobs(jobs, a.workers), list(check), PERIODS[1:])
        save("walk", out)


def monthly(curve):
    m = curve.resample("ME").last()
    prev = pd.concat([pd.Series([curve.iloc[0]], index=[curve.index[0] - pd.Timedelta(minutes=1)]), m]).shift(1).iloc[1:]
    return (m / prev.values - 1) * 100


def do_monthly(a):
    """Помесячно (15 месяцев, «реалистично»): месяцев в минусе, худший / лучший / средний месяц, худшие два месяца подряд,
    итог без трёх лучших месяцев — для нынешней и новой настройки обоих ботов."""
    c = coins84()
    combo = json.load(open(RESULT, encoding="utf-8"))["params"]["combo"]
    out = {}
    print("\n=== Помесячно, «реалистично», 07.2025–09.2026")
    for bot, over in bots_over().items():
        for name, ov in (("сейчас", {}), ("новая настройка", combo)):
            ms = []
            for p in PERIODS:
                r = chain({**over, **ov}, p, c, "day", want_curve=True)
                if isinstance(r, str):
                    print(f"  {bot}, {name}: ошибка {r[:100]}")
                    break
                ms.append(monthly(r["curve"]))
            else:
                s = pd.concat(ms)
                s.index = [f"{t:%m.%y}" for t in s.index]
                g = (1 + s / 100)
                two = ((g * g.shift(1)) - 1).dropna() * 100
                top3 = g.sort_values().iloc[:-3]
                row = {"months": {k: round(v, 1) for k, v in s.items()}, "losing": int((s < 0).sum()), "n": len(s),
                       "worst": round(float(s.min()), 1), "worst_month": s.idxmin(), "best": round(float(s.max()), 1),
                       "median": round(float(s.median()), 1), "worst_2m": round(float(two.min()), 1),
                       "total": round((float(g.prod()) - 1) * 100), "total_wo_top3": round((float(top3.prod()) - 1) * 100)}
                out[f"{bot} | {name}"] = row
                print(f"  {bot:<12} {name:<16} в минусе {row['losing']} из {row['n']}, худший {row['worst']:+.1f}% "
                      f"({row['worst_month']}), лучший {row['best']:+.1f}%, медиана {row['median']:+.1f}%, худшие 2 мес. "
                      f"{row['worst_2m']:+.1f}%, итог {row['total']:+d}%, без 3 лучших месяцев {row['total_wo_top3']:+d}%")
                print("      " + "  ".join(f"{k} {v:+.0f}%" for k, v in s.items()))
    save("monthly", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["resume", "cooldown", "params", "universe", "robust", "middle", "turbo", "walk", "monthly"])
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    DS.EP.STAKE = MV.STAKE
    {"resume": do_resume, "cooldown": do_cooldown, "params": do_params, "universe": do_universe,
     "robust": do_robust, "middle": do_middle, "turbo": do_turbo, "walk": do_walk, "monthly": do_monthly}[a.what](a)


if __name__ == "__main__":
    main()
