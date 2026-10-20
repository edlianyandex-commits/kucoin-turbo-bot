r"""Отбор монет по среднему обороту вместо оборота за одни сутки (02.10.2026, пользователь: «давай протестируем первый
вариант»). Поводок: ARK попал в список 01.10 только из-за разгона и обвала 30.09 (обычно ≈ 60 тыс. $ в сутки на KuCoin,
в день обвала 4,3 млн $, при пороге 5 млн $ по скользящим 24 ч). Только исследование, боты не меняются.

  data   — дневной оборот (USDT) всех бессрочных USDT-фьючерсов KuCoin, которые есть на Binance, с 01.03.2025
           (публичный API kline/query, granularity 1440) → results/kucoin_turnover_daily.json;
  lists  — какие монеты проходят порог 5 млн $ по правилам: «сутки» (как сейчас), среднее за 7 / 14 / 28 дней,
           медиана за 28 дней — сегодня и на начало каждого месяца 07.2025–09.2026;
  test   — оба бота «реалистично», $300, выход по BTC: список пересобирается 1-го числа каждого месяца по правилу
           (без заглядывания вперёд: только оборот до этого дня), отрезки passivbot — от начала месяца до сигнала BTC /
           конца месяца; плюс нынешний неподвижный список из 84 монет для сравнения.

    venv\Scripts\python turbo\turnover_rule.py data|lists|test [--workers 6]
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import universe_test as UT  # noqa: E402

RES = os.path.join(HERE, "results")
# Полный период (03.10.2026, пользователь: «подключим 2024 год»): TB_FULL=1 — месяцы с 01.2024, кандидаты со снятыми с
# торгов контрактами, свои файлы данных и списков (прежние 15-месячные результаты не меняются).
FULL = os.environ.get("TB_FULL") == "1"
DATA = os.path.join(RES, "kucoin_turnover_daily_full.json" if FULL else "kucoin_turnover_daily.json")
OUTF = os.path.join(RES, "turnover_rule.json")
CONTRACTS = os.path.join(RES, "kucoin_contracts_2026-10-01.json")
# Порог. Сводка KuCoin contracts/active.turnoverOf24h (по ней отобраны нынешние 84 монеты, ≥ 5 млн $) примерно вдвое
# больше оборота дневных свечей kline (медиана ×1,97 по 153 монетам 30.09–01.10; BTC 633 против 309 млн $ за одни 24 ч) —
# видимо, считает обе стороны сделки. Тот же уровень ликвидности по свечам — ≈ 2,5 млн $ в сутки.
MIN_T = 2.5e6
FROM = "2023-11-01" if FULL else "2025-03-01"
FIRST_MONTH = "2024-01-01" if FULL else "2025-07-01"
LISTS_KEY = "lists_full" if FULL else "lists"
DELISTED = os.path.join(RES, "delisted_kucoin.json")
DELISTED_SPECS = os.path.join(RES, "delisted_contracts.json")
RULES = {"сутки (сейчас)": ("mean", 1), "среднее 7 дн.": ("mean", 7), "среднее 14 дн.": ("mean", 14),
         "среднее 28 дн.": ("mean", 28), "медиана 28 дн.": ("median", 28),
         "стабильно 3 мес.": ("stable", 3)}      # среднее каждого из трёх последних 28-дневных окон ≥ порога


def candidates():
    rows = json.load(open(CONTRACTS, encoding="utf-8"))
    out = [(r["coin"], r["symbol"]) for r in rows if r["binance"] and r["coin"] not in UT.NOT_CRYPTO]
    if FULL and os.path.exists(DELISTED):                # снятые с KuCoin контракты (свечи KuCoin отдаёт и по ним)
        have = {c for c, _ in out}
        for c, r in json.load(open(DELISTED, encoding="utf-8")).items():
            if not r.get("still_active") and r.get("kucoin_days", 0) > 0 and c not in have and c not in UT.NOT_CRYPTO:
                out.append((c, r["symbol"]))
    return out


def onboard_map():
    """Дата появления монеты (мс) для фильтра «старше года»: Binance из снимка контрактов; у снятых — дата открытия
    контракта KuCoin (Binance-даты для них нет)."""
    m = {r["coin"]: r.get("binance_onboard") for r in json.load(open(CONTRACTS, encoding="utf-8"))}
    if FULL and os.path.exists(DELISTED_SPECS):
        for c, s in json.load(open(DELISTED_SPECS, encoding="utf-8")).items():
            if not m.get(c) and s.get("firstOpenDate"):
                m[c] = int(s["firstOpenDate"])
    return m


def get(url, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tb-research"})
            d = json.load(urllib.request.urlopen(req, timeout=30))
            if str(d.get("code")) == "200000":
                return d["data"]
            raise RuntimeError(d)
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 + 3 * k)


HL = os.path.join(RES, "kucoin_daily_hl_full.json" if FULL else "kucoin_daily_hl.json")   # дневные max / min / закрытие


def fetch_hl(item):
    coin, sym = item
    out = {}
    t0 = int(pd.Timestamp(FROM, tz="UTC").timestamp() * 1000)
    end = int(time.time() * 1000)
    while t0 < end:
        t1 = min(end, t0 + 190 * 86_400_000)
        rows = get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1440&from={t0}&to={t1}")
        for r in rows:
            out[pd.Timestamp(r[0], unit="ms").strftime("%Y-%m-%d")] = [float(r[2]), float(r[3]), float(r[4])]
        t0 = t1
        time.sleep(0.15)
    return coin, out


def do_data_hl(a):
    cand = candidates()
    with ThreadPoolExecutor(4) as ex:
        res = dict(ex.map(fetch_hl, cand))
    json.dump(res, open(HL, "w", encoding="utf-8"))
    print(f"монет: {len(res)} → {HL}")


def hot_coins(day, ratio, days=30):
    """Разогнанные монеты на день day: максимум дневного закрытия за прошлые days дней ≥ ratio × минимума (дневные свечи
    KuCoin; по закрытиям, а не по экстремумам — проколы вроде 10.10.2025, когда альты на минуты падали на 80%, не в счёт)."""
    if not hasattr(hot_coins, "hl"):
        hot_coins.hl = json.load(open(HL, encoding="utf-8"))
        hot_coins.memo = {}
    key = (str(day)[:10], float(ratio), days)
    if key in hot_coins.memo:
        return hot_coins.memo[key]
    lo = (pd.Timestamp(day) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    hi = pd.Timestamp(day).strftime("%Y-%m-%d")
    out = set()
    for c, d in hot_coins.hl.items():
        w = [v for k, v in d.items() if lo <= k < hi]                 # даты «ГГГГ-ММ-ДД» сравниваются как строки
        if w and min(x[2] for x in w) > 0 and max(x[2] for x in w) / min(x[2] for x in w) >= ratio:
            out.add(c)
    hot_coins.memo[key] = out
    return out


RISK_FLAGS = {"возраст<1,5г": "моложе 1,5 года на Binance", "возраст<2г": "моложе 2 лет на Binance",
              "вол≥8%": "дневная волатильность за 30 дней ≥ 8%", "оборот×5": "оборот за неделю ≥ 5× обычного (60 дней до неё)",
              "мёртвая": "цена ≤ 15% максимума за год", "7дн×2": "цена за 7 дней менялась вдвое и больше"}


def risk_flags(month, coins):
    """Признаки «опасной» монеты на 1-е число месяца (по дневным закрытиям и обороту KuCoin до этого дня)."""
    key = (month, tuple(coins))
    cache = risk_flags.__dict__.setdefault("cache", {})
    if key in cache:
        return cache[key]
    if "closes" not in risk_flags.__dict__:
        hl = json.load(open(HL, encoding="utf-8"))
        cl = pd.DataFrame({c: pd.Series({k: v[2] for k, v in d.items()}) for c, d in hl.items() if d})
        cl.index = pd.to_datetime(cl.index)
        risk_flags.closes, risk_flags.turn = cl.sort_index(), table()
        risk_flags.onboard = {r["coin"]: r.get("binance_onboard") for r in json.load(open(CONTRACTS, encoding="utf-8"))}
    cl, tu, day = risk_flags.closes, risk_flags.turn, pd.Timestamp(month + "-01")
    win = lambda df, a, b=0: df[(df.index >= day - pd.Timedelta(days=a)) & (df.index < day - pd.Timedelta(days=b))]  # noqa: E731
    vol = np.log(win(cl, 30)).diff().std()
    w7, wy = win(cl, 7), win(cl, 365)
    t7, t60 = win(tu, 7).mean(), win(tu, 67, 7).mean()
    out = {}
    for c in coins:
        ob = risk_flags.onboard.get(c)
        age = (day - pd.Timestamp(ob, unit="ms")).days if ob else 0
        f = []
        if age < 540:
            f.append("возраст<1,5г")
        if age < 730:
            f.append("возраст<2г")
        if c in vol and vol[c] >= 0.08:
            f.append("вол≥8%")
        if c in t7 and c in t60 and t60[c] > 0 and t7[c] / t60[c] >= 5:
            f.append("оборот×5")
        if c in wy and wy[c].max() > 0 and len(wy[c].dropna()) and wy[c].dropna().iloc[-1] / wy[c].max() <= 0.15:
            f.append("мёртвая")
        if c in w7 and w7[c].min() > 0 and w7[c].max() / w7[c].min() >= 2:
            f.append("7дн×2")
        out[c] = f
    cache[key] = out
    return out


def fetch(item):
    coin, sym = item
    out = {}
    t0 = int(pd.Timestamp(FROM, tz="UTC").timestamp() * 1000)
    end = int(time.time() * 1000)
    while t0 < end:
        t1 = min(end, t0 + 190 * 86_400_000)   # KuCoin отдаёт не больше 200 свечей за запрос
        rows = get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1440&from={t0}&to={t1}")
        for r in rows:
            out[pd.Timestamp(r[0], unit="ms").strftime("%Y-%m-%d")] = float(r[6])
        t0 = t1
        time.sleep(0.15)
    return coin, out


def fetch_both(item):
    """Дневные свечи KuCoin одним проходом: оборот (для списка) и [max, min, закрытие] (для фильтра разогнанных)."""
    coin, sym = item
    turn, hl = {}, {}
    t0 = int(pd.Timestamp(FROM, tz="UTC").timestamp() * 1000)
    end = int(time.time() * 1000)
    while t0 < end:
        t1 = min(end, t0 + 190 * 86_400_000)
        rows = get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1440&from={t0}&to={t1}")
        for r in rows:
            d = pd.Timestamp(r[0], unit="ms").strftime("%Y-%m-%d")
            turn[d] = float(r[6])
            hl[d] = [float(r[2]), float(r[3]), float(r[4])]
        t0 = t1
        time.sleep(0.1)
    return coin, turn, hl


def do_data_both(a):
    cand = candidates()
    turn, hl = {}, {}
    with ThreadPoolExecutor(a.workers) as ex:
        for i, (coin, t, h) in enumerate(ex.map(fetch_both, cand), 1):
            turn[coin], hl[coin] = t, h
            if i % 50 == 0:
                print(f"  {i}/{len(cand)}", flush=True)
    json.dump(turn, open(DATA, "w", encoding="utf-8"))
    json.dump(hl, open(HL, "w", encoding="utf-8"))
    print(f"монет: {len(turn)}, с данными: {sum(1 for v in turn.values() if v)} → {DATA}, {HL}")


def do_data(a):
    cand = candidates()
    res = {}
    with ThreadPoolExecutor(4) as ex:
        for i, (coin, d) in enumerate(ex.map(fetch, cand), 1):
            res[coin] = d
            if i % 50 == 0:
                print(f"  {i}/{len(cand)}", flush=True)
    json.dump(res, open(DATA, "w", encoding="utf-8"))
    print(f"монет: {len(res)}, с данными: {sum(1 for v in res.values() if v)} → {DATA}")


def table():
    """Дневной оборот: строки — дни (UTC), столбцы — монеты; сегодняшний неполный день отброшен."""
    d = json.load(open(DATA, encoding="utf-8"))
    df = pd.DataFrame({c: pd.Series(v) for c, v in d.items() if v})
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df[df.index < pd.Timestamp.utcnow().tz_localize(None).normalize()]


def rule_list(df, day, rule):
    """Монеты, проходящие порог по обороту ДО дня day (не включая его): правило = (mean|median, дней)."""
    kind, n = RULES[rule]
    if kind == "stable":                                   # ликвидность держится n окон по 28 дней подряд
        w = df[df.index < pd.Timestamp(day)].tail(28 * n)
        if len(w) < 28 * n:
            return []
        ok = pd.Series(True, index=w.columns)
        for k in range(n):
            part = w.iloc[28 * k:28 * (k + 1)]
            ok &= (part.mean() >= MIN_T) & (part.notna().sum() >= 28)
        return sorted(ok[ok].index)
    w = df[df.index < pd.Timestamp(day)].tail(n)
    if len(w) < n:
        return []
    v = w.median() if kind == "median" else w.mean()      # дни без торгов (NaN) не участвуют; монеты моложе n дней — по
    full = w.notna().sum() >= n                            # тем дням, что есть, поэтому требуем полные n дней истории
    return sorted(v[(v >= MIN_T) & full].index)


def months():
    out = []
    for m in pd.date_range(FIRST_MONTH, "2026-09-01", freq="MS"):
        e = min(m + pd.offsets.MonthBegin(1), pd.Timestamp("2026-09-30"))
        out.append((m, e))
    return out


def do_lists(a):
    df = table()
    today = df.index[-1] + pd.Timedelta(days=1)
    cur = set(UT.universes()["оборот ≥ 5 млн $"]) if not FULL else set()
    res = {"today": {}, "monthly": {}}
    print(f"Сегодня (по {df.index[-1]:%d.%m} включительно), порог {MIN_T / 1e6:g} млн $ по свечам (≈ 5 млн $ по сводке 24 ч); нынешний список 84 (снимок 01.10):")
    for r in RULES:
        L = set(rule_list(df, today, r))
        res["today"][r] = sorted(L)
        print(f"  {r:16s} {len(L):3d} монет; убрать из нынешних: {', '.join(sorted(cur - L)) or '—'}; новых: {', '.join(sorted(L - cur)) or '—'}")
    tail = df.tail(28)
    print("\nОборот последних 28 дней у монет на границе (млн $): среднее / медиана / вчера / максимум за день")
    for c in sorted(cur, key=lambda c: tail[c].mean() if c in tail else 0)[:15]:
        s = tail[c] / 1e6
        print(f"  {c:10s} {s.mean():6.2f} {s.median():6.2f} {s.iloc[-1]:6.2f} {s.max():7.2f}")
    print("\nНа начало месяца (без заглядывания вперёд): число монет / сколько из них не проходят «среднее 28 дн.»")
    for m, _ in months():
        k = f"{m:%Y-%m}"
        res["monthly"][k] = {r: rule_list(df, m, r) for r in RULES}
        lens = " ".join(f"{len(res['monthly'][k][r]):3d}" for r in RULES)
        spike = sorted(set(res["monthly"][k]["сутки (сейчас)"]) - set(res["monthly"][k]["среднее 28 дн."]))
        print(f"  {k}: {lens}   только по суткам: {', '.join(spike) or '—'}")
    print("  (столбцы: " + ", ".join(RULES) + ")")
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr[LISTS_KEY] = res
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


TEST_RULES = ["сутки (сейчас)", "среднее 7 дн.", "среднее 28 дн."]
STATIC = "нынешние 84 (неподвижный список)"


def vol_top(month, coins, k, days=30):
    """K самых волатильных монет из coins на 1-е число месяца: разброс дневной доходности (ст. откл. логарифмов
    дневных закрытий KuCoin) за прошлые days дней; монеты без данных не берутся."""
    if "closes" not in risk_flags.__dict__:
        risk_flags(month, [])                                     # загрузит дневные закрытия
    cl, day = risk_flags.closes, pd.Timestamp(month + "-01")
    w = cl[(cl.index >= day - pd.Timedelta(days=days)) & (cl.index < day)]
    vol = np.log(w.where(w > 0)).diff().std()
    ranked = sorted((c for c in coins if c in vol and np.isfinite(vol[c])), key=lambda c: -vol[c])
    return ranked[:k]


def month_lists(rule):
    """Список на каждый месяц по правилу; монеты моложе года на Binance отбрасываются заранее (TB Core их всё равно
    не берёт — minimum_coin_age_days 365), чтобы не качать лишние свечи."""
    import v4_research as VR
    if rule.startswith("risk|"):                       # «risk|<список>|<признак>» — список без монет с этим признаком
        _, base, flag = rule.split("|")
        return {m: [c for c in L if flag not in risk_flags(m, L).get(c, [])] for m, L in month_lists(base).items()}
    if rule.startswith("alive|"):                      # «alive|<список>» — без снятых потом с торгов (как раньше)
        gone = {c for c, r in json.load(open(DELISTED, encoding="utf-8")).items() if not r.get("still_active")}
        return {m: [c for c in L if c not in gone] for m, L in month_lists(rule[6:]).items()}
    if rule.startswith("ex|"):                         # «ex|<список>|A,B» — список без этих монет
        _, base, coins = rule.split("|")
        drop = set(coins.split(","))                   # в сделках TB Core пишет «BONK» вместо «1000BONK»
        return {m: [c for c in L if c not in drop and c.lstrip("0123456789") not in drop] for m, L in month_lists(base).items()}
    if rule.startswith("vol:"):                        # «vol:<список>:<K>» — K самых волатильных монет списка
        base, k = rule[4:].rsplit(":", 1)
        return {m: vol_top(m, L, int(k)) for m, L in month_lists(base).items()}
    if rule.startswith("hot:"):                        # «hot:<список>:<порог>» — список без разогнанных на 1-е число монет
        _, base, ratio = rule.split(":")
        return {m: [c for c in L if c not in hot_coins(m + "-01", float(ratio))] for m, L in month_lists(base).items()}
    if rule == STATIC:
        return {f"{m:%Y-%m}": VR.coins84() for m, _ in months()}
    lists = json.load(open(OUTF, encoding="utf-8"))[LISTS_KEY]["monthly"]
    onboard = onboard_map()
    out = {}
    for m, L in lists.items():
        ms = pd.Timestamp(m + "-01")
        out[m] = [c for c in L[rule] if onboard.get(c) and (ms - pd.Timestamp(onboard[c], unit="ms")).days >= 365]
    return out


def month_starts(shift=0):
    """Начало торговли в каждом месяце: 1-е число 00:00 UTC (или shift дней позже — «сдвиг старта»: месяц с 1+shift
    числа до 1+shift следующего, список монет — на 1-е число этого месяца), но не раньше возврата после выхода по BTC в
    прошлом месяце (сигнал от бота не зависит, поэтому старты общие для всех вариантов)."""
    import exit_rules as ER
    import v4_research as VR
    out, prev = {}, None
    for ms, me in months():
        if shift:
            ms, me = ms + pd.Timedelta(days=shift), min(me + pd.Timedelta(days=shift), pd.Timestamp("2026-09-30"))
            if ms >= me:
                continue
        t = ms
        if prev is not None:
            ex = VR.ON[(VR.ON >= prev[0]) & (VR.ON < prev[1])]
            if len(ex):
                t = max(t, VR.resume_at(ex[-1], "day"))
        while ER.flag_at(VR.FLAG, t):
            t += pd.Timedelta(days=1)
        out[f"{ms:%Y-%m}"] = (t, me)
        prev = (ms, me)
    return out


def month_job(over, coins, key, t, me):
    """Один месяц: цепочка VR.chain от t до конца месяца (выходы по BTC внутри), капитал с 1,0; в конце месяца позиции
    закрываются (список монет меняется) — минус экспозиция × 0,36%, как при выходе по BTC."""
    import exit_rules as ER
    import universe_chain as UC
    import v4_research as VR
    if not coins or t >= me - pd.Timedelta(hours=6):
        return {"curve": pd.Series([1.0, 1.0], index=[t, me]), "close": 1.0, "exits": 0}
    over = dict(over)
    exit_min, exit_cost = over.pop("__exit_min", ER.EXIT_MIN), over.pop("__exit_cost", ER.EXIT_COST)
    if over.pop("__no_btc_exit", False):          # без выхода по BTC: месяц одним прогоном TB Core
        res = VR.seg(over, t.strftime("%Y-%m-%dT%H:%M"), me.strftime("%Y-%m-%d"), coins)
        if isinstance(res, str):
            return res
        rel, fmin, ftwe = res
        curve = pd.Series(rel, index=t + pd.to_timedelta(np.arange(len(rel)), unit="min"))
        return {"curve": curve, "close": 1 - (float(ftwe[-1]) if len(ftwe) else 0.0) * exit_cost, "exits": 0}
    UC.PERIODS[key] = (t.strftime("%Y-%m-%dT%H:%M"), me.strftime("%Y-%m-%d"))
    r = VR.chain(over, key, coins, "day", exit_min=exit_min, exit_cost=exit_cost, want_curve=True)
    if isinstance(r, str):
        return r
    x = r["exposure"]
    twe = float(x[x.index < me].iloc[-1]) if len(x[x.index < me]) else 0.0
    return {"curve": r["curve"], "close": 1 - twe * exit_cost, "exits": r["exits"], "coin_pnl": r.get("coin_pnl", {}),
            "exposure": x}


def do_test(a):
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    bots = {b: {**o, **combo} for b, o in VR.bots_over().items()}
    starts = month_starts()
    variants = TEST_RULES + [STATIC]
    lists = {v: month_lists(v) for v in variants}
    jobs = [(v, b, m) for b in bots for v in variants for m in starts]

    def job(k):
        v, b, m = k
        t, me = starts[m]
        r = month_job(bots[b], lists[v][m], f"tr|{b}|{v}|{m}", t, me)
        if isinstance(r, str):
            print(f"  ОШИБКА {b} | {v} | {m}: {r[:160]}", flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(job, jobs)))
    out = {}
    for b in bots:
        print(f"\n=== {b}: список пересобирается 1-го числа по обороту KuCoin до этого дня (≥ {MIN_T / 1e6:g} млн $ по свечам)")
        for v in variants:
            parts = [res[(v, b, m)] for m in starts]
            if any(isinstance(p, str) for p in parts):
                print(f"  {v}: есть ошибки — перезапустите (готовые отрезки в кэше)")
                continue
            cap, pieces, monthly = 1.0, [], []
            for m, p in zip(starts, parts):
                c = p["curve"] * cap
                pieces.append(c)
                end = float(c.iloc[-1]) * p["close"]
                monthly.append(round((end / cap - 1) * 100, 1))
                cap = end
            curve = pd.concat(pieces)
            vals = curve.values
            dd = float((1 - vals / np.maximum.accumulate(vals)).max()) * 100
            halves = {"II пол. 2025": monthly[:6], "I пол. 2026": monthly[6:12], "II пол. 2026": monthly[12:]}
            hp = {h: round((np.prod([1 + x / 100 for x in ms]) - 1) * 100, 1) for h, ms in halves.items()}
            n = [len(lists[v][m]) for m in starts]
            out.setdefault(b, {})[v] = {"total": round((cap - 1) * 100, 1), "halves": hp, "dd_min": round(dd, 1),
                                        "monthly": monthly, "coins": n, "exits": sum(p["exits"] for p in parts)}
            print(f"  {v:34s} итог {(cap - 1) * 100:+7.1f}% | " + " / ".join(f"{x:+.1f}" for x in hp.values())
                  + f" | минутная просадка {dd:.1f}% | в минусе {sum(x < 0 for x in monthly)} мес., худший {min(monthly):+.1f}% "
                  + f"| монет {min(n)}–{max(n)}")
            print("      " + "  ".join(f"{m[2:]} {x:+.0f}%" for m, x in zip(starts, monthly)))
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["test"] = out
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


H = "bot.long.hsl."
RK = "bot.long.risk."
MONTHLY = "сутки (сейчас)"


def coin_stop(red, ema, orange_slot=0.5):
    """Стоп по монете (passivbot hsl в режиме coin; доля = баланс / позиций): RED — закрыть только эту монету рыночным
    ордером и не торговать её 3 дня; без докупок — с orange_slot доли (как сейчас у основного бота), но не выше 0,8 RED."""
    o = min(orange_slot / red, 0.8)
    return {"live.hsl_signal_mode": "coin", H + "enabled": True, H + "red_threshold": red,
            H + "restart_after_red_policy": "always", H + "cooldown_minutes_after_red": 4320.0,
            H + "tier_ratios": {"yellow": round(o * 0.6, 3), "orange": round(o, 3)},
            H + "orange_tier_mode": "tp_only_with_active_entry_cancellation", H + "ema_span_minutes": float(ema),
            H + "panic_close_order_type": "market"}


# предел доли монеты: правка TB Core (passivbot-rust/src/strategies/ema_anchor.rs, cap_entry_qty_by_wel) — докупка не
# поднимает долю монеты выше её предела (TWEL / позиций); upstream этот ключ для EMA Anchor не применял
CAP = {RK + "position_exposure_enforcer_enabled": True, RK + "position_exposure_enforcer_threshold": 1.0}
STAGES = {
    "1": {"1а: стоп по монете 0,8 доли, сглаживание 10 мин": coin_stop(0.8, 10),
          "1б: стоп по монете 0,5 доли, сглаживание 10 мин": coin_stop(0.5, 10),
          "1в: стоп по монете 0,8 доли, сглаживание 2 мин": coin_stop(0.8, 2)},
    "2": {"2: предел доли монеты в движке": CAP},
}


def run_grid(grid, workers, shift=0, detail=False):
    """grid: {(вариант, бот, список): (over, {месяц: монеты})} → {(вариант, бот, список): итог по месяцам}.
    detail — ещё вклад монет (если в over есть «__fills»), худшая минута (когда) и запас до ликвидации в неё."""
    starts = month_starts(shift)
    jobs = [(k, m) for k in grid for m in starts]

    def job(item):
        k, m = item
        over, lists = grid[k]
        t, me = starts[m]
        r = month_job(over, lists[m], "tr|" + "|".join(k) + f"|{m}|{shift}", t, me)
        if isinstance(r, str):
            print(f"  ОШИБКА {' | '.join(k)} | {m}: {r[:160]}", flush=True)
        return r
    with ThreadPoolExecutor(workers) as ex:
        flat = dict(zip(jobs, ex.map(job, jobs)))
    out = {}
    for k in grid:
        parts = [flat[(k, m)] for m in starts]
        if any(isinstance(p, str) for p in parts):
            out[k] = None
            continue
        cap, pieces, monthly = 1.0, [], []
        for p in parts:
            c = p["curve"] * cap
            pieces.append(c)
            end = float(c.iloc[-1]) * p["close"]
            monthly.append(round((end / cap - 1) * 100, 1))
            cap = end
        curve = pd.concat(pieces)
        vals = curve.values
        hp = halves_of(list(starts), monthly)
        out[k] = {"total": round((cap - 1) * 100, 1), "halves": hp, "monthly": monthly,
                  "dd_min": round(float((1 - vals / np.maximum.accumulate(vals)).max()) * 100, 1),
                  "coins": [len(grid[k][1][m]) for m in starts]}
        if detail:
            ddv = 1 - vals / np.maximum.accumulate(vals)
            j = int(np.argmax(ddv))
            tw = pd.Timestamp(curve.index[j])
            # запас до ликвидации в худшую минуту: капитал / объём позиций; объём ≈ экспозиция после последней сделки ×
            # капитал перед падением (с запасом — цены к этой минуте уже ниже, и объём на деле меньше)
            pk = float(np.maximum.accumulate(vals)[j])
            twe, cp = 0.0, {}
            for m, p in zip(starts, parts):
                x = p.get("exposure")
                if x is not None and len(x[x.index <= tw]) and starts[m][0] <= tw:
                    twe = float(x[x.index <= tw].iloc[-1])
                for c, v in (p.get("coin_pnl") or {}).items():
                    cp[c] = cp.get(c, 0.0) + v
            out[k].update({"dd_when": str(tw)[:16], "twe_at_dd": round(twe, 2),
                           "equity_to_notional": round(float(vals[j]) / (twe * pk), 4) if twe > 0 else None,
                           "coin_pnl": dict(sorted(cp.items(), key=lambda kv: -kv[1])[:12])})
    return out, list(starts)


def halves_of(month_keys, monthly):
    """Итог по календарным полугодиям («II пол. 2025» …) по месячным результатам (ключи «ГГГГ-ММ»)."""
    hp = {}
    for m, x in zip(month_keys, monthly):
        y, mm = m.split("-")
        h = f"{'I' if int(mm) <= 6 else 'II'} пол. {y}"
        hp[h] = hp.get(h, 1.0) * (1 + x / 100)
    return {h: round((v - 1) * 100, 1) for h, v in hp.items()}


def show(title, res, months_):
    print(f"\n=== {title}")
    for k, r in res.items():
        name = " | ".join(k)
        if r is None:
            print(f"  {name}: есть ошибки — перезапустите (готовые отрезки в кэше)")
            continue
        print(f"  {name:78s} итог {r['total']:+8.1f}% | " + " / ".join(f"{x:+.1f}" for x in r["halves"].values())
              + f" | мин. просадка {r['dd_min']:.1f}% | в минусе {sum(x < 0 for x in r['monthly'])} мес., худший {min(r['monthly']):+.1f}%")
        print("      " + "  ".join(f"{m[2:]} {x:+.0f}%" for m, x in zip(months_, r["monthly"])))


def do_protect(a):
    """Защита от обвала одной монеты: этап 1 — быстрый стоп по монете, 2 — предел доли в движке, 12 — оба (--stop
    имя варианта из этапа 1), 123 — оба + список «стабильно 3 мес.». Список монет: честный помесячный («сутки», как сейчас
    отбирает kucoin_refresh.py) и нынешние 84 (сколько стоит защита на привычном списке)."""
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    bots = {b: {**o, **combo} for b, o in VR.bots_over().items()}
    if a.stage in STAGES:
        variants, list_modes = STAGES[a.stage], (MONTHLY, STATIC)
    else:
        stop = STAGES["1"][a.stop]
        variants = {f"1+2: {a.stop.split(':')[0]} + предел доли": {**stop, **CAP}}
        list_modes = (MONTHLY, STATIC) if a.stage == "12" else ("стабильно 3 мес.",)
    if a.stage == "123":
        variants = {f"1+2+3: {a.stop.split(':')[0]} + предел доли, список «стабильно 3 мес.»": {**stop, **CAP}}
    lists = {lm: month_lists(lm) for lm in list_modes}
    grid = {(v, b, lm): ({**bots[b], **extra}, lists[lm]) for v, extra in variants.items() for b in bots for lm in list_modes}
    res, months_ = run_grid(grid, a.workers)
    for b in bots:
        show(f"{b}: этап {a.stage}", {k: r for k, r in res.items() if k[1] == b}, months_)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr.setdefault("protect", {})
    for k, r in res.items():
        allr["protect"][" | ".join(k)] = r
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def account_stop(red, ema):
    """Быстрый стоп всего счёта (hsl unified): просадка капитала от пика ≥ red (сглаживание ema мин) → закрыть всё рыночным,
    3 дня пауза, затем торговать дальше; без докупок с 0,8 порога."""
    return {"live.hsl_signal_mode": "unified", H + "enabled": True, H + "red_threshold": red,
            H + "restart_after_red_policy": "always", H + "cooldown_minutes_after_red": 4320.0,
            H + "tier_ratios": {"yellow": 0.5, "orange": 0.8}, H + "orange_tier_mode": "tp_only_with_active_entry_cancellation",
            H + "ema_span_minutes": float(ema), H + "panic_close_order_type": "market"}


# Turbo 5.0 сейчас: предел доли монеты (CAP) + медленный общий стоп 35%. Что ещё защищает от обвала разогнанной монеты.
TURBO = {
    "сейчас: предел доли": ({}, (MONTHLY, STATIC)),
    "А: + стоп по монете 0,95 доли, 2 мин": (coin_stop(0.95, 2), (MONTHLY, STATIC)),
    "Б: + быстрый стоп счёта 30%, 5 мин": (account_stop(0.30, 5), (MONTHLY, STATIC)),
    "В: + быстрый стоп счёта 20%, 5 мин": (account_stop(0.20, 5), (MONTHLY, STATIC)),
    "Г: предел доли 0,7 (≤ 0,9 баланса)": ({RK + "position_exposure_enforcer_threshold": 0.7}, (MONTHLY, STATIC)),
    "Д: предел доли 0,5 (≤ 0,64 баланса)": ({RK + "position_exposure_enforcer_threshold": 0.5}, (MONTHLY, STATIC)),
    "Е: + без разогнанных (×3 за 30 дн.)": ({}, (f"hot:{MONTHLY}:3", f"hot:{STATIC}:3")),
    "Ж: + без разогнанных (×2 за 30 дн.)": ({}, (f"hot:{MONTHLY}:2", f"hot:{STATIC}:2")),
    "З: + без разогнанных (×4 за 30 дн.)": ({}, (f"hot:{MONTHLY}:4", f"hot:{STATIC}:4")),
    "И: Е + стоп по монете 0,8 доли, 2 мин": (coin_stop(0.8, 2), (f"hot:{MONTHLY}:3", f"hot:{STATIC}:3")),
    "К: Е + быстрый стоп счёта 20%, 5 мин": (account_stop(0.20, 5), (f"hot:{MONTHLY}:3", f"hot:{STATIC}:3")),
}


def do_turbo(a):
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    bot = "Turbo 5.0"
    base = {**VR.bots_over()[bot], **combo, **CAP}
    names = [n for n in TURBO if not a.only or any(n.startswith(x) for x in a.only.split(","))]
    lists = {lm: month_lists(lm) for n in names for lm in TURBO[n][1]}
    grid = {(n, bot, lm): ({**base, **TURBO[n][0]}, lists[lm]) for n in names for lm in TURBO[n][1]}
    res, months_ = run_grid(grid, a.workers)
    show("Turbo 5.0: защита от обвала разогнанной монеты (везде предел доли монеты)", res, months_)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr.setdefault("turbo", {})
    for k, r in res.items():
        allr["turbo"][" | ".join(k)] = r
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def do_author(a):
    """Своя настройка EMA Anchor (02.10.2026, подобрана на сегодняшнем списке 84 монет) против авторской и полшага между
    ними — в честной проверке (список по месяцам) и с нынешними защитами: основной — быстрый стоп по монете (1в),
    Turbo — предел доли + фильтр разогнанных ×3."""
    import v4_research as VR
    bp, combo = VR.base_params(), json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    step = lambda f: {k: VR.pval(k, bp[k.split(".")[-1]] + (v - bp[k.split(".")[-1]]) * f) for k, v in combo.items()}  # noqa: E731
    params = {"автор движка": {}, "полшага": step(0.5), "своя (сейчас в ботах)": combo}
    bo = VR.bots_over()
    setups = {"основной v4": ({**bo["основной v4"], **coin_stop(0.8, 2)}, (MONTHLY, STATIC)),
              "Turbo 5.0": ({**bo["Turbo 5.0"], **CAP}, (f"hot:{MONTHLY}:3", f"hot:{STATIC}:3"))}
    lists = {lm: month_lists(lm) for _, lms in setups.values() for lm in lms}
    grid = {(n, b, lm): ({**over, **pv}, lists[lm]) for b, (over, lms) in setups.items() for n, pv in params.items() for lm in lms}
    res, months_ = run_grid(grid, a.workers)
    for b in setups:
        show(f"{b}: настройка EMA Anchor с нынешними защитами", {k: r for k, r in res.items() if k[1] == b}, months_)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["author"] = {" | ".join(k): r for k, r in res.items()}
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def do_risk(a):
    """Turbo с нынешними настройками и защитами (своя настройка, предел доли, фильтр разогнанных ×3) + ещё один признак
    «опасной» монеты (RISK_FLAGS): честно (список по месяцам) и на нынешних 84."""
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    over = {**VR.bots_over()["Turbo 5.0"], **combo, **CAP}
    hon, sta = f"hot:{MONTHLY}:3", f"hot:{STATIC}:3"
    variants = {"сейчас (фильтр ×3)": (hon, sta), **{f"+ без: {d}": (f"risk|{hon}|{k}", f"risk|{sta}|{k}") for k, d in RISK_FLAGS.items()}}
    lists = {lm: month_lists(lm) for pair in variants.values() for lm in pair}
    grid = {(n, "Turbo 5.0", lm): (over, lists[lm]) for n, pair in variants.items() for lm in pair}
    res, months_ = run_grid(grid, a.workers)
    show("Turbo 5.0: дополнительный отбор монет (поверх предела доли и фильтра разогнанных ×3)", res, months_)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["risk"] = {" | ".join(k): r for k, r in res.items()}
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


SUPER = {
    "Turbo сейчас: 7 × 9": {},
    "5 позиций × 9": {"bot.long.risk.n_positions": 5.0},
    "4 позиции × 9": {"bot.long.risk.n_positions": 4.0},
    "3 позиции × 9": {"bot.long.risk.n_positions": 3.0},
    "3 позиции × 9 + стоп по монете 0,8": {"bot.long.risk.n_positions": 3.0, **coin_stop(0.8, 2)},
}


def do_super(a):
    """«Супер Турбо» (03.10.2026, пользователь: «высокие ожидания, но и риски»): меньше позиций при той же экспозиции 9
    (плечо 10 — предел пользователя) поверх нынешнего Turbo (своя настройка, предел доли = 9 / позиций, фильтр
    разогнанных ×3, выход по BTC). Честно (список по месяцам) и на нынешних 84."""
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    base = {**VR.bots_over()["Turbo 5.0"], **combo, **CAP}
    names = [n for n in SUPER if not a.only or any(n.startswith(x) for x in a.only.split(","))]
    lists = {lm: month_lists(lm) for lm in (f"hot:{MONTHLY}:3", f"hot:{STATIC}:3")}
    grid = {(n, "Turbo 5.0", lm): ({**base, **SUPER[n]}, lists[lm]) for n in names for lm in lists}
    res, months_ = run_grid(grid, a.workers)
    show("Супер Турбо: меньше позиций при экспозиции 9 (поверх нынешних защит Turbo)", res, months_)
    for k, r in res.items():
        if r:
            m = sorted(r["monthly"])
            print(f"  {' | '.join(k)[:70]:70s} медиана месяца {m[len(m) // 2]:+.1f}%, месяцев ≤ −20%: {sum(x <= -20 for x in m)}, "
                  f"лучший {max(m):+.0f}%, ×2 за месяц: {sum(x >= 100 for x in m)}")
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["super"] = {" | ".join(k): r for k, r in res.items()}
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


EA = "bot.long.strategy.ema_anchor."
SUPEROPT = {
    "Супер 5 × 9 (сейчас)": ({}, "3"),
    "6 позиций": ({"bot.long.risk.n_positions": 6.0}, "3"),
    "экспозиция 10": ({"bot.long.risk.total_wallet_exposure_limit": 10.0}, "3"),
    "первая покупка 9%": ({EA + "base_qty_pct": 0.09}, "3"),
    "первая покупка 6%": ({EA + "base_qty_pct": 0.06}, "3"),
    "докупка 3,5": ({EA + "entry_double_down_factor": 3.5}, "3"),
    "докупка 2,2": ({EA + "entry_double_down_factor": 2.2}, "3"),
    "средние короче ×0,8": ({EA + "ema_span_0": 266.0, EA + "ema_span_1": 381.0}, "3"),
    "средние длиннее ×1,25": ({EA + "ema_span_0": 415.0, EA + "ema_span_1": 595.0}, "3"),
    "отступ 1,5%": ({EA + "offset": 0.015}, "3"),
    "отступ 2,5%": ({EA + "offset": 0.025}, "3"),
    "фильтр разогнанных ×4": ({}, "4"),
    "без стопа 35%": ({"bot.long.hsl.enabled": False}, "3"),
}


def do_superopt(a):
    """Супер Турбо на максимальный месячный рост (03.10.2026): от 5 × 9 по одному параметру, честно (список по месяцам);
    засчитывается то, что лучше в каждом из трёх полугодий."""
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    base = {**VR.bots_over()["Turbo 5.0"], **combo, **CAP, "bot.long.risk.n_positions": 5.0}
    variants = SUPEROPT if not a.combo else json.load(open(a.combo, encoding="utf-8"))
    names = [n for n in variants if not a.only or any(n.startswith(x) for x in a.only.split(","))]
    grid = {}
    for n in names:
        extra, ratio = variants[n]
        lm = f"hot:{MONTHLY}:{ratio}"
        grid[(n, "Turbo 5.0", lm)] = ({**base, **extra}, month_lists(lm))
    res, months_ = run_grid(grid, a.workers)
    show("Супер Турбо: подбор на месячный рост (честно, список по месяцам)", res, months_)
    b = next((r for k, r in res.items() if k[0].startswith("Супер 5 × 9")), None)
    for k, r in res.items():
        if r and b:
            m = sorted(r["monthly"])
            geo = ((1 + r["total"] / 100) ** (1 / len(m)) - 1) * 100
            better = sum(x > y for x, y in zip(r["halves"].values(), b["halves"].values()))
            print(f"  {k[0]:28s} в месяц {geo:+5.1f}% | медиана {m[len(m) // 2]:+5.1f}% | лучше 5 × 9 в {better} из 3 полугодий | "
                  f"мин. просадка {r['dd_min']:.0f}% | худший месяц {min(m):+.0f}%")
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr.setdefault("superopt", {}).update({" | ".join(k): r for k, r in res.items()})
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


LEV3 = {"3 × 9": 9.0, "3 × 6": 6.0, "3 × 3": 3.0}
LEV3_LISTS = {"все монеты": "", "20 волатильных": "20", "10 волатильных": "10"}


def do_lev3(a):
    """3 позиции при плече 10 (03.10.2026, пользователь: «плечо х10, максимум 3 позиции, валюты из списка волатильных»):
    своя настройка Turbo, предел доли (экспозиция / 3 на монету), фильтр разогнанных ×3, выход по BTC, стоп 35%; список —
    все монеты месяца или K самых волатильных за прошлые 30 дней (после фильтра разогнанных). Честно (список по месяцам)."""
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    base = {**VR.bots_over()["Turbo 5.0"], **combo, **CAP, "bot.long.risk.n_positions": 3.0}
    hon = f"hot:{MONTHLY}:3"
    grid = {}
    for n, tw in LEV3.items():
        for ln, k in LEV3_LISTS.items():
            name = f"{n}, {ln}"
            if a.only and not any(name.startswith(x) for x in a.only.split(",")):
                continue
            lm = f"vol:{hon}:{k}" if k else hon
            over = {**base, "bot.long.risk.total_wallet_exposure_limit": tw}
            if tw == 9.0 and not k:             # тот же прогон, что «3 позиции × 9» в do_super (кэш)
                over = {**VR.bots_over()["Turbo 5.0"], **combo, **CAP, "bot.long.risk.n_positions": 3.0}
            grid[(name, "Turbo 5.0", lm)] = (over, month_lists(lm))
    res, months_ = run_grid(grid, a.workers)
    show("3 позиции, плечо 10: экспозиция и список волатильных (честно, список по месяцам)", res, months_)
    for k, r in res.items():
        if r:
            m = sorted(r["monthly"])
            geo = ((1 + r["total"] / 100) ** (1 / len(m)) - 1) * 100
            print(f"  {k[0]:28s} в месяц {geo:+5.1f}% | медиана {m[len(m) // 2]:+5.1f}% | ×2 за месяц {sum(x >= 100 for x in m)} | "
                  f"месяцев ≤ −20%: {sum(x <= -20 for x in m)} | мин. просадка {r['dd_min']:.0f}% | худший месяц {min(m):+.0f}%")
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr.setdefault("lev3", {}).update({" | ".join(k): r for k, r in res.items()})
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


SW_H = "bot.long.hsl."
SW_S = "bot.short."


def sweep_variants(bot):
    """Варианты «по одной настройке» вокруг нынешнего бота: {имя: (группа, правки, правило списка или None)}."""
    import v4_research as VR
    bp = VR.base_params()
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    cur = {k: (combo.get(EA + k, v) if bot != "v4" else v) for k, v in bp.items()}
    n0, tw0 = {"v4": (10, 8.0), "turbo": (7, 9.0), "super": (5, 9.0)}[bot]
    V = {}

    def add(group, name, over, lst=None):
        V[name] = (group, over, lst)
    g = "Монеты и экспозиция"
    add(g, "монет меньше: " + str({"v4": 7, "turbo": 5, "super": 4}[bot]), {RK + "n_positions": float({"v4": 7, "turbo": 5, "super": 4}[bot])})
    add(g, "монет больше: " + str({"v4": 14, "turbo": 10, "super": 7}[bot]), {RK + "n_positions": float({"v4": 14, "turbo": 10, "super": 7}[bot])})
    add(g, f"экспозиция ниже: {6 if bot != 'v4' else 5}", {RK + "total_wallet_exposure_limit": 6.0 if bot != "v4" else 5.0})
    if bot == "v4":
        add(g, "экспозиция выше: 9", {RK + "total_wallet_exposure_limit": 9.0})
    g = "Стоп (SL)"
    if bot == "v4":
        tb = VR.bots_over()["Turbo 5.0"]
        unified = {k: v for k, v in tb.items() if k.startswith(SW_H) or k == "live.hsl_signal_mode"}
        add(g, "общий стоп 35% вместо стопа по монете", {**unified, "live.hsl_signal_mode": "unified"})
        add(g, "стоп по монете 0,5 доли", coin_stop(0.5, 2))
        add(g, "стоп по монете 1,0 доли, медленный (как раньше)", {"__drop_coin_stop": True})
    else:
        add(g, "без стопа", {SW_H + "enabled": False})
        add(g, "общий стоп 25%", {SW_H + "red_threshold": 0.25})
        add(g, "общий стоп 50%", {SW_H + "red_threshold": 0.5})
        add(g, "+ быстрый стоп по монете 0,8 доли", coin_stop(0.8, 2))
    g = "Вход и выход (TP)"
    for k, m, nm in (("offset", 0.7, "отступ от средней ×0,7 (вход и продажа ближе)"), ("offset", 1.4, "отступ от средней ×1,4 (дальше)"),
                     ("ema", 0.7, "средние короче ×0,7 (раньше входит)"), ("ema", 1.4, "средние длиннее ×1,4"),
                     ("offset_psize_weight", 0.5, "сдвиг от размера позиции ×0,5"), ("offset_psize_weight", 2.0, "сдвиг от размера позиции ×2"),
                     ("offset_volatility_1h_weight", 0.0, "без поправки на волатильность"), ("offset_volatility_1h_weight", 2.0, "поправка на волатильность ×2")):
        if k == "ema":
            add(g, nm, {EA + "ema_span_0": round(cur["ema_span_0"] * m, 1), EA + "ema_span_1": round(cur["ema_span_1"] * m, 1)})
        else:
            add(g, nm, {EA + k: round(cur[k] * m, 6)})
    g = "Докупки"
    add(g, "первая покупка ×0,6 (больше докупок)", {EA + "base_qty_pct": round(cur["base_qty_pct"] * 0.6, 5)})
    add(g, "первая покупка ×1,5 (меньше докупок)", {EA + "base_qty_pct": round(cur["base_qty_pct"] * 1.5, 5)})
    add(g, "докупки равными частями (удвоение 0)", {EA + "entry_double_down_factor": 0.0})
    add(g, "докупки крупнее ×1,5", {EA + "entry_double_down_factor": round(cur["entry_double_down_factor"] * 1.5, 4)})
    g = "Выбор монет"
    if bot == "v4":
        add(g, "+ фильтр разогнанных ×3", {}, f"hot:{MONTHLY}:3")
        add(g, "+ фильтр разогнанных ×5", {}, f"hot:{MONTHLY}:5")
        base_l = MONTHLY
    else:
        add(g, "без фильтра разогнанных", {}, f"hot:{MONTHLY}:1000")
        add(g, "фильтр разогнанных ×2", {}, f"hot:{MONTHLY}:2")
        add(g, "фильтр разогнанных ×4", {}, f"hot:{MONTHLY}:4")
        add(g, "фильтр разогнанных ×5", {}, f"hot:{MONTHLY}:5")
        base_l = f"hot:{MONTHLY}:3"
    add(g, "только 20 самых волатильных", {}, f"vol:{base_l}:20")
    add(g, "только монеты старше 2 лет", {}, f"risk|{base_l}|возраст<2г")
    W = "bot.long.forager.score_weights"
    add(g, "отбор только по волатильности", {W: {"ema_readiness": 0.0, "volatility": 1.0, "volume": 0.0}})
    add(g, "отбор только по объёму", {W: {"ema_readiness": 0.0, "volatility": 0.0, "volume": 1.0}})
    g = "Лонг / шорт"
    short = {SW_S + "risk.n_positions": float(n0), "live.hedge_mode": True,
             SW_S + "risk.position_exposure_enforcer_enabled": True, SW_S + "risk.position_exposure_enforcer_threshold": 1.0,
             SW_S + "hsl.enabled": True, SW_S + "hsl.red_threshold": 0.35, SW_S + "hsl.restart_after_red_policy": "always",
             SW_S + "hsl.tier_ratios": {"yellow": 0.3, "orange": 0.5}}
    add(g, "лонг + шорт поровну", {**short, RK + "total_wallet_exposure_limit": tw0 / 2, SW_S + "risk.total_wallet_exposure_limit": tw0 / 2})
    add(g, "только шорт", {**short, RK + "total_wallet_exposure_limit": 0.0, SW_S + "risk.total_wallet_exposure_limit": tw0})
    g = "Обвал BTC"
    add(g, "без выхода при обвале BTC", {"__no_btc_exit": True})
    g = "Проскальзывание и комиссии"
    add(g, "как в бэктесте (почти без проскальзывания)", {"backtest.limit_order_fill_buffer_pct": 0.0,
                                                          "backtest.market_order_slippage_pct": 0.0005, "__exit_min": 3, "__exit_cost": 0.0006})
    add(g, "средне (запас 0,05%, проскальзывание 0,3%)", {"backtest.limit_order_fill_buffer_pct": 0.0005,
                                                          "backtest.market_order_slippage_pct": 0.003, "__exit_min": 7, "__exit_cost": 0.0066})
    add(g, "пессимистично (запас 0,1%, 0,5%, комиссии ×2)", {"backtest.limit_order_fill_buffer_pct": 0.001,
                                                             "backtest.market_order_slippage_pct": 0.005, "backtest.maker_fee_override": 0.0004,
                                                             "backtest.taker_fee_override": 0.001, "__exit_min": 10, "__exit_cost": 0.011})
    return V


def sweep_base(bot):
    import v4_research as VR
    bo = VR.bots_over()
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    if bot == "v4":
        return {**bo["основной v4"], **coin_stop(0.8, 2)}, MONTHLY
    over = {**bo["Turbo 5.0"], **CAP, **combo}
    if bot == "super":
        over[RK + "n_positions"] = 5.0
    return over, f"hot:{MONTHLY}:3"


def do_sweep(a):
    """Перебор по одной настройке вокруг нынешних ботов (v4, Turbo 5.0, Супер Турбо); результаты — по ходу, после
    каждого бота, в turnover_rule.json["sweep"][бот] и results/turnover_sweep_<бот>.log."""
    import v4_research as VR
    bots = a.bots.split(",")
    for bot in bots:
        base, base_l = sweep_base(bot)
        V = {"нынешний": ("", {}, None), **sweep_variants(bot)}
        if a.only:
            V = {k: v for k, v in V.items() if k == "нынешний" or any(k.startswith(x) for x in a.only.split(","))}
        grid, groups = {}, {}
        for name, (group, ov, lst) in V.items():
            ov = dict(ov)
            over = dict(base)
            if ov.pop("__drop_coin_stop", False):
                over = dict(VR.bots_over()["основной v4"])
            over.update(ov)
            lm = lst or base_l
            grid[(name, bot, lm)] = (over, month_lists(lm))
            groups[name] = group
        res, months_ = run_grid(grid, a.workers)
        show(f"{bot}: перебор настроек (честно, список по месяцам, «реалистично», выход по BTC)", res, months_)
        b = res[next(k for k in res if k[0] == "нынешний")]
        allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
        allr.setdefault("sweep", {})[bot] = {k[0]: {**(r or {}), "group": groups[k[0]], "list": k[2]} for k, r in res.items()}
        json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        if b:
            print(f"\n  относительно нынешнего ({bot}): итог {b['total']:+.0f}%, полугодия " + " / ".join(f"{x:+.0f}" for x in b["halves"].values()))
            for k, r in res.items():
                if r and k[0] != "нынешний":
                    better = sum(x > y for x, y in zip(r["halves"].values(), b["halves"].values()))
                    print(f"  {groups[k[0]][:26]:26s} {k[0][:52]:52s} итог {r['total']:+8.0f}% ({r['total'] - b['total']:+7.0f}) | "
                          f"лучше в {better} из 3 полугодий | мин. просадка {r['dd_min']:5.1f}% | худший месяц {min(r['monthly']):+5.1f}%", flush=True)


PS_EA = "bot.long.strategy.ema_anchor.offset_psize_weight"


def ps_base():
    over, lm = sweep_base("super")
    return over, lm


def report(title, res, months_, base_key=None):
    show(title, res, months_)
    b = res.get(base_key) if base_key else None
    for k, r in res.items():
        if not r:
            continue
        m = r["monthly"]
        geo = ((1 + r["total"] / 100) ** (1 / len(m)) - 1) * 100
        extra = ""
        if b and k != base_key:
            extra = f" | лучше нынешнего в {sum(x > y for x, y in zip(r['halves'].values(), b['halves'].values()))} из 3 полугодий"
        if "dd_when" in r:
            extra += (f" | худшая минута {r['dd_when']} UTC, экспозиция {r['twe_at_dd']}, капитал/объём "
                      f"{(r['equity_to_notional'] or 0) * 100:.1f}%")
        print(f"  {k[0][:60]:60s} в месяц {geo:+5.1f}% | месяцев в минусе {sum(x < 0 for x in m)}, худший {min(m):+.1f}%{extra}", flush=True)


def do_psize(a):
    """Супер Турбо, сдвиг продажи от размера позиции (offset_psize_weight) точнее (03.10.2026, пользователь): сетка
    множителей, издержки, со стопом по монете / 6 монетами / фильтром ×2, вклад монет, без 5 лучших, сдвиг старта,
    выбор множителя только по первым двум полугодиям (проверка на июле–сентябре 2026)."""
    over0, lm0 = ps_base()
    cur = over0.get(PS_EA, 0.28608)
    lists0 = month_lists(lm0)
    MULTS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    grid = {}
    for m in MULTS:
        grid[(f"×{m:g} ({cur * m:.4f})", "super", lm0)] = ({**over0, PS_EA: round(cur * m, 6), "__fills": True}, lists0)
    for m in (0.5, 0.7):
        for cn, cost in (("средне", {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003,
                                     "__exit_min": 7, "__exit_cost": 0.0066}),
                         ("пессимистично", {"backtest.limit_order_fill_buffer_pct": 0.001, "backtest.market_order_slippage_pct": 0.005,
                                            "backtest.maker_fee_override": 0.0004, "backtest.taker_fee_override": 0.001,
                                            "__exit_min": 10, "__exit_cost": 0.011})):
            grid[(f"×{m:g}, {cn}", "super", lm0)] = ({**over0, PS_EA: round(cur * m, 6), **cost}, lists0)
    for cn, cost in (("средне", {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003,
                                 "__exit_min": 7, "__exit_cost": 0.0066}),
                     ("пессимистично", {"backtest.limit_order_fill_buffer_pct": 0.001, "backtest.market_order_slippage_pct": 0.005,
                                        "backtest.maker_fee_override": 0.0004, "backtest.taker_fee_override": 0.001,
                                        "__exit_min": 10, "__exit_cost": 0.011})):
        grid[(f"×1 (как сейчас), {cn}", "super", lm0)] = ({**over0, **cost}, lists0)
    grid[("×0,5 + быстрый стоп по монете 0,8", "super", lm0)] = ({**over0, PS_EA: round(cur * 0.5, 6), **coin_stop(0.8, 2)}, lists0)
    grid[("×0,5, 6 монет", "super", lm0)] = ({**over0, PS_EA: round(cur * 0.5, 6), RK + "n_positions": 6.0}, lists0)
    lm2 = f"hot:{MONTHLY}:2"
    grid[("×0,5, фильтр разогнанных ×2", "super", lm2)] = ({**over0, PS_EA: round(cur * 0.5, 6)}, month_lists(lm2))
    res, months_ = run_grid(grid, a.workers, detail=True)
    base_key = next(k for k in res if k[0].startswith("×1 ("))
    report("Супер Турбо: сдвиг продажи от размера позиции (честно, «реалистично»)", res, months_, base_key)
    for k, r in res.items():
        if r and r.get("coin_pnl"):
            print(f"  {k[0][:24]:24s} вклад монет (доли стартового капитала): " +
                  ", ".join(f"{c} {v:+.1f}" for c, v in list(r["coin_pnl"].items())[:8]))
    # выбор множителя только по II 2025 + I 2026 → проверка на II 2026
    print("\n  выбор по первым двум полугодиям (июль–сентябрь 2026 выбор не видит):")
    for k, r in res.items():
        if r and k[0].startswith("×") and "," not in k[0] and "+" not in k[0]:
            h = list(r["halves"].values())
            tr = ((1 + h[0] / 100) * (1 + h[1] / 100) - 1) * 100
            print(f"    {k[0]:22s} обучение {tr:+7.0f}% | проверка II 2026 {h[2]:+6.0f}% | худшая минута −{r['dd_min']:.0f}%")
    # без 5 лучших монет и сдвиг старта для ×1, ×0,7, ×0,5
    grid2 = {}
    for m in (1.0, 0.7, 0.5):
        k = next(x for x in res if x[0].startswith(f"×{m:g} ("))
        top = list((res[k] or {}).get("coin_pnl", {}))[:5]
        lmx = f"ex|{lm0}|{','.join(top)}"
        grid2[(f"×{m:g} без {', '.join(top)}", "super", lmx)] = ({**over0, PS_EA: round(cur * m, 6)}, month_lists(lmx))
    res2, months2 = run_grid(grid2, a.workers)
    report("Супер Турбо: без 5 самых прибыльных монет", res2, months2)
    grid3 = {(f"×{m:g}, старт 11-го числа", "super", lm0): ({**over0, PS_EA: round(cur * m, 6)}, lists0) for m in (1.0, 0.7, 0.5)}
    res3, months3 = run_grid(grid3, a.workers, shift=10)
    report("Супер Турбо: сдвиг старта (месяцы с 11-го числа)", res3, months3)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["psize"] = {" | ".join(k): r for d in (res, res2, res3) for k, r in d.items()}
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def do_tcombo(a):
    """Turbo 5.0: «первая покупка ×1,5 + средние ×0,7» вместе, соседние значения, сдвиг старта, издержки, без 5 лучших
    монет (03.10.2026, пользователь)."""
    import v4_research as VR
    over0, lm0 = sweep_base("turbo")
    lists0 = month_lists(lm0)
    cur = {k: over0.get(EA + k, v) for k, v in VR.base_params().items()}

    def v(bq, ema, extra=None):
        o = {**over0, EA + "base_qty_pct": round(cur["base_qty_pct"] * bq, 5),
             EA + "ema_span_0": round(cur["ema_span_0"] * ema, 1), EA + "ema_span_1": round(cur["ema_span_1"] * ema, 1)}
        return {**o, **(extra or {})}
    grid = {("нынешний", "turbo", lm0): ({**over0, "__fills": True}, lists0)}
    for bq in (1.0, 1.25, 1.5, 1.75):
        for ema in (0.6, 0.7, 0.8, 1.0):
            if bq == 1.0 and ema == 1.0:
                continue
            extra = {"__fills": True} if (bq, ema) == (1.5, 0.7) else None
            grid[(f"первая ×{bq:g} + средние ×{ema:g}", "turbo", lm0)] = (v(bq, ema, extra), lists0)
    mid = {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003, "__exit_min": 7, "__exit_cost": 0.0066}
    pes = {"backtest.limit_order_fill_buffer_pct": 0.001, "backtest.market_order_slippage_pct": 0.005, "backtest.maker_fee_override": 0.0004,
           "backtest.taker_fee_override": 0.001, "__exit_min": 10, "__exit_cost": 0.011}
    for cn, cost in (("средне", mid), ("пессимистично", pes)):
        grid[(f"нынешний, {cn}", "turbo", lm0)] = ({**over0, **cost}, lists0)
        grid[(f"первая ×1,5 + средние ×0,7, {cn}", "turbo", lm0)] = (v(1.5, 0.7, cost), lists0)
    res, months_ = run_grid(grid, a.workers, detail=True)
    report("Turbo 5.0: первая покупка × средние (честно, «реалистично»)", res, months_, ("нынешний", "turbo", lm0))
    for k in (("нынешний", "turbo", lm0), ("первая ×1.5 + средние ×0.7", "turbo", lm0)):
        r = res.get(k)
        if r and r.get("coin_pnl"):
            print(f"  {k[0]:28s} вклад монет: " + ", ".join(f"{c} {x:+.1f}" for c, x in list(r["coin_pnl"].items())[:8]))
    grid2, grid3 = {}, {}
    for name, bq, ema in (("нынешний", 1.0, 1.0), ("первая ×1.5 + средние ×0.7", 1.5, 0.7)):
        k = next(x for x in res if x[0] == name)
        top = list((res[k] or {}).get("coin_pnl", {}))[:5]
        lmx = f"ex|{lm0}|{','.join(top)}"
        grid2[(f"{name} без {', '.join(top)}", "turbo", lmx)] = (v(bq, ema), month_lists(lmx))
        grid3[(f"{name}, старт 11-го числа", "turbo", lm0)] = (v(bq, ema), lists0)
    res2, m2 = run_grid(grid2, a.workers)
    report("Turbo 5.0: без 5 самых прибыльных монет", res2, m2)
    res3, m3 = run_grid(grid3, a.workers, shift=10)
    report("Turbo 5.0: сдвиг старта (месяцы с 11-го числа)", res3, m3)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["tcombo"] = {" | ".join(k): r for d in (res, res2, res3) for k, r in d.items()}
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def full_variants():
    """Боты на полном периоде (как сейчас) и проверочные варианты Супер Турбо: {имя: (over, правило списка)}."""
    sup, lm = sweep_base("super")
    tur, lt = sweep_base("turbo")
    v4, l4 = sweep_base("v4")
    mid = {"backtest.limit_order_fill_buffer_pct": 0.0005, "backtest.market_order_slippage_pct": 0.003,
           "__exit_min": 7, "__exit_cost": 0.0066}
    s_now = {**sup, PS_EA: 0.14304}
    return {
        "Супер Турбо (как сейчас: сдвиг 0,143)": ({**s_now, "__fills": True}, lm),
        "Супер Турбо, сдвиг 0,229 (×0,8)": ({**sup, PS_EA: 0.22886}, lm),
        "Супер Турбо, сдвиг 0,286 (как до 03.10)": (dict(sup), lm),
        "Супер Турбо, издержки «средне»": ({**s_now, **mid}, lm),
        "Супер Турбо без снятых с торгов монет": (dict(s_now), f"alive|{lm}"),
        "Turbo 5.0 (как сейчас)": ({**tur, EA + "base_qty_pct": 0.09375, "__fills": True}, lt),
        "Основной v4 (как сейчас)": ({**v4, "__fills": True}, l4),
    }


def protect_variants():
    """Защиты от минутных проколов на полном периоде: минутный выход по корзине альтов, быстрый стоп по монете, меньшая
    экспозиция и сочетания — для Turbo 5.0 и Супер Турбо; минутный выход — и для основного бота."""
    V = full_variants()
    tur, lt = V["Turbo 5.0 (как сейчас)"]
    sup, ls = V["Супер Турбо (как сейчас: сдвиг 0,143)"]
    v4, l4 = V["Основной v4 (как сейчас)"]
    tur = {k: v for k, v in tur.items() if k != "__fills"}
    sup = {k: v for k, v in sup.items() if k != "__fills"}
    v4 = {k: v for k, v in v4.items() if k != "__fills"}
    cs = coin_stop(0.8, 2)
    out = {}
    for bn, base, lst in (("Turbo 5.0", tur, lt), ("Супер Турбо", sup, ls)):
        out[f"{bn} + быстрый стоп по монете"] = ({**base, **cs}, lst)
        for sig in ("alt|0.04|15", "alt|0.05|30", "alt|0.06|60"):
            x, n = sig.split("|")[1:]
            out[f"{bn} + минутный выход: альты −{float(x) * 100:g}% за {n} мин"] = ({**base, "__signal": sig}, lst)
        out[f"{bn}, экспозиция 6"] = ({**base, RK + "total_wallet_exposure_limit": 6.0}, lst)
        out[f"{bn}, экспозиция 4"] = ({**base, RK + "total_wallet_exposure_limit": 4.0}, lst)
        out[f"{bn} + минутный выход −5%/30 + стоп по монете"] = ({**base, "__signal": "alt|0.05|30", **cs}, lst)
        out[f"{bn} + минутный выход −5%/30 + стоп по монете, экспозиция 6"] = (
            {**base, "__signal": "alt|0.05|30", **cs, RK + "total_wallet_exposure_limit": 6.0}, lst)
    out["Основной v4 + минутный выход: альты −5% за 30 мин"] = ({**v4, "__signal": "alt|0.05|30"}, l4)
    out["Основной v4 + минутный выход: альты −4% за 15 мин"] = ({**v4, "__signal": "alt|0.04|15"}, l4)
    return out


def smart_variants():
    """«Умный» минутный выход: закрыть всё по минутному сигналу корзины альтов, только если загрузка ≥ порога."""
    V = full_variants()
    out = {}
    for bn, key in (("Основной v4", "Основной v4 (как сейчас)"), ("Turbo 5.0", "Turbo 5.0 (как сейчас)"),
                    ("Супер Турбо", "Супер Турбо (как сейчас: сдвиг 0,143)")):
        base, lst = V[key]
        base = {k: v for k, v in base.items() if k != "__fills"}
        for thr in (2.0, 3.0, 4.0):
            out[f"{bn} + умный минутный выход −5%/30 при загрузке ≥ {thr:g}"] = ({**base, "__signal": "alt|0.05|30", "__exit_twe": thr}, lst)
        if bn != "Основной v4":
            out[f"{bn} + умный выход −5%/30 при загрузке ≥ 3 + стоп по монете"] = (
                {**base, "__signal": "alt|0.05|30", "__exit_twe": 3.0, **coin_stop(0.8, 2)}, lst)
    return out


def do_protect_full(a):
    """Защиты на полном периоде 01.2024–09.2026 (TB_FULL=1) → turnover_rule.json["protect_full"], журнал."""
    assert FULL, "запускать с TB_FULL=1"
    V = smart_variants() if a.cmd == "smart_full" else protect_variants()
    names = [n for n in V if not a.only or any(n.startswith(x) for x in a.only.split(","))]
    full = (json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}).get("full", {})
    for bot in ("Основной v4", "Turbo 5.0", "Супер Турбо"):
        grid = {(n, "full", V[n][1]): (V[n][0], month_lists(V[n][1])) for n in names if n.startswith(bot)}
        if not grid:
            continue
        res, months_ = run_grid(grid, a.workers, detail=True)
        report(f"{bot}: защиты от минутных проколов, 01.2024–09.2026", res, months_)
        b = next((full[k] for k in full if k.startswith(bot) and "как сейчас" in k), None)
        if b:
            print(f"  нынешний {bot}: итог {b['total']:+.0f}%, полугодия " + " / ".join(f"{x:+.0f}" for x in b["halves"].values())
                  + f", худшая минута −{b['dd_min']:.0f}%", flush=True)
        allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
        allr.setdefault("protect_full", {}).update({k[0]: {**(r or {}), "list": k[2]} for k, r in res.items()})
        json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def do_full(a):
    """Полный период 01.2024–09.2026 (TB_FULL=1): нынешние боты и проверки Супер Турбо, честно (список по месяцам со
    снятыми с торгов монетами), «реалистично», выход по BTC → turnover_rule.json["full"], журнал turnover_full.log."""
    assert FULL, "запускать с TB_FULL=1"
    V = full_variants()
    names = [n for n in V if not a.only or any(n.startswith(x) for x in a.only.split(","))]
    grid = {(n, "full", V[n][1]): (V[n][0], month_lists(V[n][1])) for n in names}
    res, months_ = run_grid(grid, a.workers, detail=True)
    report("Полный период 01.2024–09.2026 (честно, со снятыми с торгов монетами)", res, months_)
    for k, r in res.items():
        if r and r.get("coin_pnl"):
            print(f"  {k[0][:30]:30s} вклад монет: " + ", ".join(f"{c} {v:+.1f}" for c, v in list(r["coin_pnl"].items())[:10]), flush=True)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr.setdefault("full", {}).update({k[0]: {**(r or {}), "list": k[2]} for k, r in res.items()})
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def do_static(a):
    """Сегодняшние списки по правилам на всех 15 месяцах (как прежние исследования — с заглядыванием вперёд)."""
    import v4_research as VR
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    bots = {b: {**o, **combo} for b, o in VR.bots_over().items()}
    today = json.load(open(OUTF, encoding="utf-8"))["lists"]["today"]
    lists = {"нынешние 84": VR.coins84(), **{f"сегодня: {r}": today[r] for r in ("среднее 7 дн.", "среднее 28 дн.")}}
    jobs = {(f"{b} | {v}", p): (bots[b], L, "day") for b in bots for v, L in lists.items() for p in VR.PERIODS}
    res = VR.run_jobs(jobs, a.workers)
    out = {}
    for b in bots:
        out[b] = VR.table(f"{b}: неподвижный список, сегодняшний по правилу", res, [f"{b} | {v}" for v in lists], VR.PERIODS)
    allr = json.load(open(OUTF, encoding="utf-8")) if os.path.exists(OUTF) else {}
    allr["static"] = out
    json.dump(allr, open(OUTF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["data", "data_hl", "lists", "test", "static", "protect", "turbo", "author", "risk", "super", "superopt", "lev3", "sweep", "psize", "tcombo", "full", "data_both", "protect_full", "smart_full"])
    ap.add_argument("--bots", default="turbo,super,v4", help="sweep: боты через запятую (v4, turbo, super)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--stage", choices=["1", "2", "12", "123"], default="1")
    ap.add_argument("--stop", help="вариант стопа из этапа 1 для этапов 12/123 (полное имя)")
    ap.add_argument("--combo", help="superopt: JSON-файл {имя: [правки, порог фильтра]} вместо SUPEROPT")
    ap.add_argument("--only", help="turbo: варианты по началу имени через запятую (сейчас,А,Б…)")
    a = ap.parse_args()
    {"data": do_data, "data_hl": do_data_hl, "lists": do_lists, "test": do_test, "static": do_static,
     "protect": do_protect, "turbo": do_turbo, "author": do_author, "risk": do_risk, "super": do_super, "superopt": do_superopt, "lev3": do_lev3, "sweep": do_sweep, "psize": do_psize, "tcombo": do_tcombo, "full": do_full, "data_both": do_data_both, "protect_full": do_protect_full, "smart_full": do_protect_full}[a.cmd](a)
