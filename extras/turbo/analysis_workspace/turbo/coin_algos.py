"""Турбо 3.0, вторая половина 2026: алгоритмы выбора монет перед ставкой — по цене монеты на момент старта.

Каждое правило видит только часовые цены до старта ставки S и задаёт список разрешённых монет на 30 дней;
внутри списка монеты по-прежнему выбирает passivbot (3 позиции, экспозиция 20). Ставки — старт с 01.07 по 30.08
каждые 2 дня, 30 дней или до правила панели 3.0 (цель ×2, стоп ставки ×0,7, выход при обвале BTC).
Правило считается находкой, только если оно лучше 3.0 на всех монетах и на июльских, и на августовских стартах.
Монеты, которые не проходят минимальный ордер KuCoin при ставке $200, из списков исключаются заранее.

    venv\\Scripts\\python turbo\\coin_algos.py [--workers 6]
Итог: turbo/results/coin_algos.json и таблица в консоли; цены — turbo/results/coin_prices_1h.json.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import drawdown_study as DS  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

MC, ROOT = DS.MC, DS.ROOT
RES = os.path.join(HERE, "results")
PRICES = os.path.join(RES, "coin_prices_1h.json")
CURVES = os.path.join(RES, "coin_algos")
BET_STARTS = CP.grid("2026-07-01", "2026-08-30", step=2)
STAKE = 200.0
F = "bot.long.forager."


def prices():
    """Часовые закрытия монет (Binance USDⓈ-M, публичные данные) с 01.03.2026; кэш в coin_prices_1h.json."""
    if os.path.exists(PRICES):
        d = json.load(open(PRICES))
    else:
        s, d = RR.http(), {}
        t_end = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
        for c in MC.default_coins():
            t, rows_all = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000), {}
            while t < t_end:
                rows = s.get("https://fapi.binance.com/fapi/v1/klines",
                             params={"symbol": f"{c}USDT", "interval": "1h", "startTime": t, "limit": 1500}, timeout=20).json()
                if not isinstance(rows, list) or not rows:
                    break
                for r in rows:
                    rows_all[str(r[0])] = float(r[4])
                t = rows[-1][0] + 3_600_000
            d[c] = rows_all
            print(f"  {c}: {len(rows_all)} ч", flush=True)
        json.dump(d, open(PRICES, "w"))
    out = {}
    for c, rows in d.items():
        if rows:
            ser = pd.Series({int(k): v for k, v in rows.items()}).sort_index()
            ser.index = pd.to_datetime(ser.index, unit="ms")
            out[c] = ser
    return pd.DataFrame(out)


def tradable_coins():
    """Проходят минимальный ордер KuCoin при ставке $200 (как фильтр passivbot и панель)."""
    mc = json.load(open(os.path.join(ROOT, "tb4", "kucoin_min_cost.json"), encoding="utf-8"))
    risk = json.load(open(MC.BASE, encoding="utf-8"))["bot"]["long"]
    wel = 20.0 / 3.0
    first = STAKE * wel * (1 + min(risk["risk"]["we_excess_allowance_pct"], 20.0 / wel - 1)) * \
        risk["strategy"]["trailing_martingale"]["entry"]["initial_qty_pct"]
    return [c for c in MC.default_coins() if mc.get(c, 1e9) <= first]


def features(px, start):
    """Признаки монет по часовым ценам до старта ставки."""
    h = px[px.index < pd.Timestamp(start)]
    d = h.resample("1D").last()
    last = h.iloc[-1]
    f = pd.DataFrame({
        "ret7": last / h.iloc[-24 * 7 - 1] - 1, "ret14": last / h.iloc[-24 * 14 - 1] - 1,
        "ret30": last / h.iloc[-24 * 30 - 1] - 1,
        "up20": last > d.iloc[-20:].mean(), "up50": last > d.iloc[-50:].mean(),
        "dd14": 1 - last / h.iloc[-24 * 14:].max(), "dd30": 1 - last / h.iloc[-24 * 30:].max(),
        "dd60": 1 - last / h.iloc[-24 * 60:].max(),
        "vol14": np.log(h.iloc[-24 * 14:]).diff().std(),
    })
    return f


def algos(px, btc, tradable):
    """{название: функция(старт) → (список монет или None = все, правки движка)}."""
    def feat(s):
        return features(px, s).loc[[c for c in tradable if c in px.columns]].dropna()

    def btc_ret14(s):
        b = btc[btc.index < pd.Timestamp(s)]
        return b.iloc[-1] / b.iloc[-24 * 14 - 1] - 1

    def top(col, k, asc=False):
        return lambda s: (list(feat(s).sort_values(col, ascending=asc).index[:k]), {})

    return {
        "все монеты (3.0 сейчас)": lambda s: (None, {}),
        "тренд: выше средней 20 дн.": lambda s: (list((f := feat(s)).index[f.up20]), {}),
        "тренд: выше средней 50 дн.": lambda s: (list((f := feat(s)).index[f.up50]), {}),
        "импульс: 6 лучших за 7 дн.": top("ret7", 6),
        "импульс: 6 лучших за 14 дн.": top("ret14", 6),
        "импульс: 6 лучших за 30 дн.": top("ret30", 6),
        "импульс: 3 лучшие за 14 дн.": top("ret14", 3),
        "без худшей половины за 14 дн.": lambda s: (list((f := feat(s)).sort_values("ret14", ascending=False).index[:len(f) // 2 + 1]), {}),
        "сильнее BTC за 14 дн.": lambda s: (list((f := feat(s)).index[f.ret14 > btc_ret14(s)]), {}),
        "просадка от максимума месяца < 15%": lambda s: (list((f := feat(s)).index[f.dd30 < 0.15]), {}),
        "без самой волатильной половины": lambda s: (list((f := feat(s)).sort_values("vol14").index[:len(f) // 2 + 1]), {}),
        "только самая волатильная половина": lambda s: (list((f := feat(s)).sort_values("vol14", ascending=False).index[:len(f) // 2 + 1]), {}),
        "тренд 20 дн. + 6 лучших за 14 дн.": lambda s: (list((f := feat(s))[f.up20].sort_values("ret14", ascending=False).index[:6]), {}),
        "passivbot выбирает по объёму": lambda s: (None, {F + "score_weights": {"ema_readiness": 0.0, "volatility": 0.0, "volume": 1.0}}),
        "passivbot: объём и волатильность поровну": lambda s: (None, {F + "score_weights": {"ema_readiness": 0.2, "volatility": 0.4, "volume": 0.4}}),
        # соседние пороги и окна для «просадка от максимума < X%» — область, а не одна точка
        **{f"просадка от максимума {w} дн. < {thr * 100:g}%": (lambda w, thr: lambda s: (
            list((f := feat(s)).index[f[f"dd{w}"] < thr]), {}))(w, thr)
           for w in (14, 30, 60) for thr in (0.10, 0.15, 0.20, 0.25) if (w, thr) != (30, 0.15)},
    }


def bet_key(i, coins):
    """Папка кэша: номер правила + хэш списка монет (списки бывают длинными)."""
    return f"a{i:02d}_" + ("all" if coins is None else hashlib.md5(",".join(coins).encode()).hexdigest()[:10])


def run_bet(key, start, coins, over):
    path = os.path.join(CURVES, key, f"{start}.npy")
    if not os.path.exists(path):
        res = DS.backtest_curve({**PW.ENGINE3, **over}, start, CP.end_of(start), f"ca_{key}_{start}", coins=coins)
        if isinstance(res, str):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.save(path, res[0])
        shutil.rmtree(res[1], ignore_errors=True)
    return np.load(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    px, btc = prices(), RR.btc_hourly()
    tradable = tradable_coins()
    print(f"цены: {px.shape[1]} монет, {px.index[0]} … {px.index[-1]}; проходят минимальный ордер: {len(tradable)}", flush=True)
    al = algos(px, btc, tradable)
    jobs = []
    for i, (name, fn) in enumerate(al.items()):
        for s in BET_STARTS:
            coins, over = fn(s)
            jobs.append((i, name, s, sorted(coins) if coins is not None else None, over))
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        rels = list(ex.map(lambda j: run_bet(bet_key(j[0], j[3]), j[2], j[3], j[4]) if j[3] != [] else None, jobs))
    print(f"ставки: {len(jobs)} прогонов, {time.time() - t0:.0f} с", flush=True)
    res = {name: [] for name in al}
    for (i, name, s, coins, over), rel in zip(jobs, rels):
        if coins == []:
            res[name].append({"start": s, "coins": [], "result_pct": 0.0, "kind": "нет монет — ставки нет"})
        elif rel is not None:
            r, kind = CP.bet(rel, s, btc)
            res[name].append({"start": s, "coins": coins, "result_pct": round(r * 100, 1), "kind": kind})

    def summ(bets):
        return CP.summary([(b["result_pct"] / 100, b["kind"]) for b in bets])

    out = {}
    for name, bets in res.items():
        jul = [b for b in bets if b["start"] < "2026-08-01"]
        aug = [b for b in bets if b["start"] >= "2026-08-01"]
        out[name] = {"all": summ(bets), "jul": summ(jul), "aug": summ(aug), "bets": bets,
                     "avg_coins": round(float(np.mean([len(b["coins"]) if b["coins"] is not None else len(tradable) for b in bets])), 1)}
    json.dump(out, open(os.path.join(RES, "coin_algos.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    base = out["все монеты (3.0 сейчас)"]
    print(f"\n{len(BET_STARTS)} ставок {BET_STARTS[0]} … {BET_STARTS[-1]} (каждые 2 дня), правила 3.0; пары — июльские / августовские старты")
    for name, v in sorted(out.items(), key=lambda kv: -kv[1]["all"]["mean"]):
        a_, j, g = v["all"], v["jul"], v["aug"]
        better = j["mean"] > base["jul"]["mean"] and g["mean"] > base["aug"]["mean"]
        print(f"  {'★' if better and name != 'все монеты (3.0 сейчас)' else ' '} {name:<40} монет ~{v['avg_coins']:4.1f} | "
              f"ср. итог {a_['mean']:+6.1f}% ({j['mean']:+6.1f} / {g['mean']:+6.1f}) | ×2 {a_['p_target']:5.1f}% "
              f"({j['p_target']:4.1f} / {g['p_target']:4.1f}) | в минусе {a_['p_loss']:5.1f}% | ср. убыток {a_['avg_loss']:+6.1f}% | "
              f"худшая {a_['worst']:+6.1f}%")
    print("  ★ — лучше «все монеты» и на июльских, и на августовских стартах")


if __name__ == "__main__":
    main()
