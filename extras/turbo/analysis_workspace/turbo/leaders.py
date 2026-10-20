"""«Ставка на лидеров» — экспериментальный Turbo, ТОЛЬКО бумага (пользователь, 01.10.2026).

Правила (turbo/lottery_scan.py, turbo/results/lottery_check.json): раз в 12 ч (закрытие свечей 00:00 и 12:00 UTC) из
монет Turbo выбираются n с наибольшим ростом за 72 ч; объём — exposure × капитал поровну на них (только лонг); если
BTC ниже своей средней за 168 ч — без позиций. Плечо на бирже не больше 10× (у этих правил объём 3×).
На истории 2026 года: типичный месяц +12…+22%, ×2 в 14–19% месяцев, но результат сильно зависит от набора монет
(на 21 «старой» монете −56% за январь–сентябрь) — поэтому только бумага.

Бумага — вперёд, не задним числом: каждый час берутся закрытые часовые свечи фьючерсов KuCoin, позиции оцениваются по
закрытиям; учёт как у фьючерсов: количество в контрактах KuCoin (вниз до целого контракта), средняя цена входа,
реализованная и нереализованная прибыль, комиссия cost с оборота, финансирование funding_8h за 8 ч с объёма позиций.
Если панель была выключена, пропущенные часы досчитываются по свечам при следующем запуске.
"""
import json
import math
import os
import time

DEFAULTS = {"n": 3, "lookback_h": 72, "rebalance_h": 12, "exposure": 3.0, "btc_ma_h": 168,
            "cost": 0.0008, "funding_8h": 0.0001}
H = 3_600_000
HISTORY_H = 200                       # часов истории для решения (средняя BTC за 168 ч + запас)


# ------------------------------------------------------------------ данные
def kucoin_closes(symbols, t_from_ms, t_to_ms, session=None):
    """Закрытые часовые свечи фьючерсов KuCoin: {монета: {время закрытия свечи, мс: цена закрытия}}."""
    import requests
    s = session or requests.Session()
    s.trust_env = False
    out = {}
    for coin, sym in symbols.items():
        rows, t = {}, t_from_ms
        while t < t_to_ms:
            stop = min(t_to_ms, t + 190 * H)
            for attempt in range(3):
                try:
                    r = s.get("https://api-futures.kucoin.com/api/v1/kline/query",
                              params={"symbol": sym, "granularity": 60, "from": t, "to": stop}, timeout=15).json()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(1)
            for x in r.get("data") or []:
                close_ms = int(x[0]) + H
                if close_ms <= t_to_ms:
                    rows[close_ms] = float(x[4])
            t = stop
        out[coin] = rows
    return out


# ------------------------------------------------------------------ решение
def decide(closes, t, p, btc="BTC"):
    """Решение на закрытии часа t (мс) только по закрытиям ≤ t: (BTC выше средней?, BTC, средняя, рейтинг, целевые доли)."""
    b = closes.get(btc) or {}
    hist = [x for k in range(p["btc_ma_h"]) if (x := b.get(t - k * H)) is not None]
    ok, price, ma = False, b.get(t), None
    if price and len(hist) >= 0.9 * p["btc_ma_h"]:              # пропуски свечей KuCoin — до 10%
        ma = sum(hist) / len(hist)
        ok = price > ma
    ranking = []
    for coin, cl in closes.items():
        now, past = cl.get(t), cl.get(t - p["lookback_h"] * H)
        if now and past:
            ranking.append((coin, now / past - 1))
    ranking.sort(key=lambda x: -x[1])
    targets = {c: 1.0 / p["n"] for c, _ in ranking[:p["n"]]} if ok and len(ranking) >= p["n"] else {}
    return {"t": t, "btc_ok": ok, "btc": price, "btc_ma": round(ma, 2) if ma else None,
            "ranking": [[c, round(r * 100, 2)] for c, r in ranking[:8]], "targets": targets}


# ------------------------------------------------------------------ учёт
def new_state(balance, start_ms, params=None):
    t0 = start_ms - start_ms % H                      # с последнего закрытого часа
    return {"params": dict(DEFAULTS, **(params or {})), "start_ms": t0, "start_balance": float(balance),
            "balance": float(balance), "positions": {}, "last_ms": t0, "last_prices": {}, "equity": [[t0, float(balance)]],
            "trades": [], "decisions": [], "fees": 0.0, "funding": 0.0}


def equity_of(st, prices):
    return st["balance"] + sum(pos["qty"] * (prices.get(c, st["last_prices"].get(c, pos["entry"])) - pos["entry"])
                               for c, pos in st["positions"].items())


