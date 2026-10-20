"""Поиск стратегий вне passivbot: опубликованные идеи на рынке 2025–2026, фьючерсы KuCoin, без плеча.

Подбор параметров — на 2025 году, проверка — на 2026 (01.01–29.09), который подбор не видел.
Стратегии (часовые свечи Binance USDⓈ-M, сигнал по закрытию часа, сделка со следующего часа):
  season  — держать монету только в часы [h, h+d) UTC (Quantpedia / Vojtko & Javorská: BTC 21–23 UTC);
  tsmom   — лонг (или лонг/шорт), пока EMA быстрая выше медленной (time-series momentum);
  donch   — прорыв канала Дончиана по дневным закрытиям: вход на максимуме N дней, выход на минимуме M дней;
  fund    — BTC: лонг, кроме дней, когда средняя ставка финансирования за 3 дня выше p-го процентиля прошлого года.
Расходы — на каждое изменение позиции: taker KuCoin 0,06% + проскальзывание 0,02% (или maker 0,02%) за сторону.

    venv\\Scripts\\python tb4\\strategy_scan.py
Итог: tb4/results/strategy_scan.json и таблицы в консоли; цены — tb4/results/scan_prices_1h.json.
"""
import itertools
import json
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
PRICES = os.path.join(RES, "scan_prices_1h.json")
FUNDING = os.path.join(os.path.dirname(HERE), "..", "tradebot turbo", "turbo", "results", "btc_funding.json")
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LINK"]
IS = ("2025-01-01", "2026-01-01")          # подбор
OOS = ("2026-01-01", "2026-09-30")         # проверка
COST = {"taker": 0.0008, "maker": 0.0002}  # за сторону: изменение позиции на 1 = 1 × cost


def prices():
    if os.path.exists(PRICES):
        d = json.load(open(PRICES))
    else:
        s = requests.Session()
        s.trust_env = False
        d = {}
        t_end = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
        for c in COINS:
            t, rows_all = int(datetime(2024, 7, 1, tzinfo=timezone.utc).timestamp() * 1000), {}
            while t < t_end:
                rows = s.get("https://fapi.binance.com/fapi/v1/klines", params={"symbol": f"{c}USDT", "interval": "1h",
                                                                                 "startTime": t, "endTime": t_end, "limit": 1500},
                             timeout=20).json()
                if not isinstance(rows, list) or not rows:
                    break
                for r in rows:
                    rows_all[str(r[0])] = float(r[4])
                t = rows[-1][0] + 3_600_000
                time.sleep(0.1)
            d[c] = rows_all
            print(f"  {c}: {len(rows_all)} ч", flush=True)
        os.makedirs(RES, exist_ok=True)
        json.dump(d, open(PRICES, "w"))
    df = pd.DataFrame({c: pd.Series({int(k): v for k, v in rows.items()}) for c, rows in d.items()}).sort_index()
    df.index = pd.to_datetime(df.index, unit="ms")
    return df[df.index < pd.Timestamp(OOS[1])]


def funding():
    d = json.load(open(FUNDING))
    s = pd.Series({int(k): v for k, v in d.items()}).sort_index()
    s.index = pd.to_datetime(s.index, unit="ms")
    return s


def backtest(px, pos, cost):
    """px — закрытия часа, pos — желаемая позиция по закрытию часа (−1…1). Доход часа t = pos(t−1) × ret(t) − расходы."""
    ret = px.pct_change().fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    turn = held.diff().abs().fillna(held.abs())
    return held * ret - turn * cost


def stats(r, period):
    r = r[(r.index >= pd.Timestamp(period[0])) & (r.index < pd.Timestamp(period[1]))]
    if r.empty:
        return None
    eq = (1 + r).cumprod()
    years = len(r) / (24 * 365)
    total = eq.iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    dd = float((1 - eq / eq.cummax()).max())
    sharpe = float(r.mean() / r.std() * math.sqrt(24 * 365)) if r.std() > 0 else 0.0
    return {"total": round(total * 100, 1), "ann": round(ann * 100, 1), "dd": round(dd * 100, 1), "sharpe": round(sharpe, 2)}


def portfolio(rets):
    """Равные доли по монетам (перебалансировка каждый час — приближение)."""
    return pd.concat(rets, axis=1).mean(axis=1)


# ------------------------------------------------------------------ сигналы
def sig_season(px, h, d):
    """Держать свечи с открытием в часы h … h+d−1 UTC: решение — по закрытию предыдущей свечи (её час h−1+k)."""
    hours = px.index.hour
    want = np.zeros(len(px))
    for k in range(d):
        want += (hours == (h - 1 + k) % 24)
    return pd.Series(np.clip(want, 0, 1), index=px.index)