def rebalance(st, targets, prices, t, mult):
    """Довести позиции до целевых долей (по закрытиям часа t); количество — вниз до целого контракта."""
    p = st["params"]
    eq = equity_of(st, prices)
    want = {}
    for c, w in targets.items():
        px = prices.get(c)
        if not px:
            continue
        m = mult.get(c, 1e-9)
        want[c] = math.floor(w * p["exposure"] * max(eq, 0) / px / m) * m
    for c in sorted(set(st["positions"]) | set(want)):
        px = prices.get(c) or st["last_prices"].get(c)
        cur = st["positions"].get(c, {"qty": 0.0, "entry": px})
        new = want.get(c, 0.0)
        d = new - cur["qty"]
        if abs(d) < 1e-12 or not px:
            continue
        if d < 0:                                       # уменьшить / закрыть: прибыль фиксируется
            st["balance"] += -d * (px - cur["entry"])
            entry = cur["entry"]
        else:                                           # добавить: средняя цена входа
            entry = (cur["qty"] * cur["entry"] + d * px) / new if new else px
        fee = abs(d) * px * p["cost"]
        st["balance"] -= fee
        st["fees"] += fee
        if new > 1e-12:
            st["positions"][c] = {"qty": new, "entry": entry}
        else:
            st["positions"].pop(c, None)
        st["trades"].append({"t": t, "coin": c, "side": "buy" if d > 0 else "sell", "qty": round(abs(d), 8),
                             "price": px, "fee": round(fee, 4)})
    st["trades"] = st["trades"][-300:]


def advance(st, closes, now_ms, mult, btc="BTC"):
    """Обработать все закрытые часы после st["last_ms"] до now_ms: оценка, финансирование, перестановка в 00:00/12:00 UTC."""
    p = st["params"]
    t = st["last_ms"] + H
    while t <= now_ms:
        prices = {c: cl[t] for c, cl in closes.items() if t in cl}
        if not prices:                                  # нет свечей этого часа (ещё не пришли) — подождать
            break
        notional = sum(abs(pos["qty"]) * prices.get(c, st["last_prices"].get(c, pos["entry"]))
                       for c, pos in st["positions"].items())
        fund = notional * p["funding_8h"] / 8
        st["balance"] -= fund
        st["funding"] += fund
        st["last_prices"].update(prices)
        if (t // H) % p["rebalance_h"] == 0:
            dec = decide(closes, t, p, btc)
            rebalance(st, dec["targets"], prices, t, mult)
            st["decisions"] = (st["decisions"] + [dec])[-60:]
        st["equity"].append([t, round(equity_of(st, prices), 4)])
        st["last_ms"] = t
        t += H
    return st


def tick(st, symbols, mult, now_ms, fetch=kucoin_closes):
    """Догнать бумагу до последнего закрытого часа (now_ms — текущее время)."""
    last_closed = now_ms - now_ms % H
    if last_closed <= st["last_ms"]:
        return st, False
    t_from = st["last_ms"] - (HISTORY_H + 2) * H
    closes = fetch(symbols, t_from, last_closed)
    advance(st, closes, last_closed, mult)
    return st, True


def summary(st, max_points=400):
    """Сводка для панели и отчёта."""
    eq = st["equity"]
    last = eq[-1][1] if eq else st["start_balance"]
    days = max(1e-9, (st["last_ms"] - st["start_ms"]) / 86_400_000)
    peak, dd = 0.0, 0.0
    for _, v in eq:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak if peak > 0 else 0)
    step = max(1, len(eq) // max_points)
    curve = eq[::step] + ([eq[-1]] if eq and (len(eq) - 1) % step else [])
    pos = [{"coin": c, "qty": x["qty"], "entry": x["entry"], "price": st["last_prices"].get(c),
            "pnl": round(x["qty"] * (st["last_prices"].get(c, x["entry"]) - x["entry"]), 2),
            "notional": round(x["qty"] * st["last_prices"].get(c, x["entry"]), 2)} for c, x in st["positions"].items()]
    dec = st["decisions"][-1] if st["decisions"] else None
    p = st["params"]
    next_reb = (st["last_ms"] // (p["rebalance_h"] * H) + 1) * p["rebalance_h"] * H
    return {"start_ms": st["start_ms"], "start_balance": st["start_balance"], "equity": round(last, 2),
            "return_pct": round((last / st["start_balance"] - 1) * 100, 2), "days": round(days, 2),
            "max_dd_pct": round(dd * 100, 1), "positions": pos, "decision": dec, "last_ms": st["last_ms"],
            "next_rebalance_ms": next_reb, "trades": st["trades"][-20:], "fees": round(st["fees"], 2),
            "funding": round(st["funding"], 2), "params": p, "curve": curve}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)