def sig_tsmom(px, fast, slow, short):
    f, s = px.ewm(span=fast, adjust=False).mean(), px.ewm(span=slow, adjust=False).mean()
    up = (f > s).astype(float)
    return up - (1 - up) if short else up


def sig_donch(px, n, m, short):
    daily = px.resample("1D").last()
    hi, lo = daily.rolling(n).max().shift(1), daily.rolling(m).min().shift(1)
    pos, cur = [], 0.0
    for c, h, l_ in zip(daily, hi, lo):
        if np.isnan(h) or np.isnan(l_):
            pos.append(0.0)
            continue
        if c > h:
            cur = 1.0
        elif cur > 0 and c < l_:
            cur = 0.0
        pos.append(cur)
    p = pd.Series(pos, index=daily.index)
    if short:                                       # шорт на минимуме N дней, выход на максимуме M дней
        lo_n, hi_m = daily.rolling(n).min().shift(1), daily.rolling(m).max().shift(1)
        sp, cur = [], 0.0
        for c, l_, h in zip(daily, lo_n, hi_m):
            if np.isnan(l_) or np.isnan(h):
                sp.append(0.0)
                continue
            if c < l_:
                cur = -1.0
            elif cur < 0 and c > h:
                cur = 0.0
            sp.append(cur)
        p = p + pd.Series(sp, index=daily.index)
        p = p.clip(-1, 1)
    p.index = p.index + pd.Timedelta(days=1) - pd.Timedelta(hours=1)   # решение по закрытию дня — с последнего часа дня
    return p.reindex(px.index, method="ffill").fillna(0.0)


def sig_fund(px, fund, pct):
    f3 = fund.rolling(9).mean()                                        # 9 выплат = 3 дня
    thr = f3.rolling(3 * 365, min_periods=200).quantile(pct)           # порог по прошлому году (без заглядывания)
    hot = (f3 > thr).astype(float)
    hot.index = hot.index + pd.Timedelta(hours=1)
    return (1 - hot.reindex(px.index, method="ffill").fillna(0.0))


def xsec_returns(px, look_d, top, ls, cost, hold_d=7):
    """Ротация по силе: раз в hold_d дней лонг top лучших монет по доходу за look_d дней (и шорт top худших при ls)."""
    daily = px.resample("1D").last()
    ret = daily.pct_change(look_d)
    w = pd.DataFrame(0.0, index=daily.index, columns=px.columns)
    for i, t in enumerate(daily.index):
        if i % hold_d or ret.loc[t].isna().any():
            w.iloc[i] = w.iloc[i - 1] if i else 0.0
            continue
        r = ret.loc[t].sort_values(ascending=False)
        row = pd.Series(0.0, index=px.columns)
        row[r.index[:top]] = 1.0 / top / (2 if ls else 1)
        if ls:
            row[r.index[-top:]] = -1.0 / top / 2
        w.iloc[i] = row
    w.index = w.index + pd.Timedelta(days=1) - pd.Timedelta(hours=1)
    held = w.reindex(px.index, method="ffill").fillna(0.0).shift(1).fillna(0.0)
    return (held * px.pct_change().fillna(0.0)).sum(axis=1) - held.diff().abs().sum(axis=1).fillna(0.0) * cost


def sig_rsi2(px, lo, hi, max_days):
    """Возврат к среднему: лонг, когда дневной RSI(2) < lo; выход при RSI(2) > hi или через max_days дней."""
    d = px.resample("1D").last()
    delta = d.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn)
    pos, cur, age = [], 0.0, 0
    for v in rsi:
        if cur == 0 and v < lo:
            cur, age = 1.0, 0
        elif cur == 1:
            age += 1
            if v > hi or age >= max_days:
                cur = 0.0
        pos.append(cur)
    p = pd.Series(pos, index=d.index)
    p.index = p.index + pd.Timedelta(days=1) - pd.Timedelta(hours=1)
    return p.reindex(px.index, method="ffill").fillna(0.0)


def main():
    px = prices()
    fund = funding()
    print(f"цены: {px.shape[1]} монет, {px.index[0]} … {px.index[-1]}")
    rows = []

    def add(family, name, params, r_by_cost, universe):
        for cost, r in r_by_cost.items():
            rows.append({"family": family, "name": name, "params": params, "cost": cost, "universe": universe,
                         "is": stats(r, IS), "oos": stats(r, OOS)})

    # эталон: купил и держи
    for c in COINS:
        r = backtest(px[c], pd.Series(1.0, index=px.index), 0.0)
        add("hold", f"держать {c}", {}, {"none": r}, c)
    add("hold", "держать 8 монет поровну", {}, {"none": portfolio([backtest(px[c], pd.Series(1.0, index=px.index), 0.0)
                                                                  for c in COINS])}, "8")
    # сезонность: BTC и 8 монет, все окна
    for h, d in itertools.product(range(24), (1, 2, 3, 4)):
        for uni in ("BTC", "8"):
            cs = ["BTC"] if uni == "BTC" else COINS
            rb = {k: portfolio([backtest(px[c], sig_season(px[c], h, d), v) for c in cs]) for k, v in COST.items()}
            add("season", f"держать {h:02d}:00–{(h + d) % 24:02d}:00 UTC", {"h": h, "d": d}, rb, uni)
    # следование тренду
    for (fast, slow), short in itertools.product([(12, 48), (24, 96), (48, 192), (96, 384), (168, 672), (24, 240), (72, 720)],
                                                 (False, True)):
        for uni in ("BTC", "8"):
            cs = ["BTC"] if uni == "BTC" else COINS
            rb = {k: portfolio([backtest(px[c], sig_tsmom(px[c], fast, slow, short), v) for c in cs]) for k, v in COST.items()}
            add("tsmom", f"EMA {fast}/{slow} ч, {'лонг/шорт' if short else 'лонг/вне рынка'}",
                {"fast": fast, "slow": slow, "short": short}, rb, uni)
    # прорыв Дончиана
    for (n, m), short in itertools.product([(10, 5), (20, 10), (30, 15), (55, 20)], (False, True)):
        for uni in ("BTC", "8"):
            cs = ["BTC"] if uni == "BTC" else COINS
            rb = {k: portfolio([backtest(px[c], sig_donch(px[c], n, m, short), v) for c in cs]) for k, v in COST.items()}
            add("donch", f"Дончиан {n}/{m} дн., {'лонг/шорт' if short else 'лонг'}", {"n": n, "m": m, "short": short}, rb, uni)
    # ставка финансирования (BTC)
    for pct in (0.7, 0.8, 0.9, 0.95):
        rb = {k: backtest(px["BTC"], sig_fund(px["BTC"], fund, pct), v) for k, v in COST.items()}
        add("fund", f"BTC: вне рынка, когда финансирование выше {int(pct * 100)}-го процентиля", {"pct": pct}, rb, "BTC")

    # ротация по силе (8 монет) и возврат к среднему RSI(2)
    for look, top, ls in itertools.product((7, 14, 30), (1, 2, 3), (False, True)):
        rb = {k: xsec_returns(px, look, top, ls, v) for k, v in COST.items()}
        add("xsec", f"ротация: {top} лучш. за {look} дн.{', против худших' if ls else ''}", {"look": look, "top": top, "ls": ls}, rb, "8")
    for lo, hi, md in itertools.product((5, 10, 20), (60, 70, 80), (3, 5)):
        rb = {k: portfolio([backtest(px[c], sig_rsi2(px[c], lo, hi, md), v) for c in COINS]) for k, v in COST.items()}
        add("rsi2", f"RSI(2) < {lo}, выход > {hi} или {md} дн.", {"lo": lo, "hi": hi, "max_days": md}, rb, "8")

    json.dump(rows, open(os.path.join(RES, "strategy_scan.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def line(r):
        a, b = r["is"], r["oos"]
        return (f"{r['family']:<6} {r['universe']:<4} {r['cost']:<6} {r['name']:<52} | 2025: {a['ann']:+7.1f}% год., просадка {a['dd']:5.1f}%, "
                f"Шарп {a['sharpe']:+5.2f} | 2026: {b['total']:+7.1f}% за 9 мес., просадка {b['dd']:5.1f}%, Шарп {b['sharpe']:+5.2f}")

    print("\n=== Эталон: купил и держи")
    for r in rows:
        if r["family"] == "hold":
            print("  " + line(r))
    print("\n=== Опубликованное правило: BTC 21:00–23:00 UTC (подбора не было — 2025 и 2026 оба проверка)")
    for r in rows:
        if r["family"] == "season" and r["params"] == {"h": 21, "d": 2}:
            print("  " + line(r))
    for fam, title in (("season", "Сезонность"), ("tsmom", "Тренд EMA"), ("donch", "Дончиан"), ("fund", "Финансирование"),
                       ("xsec", "Ротация по силе"), ("rsi2", "RSI(2)")):
        cand = [r for r in rows if r["family"] == fam and r["cost"] == "taker"]
        best = sorted(cand, key=lambda r: -r["is"]["sharpe"])[:5]
        both = sum(r["is"]["ann"] > 0 and r["oos"]["total"] > 0 for r in cand)
        print(f"\n=== {title}: 5 лучших по Шарпу 2025 (расходы taker) → как прошёл 2026; в плюсе в оба года {both} из {len(cand)}")
        for r in best:
            print("  " + line(r))


if __name__ == "__main__":
    main()
