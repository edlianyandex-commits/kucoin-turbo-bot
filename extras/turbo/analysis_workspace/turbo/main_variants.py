"""Основной бот (v4, EMA Anchor на 84 монетах, экспозиция 8, HSL 35%, выход при обвале BTC с запуском со следующего дня):
1) сколько съедает плата за финансирование (funding) — бэктест passivbot её не считает;
2) варианты: число позиций, веса отбора монет (forager), unstuck — по трём периодам (II пол. 2025 с обвалом 10.10,
   I пол. 2026, II пол. 2026). Счёт $300, как у v4.

Funding: история ставок Binance (fapi/v1/fundingRate) по каждой монете, кэш turbo/results/funding_binance.json.
Позиция между сделками = psize × pprice из fills.csv (приближённо: цена входа вместо текущей); в момент выплаты
длинная позиция платит notional × ставку (при отрицательной ставке — получает).

    venv\\Scripts\\python turbo\\main_variants.py [--workers 6] [--only base,n15]
Итог: turbo/results/main_variants.json и таблица.
"""
import argparse
import glob
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

RES = os.path.join(HERE, "results")
FUND = os.path.join(RES, "funding_binance.json")
FILLS = os.path.join(RES, "main_variants_fills")       # сделки цепочек (для пересчёта funding по ставкам KuCoin)
L = "bot.long."
W = L + "forager.score_weights"
VARIANTS = {
    "base": ("как сейчас: 10 позиций", {}),
    "n15": ("15 позиций", {L + "risk.n_positions": 15.0}),
    "n20": ("20 позиций", {L + "risk.n_positions": 20.0}),
    "n7": ("7 позиций", {L + "risk.n_positions": 7.0}),
    "n8": ("8 позиций", {L + "risk.n_positions": 8.0}),
    "tw9": ("экспозиция 9 (10 позиций)", {L + "risk.total_wallet_exposure_limit": 9.0}),
    "n5": ("5 позиций", {L + "risk.n_positions": 5.0}),
    "n5tw9": ("5 позиций, экспозиция 9", {L + "risk.n_positions": 5.0, L + "risk.total_wallet_exposure_limit": 9.0}),
    "n3tw9": ("3 позиции, экспозиция 9", {L + "risk.n_positions": 3.0, L + "risk.total_wallet_exposure_limit": 9.0}),
    "n7tw9": ("7 позиций, экспозиция 9", {L + "risk.n_positions": 7.0, L + "risk.total_wallet_exposure_limit": 9.0}),
    "w_vola": ("отбор только по волатильности", {W: {"ema_readiness": 0.0, "volatility": 1.0, "volume": 0.0}}),
    "w_volume": ("отбор только по объёму", {W: {"ema_readiness": 0.0, "volatility": 0.0, "volume": 1.0}}),
    "w_ema": ("отбор только по готовности EMA", {W: {"ema_readiness": 1.0, "volatility": 0.0, "volume": 0.0}}),
    "unstuck": ("unstuck включён (параметры автора)", {L + "unstuck.enabled": True}),
}
STAKE = 300.0
OFFSET = 0                                       # сдвиг старта периода, дней (--offset)


def funding(coins):
    data = json.load(open(FUND)) if os.path.exists(FUND) else {}
    s = RR.http()
    t0 = int(pd.Timestamp("2025-06-25", tz="UTC").timestamp() * 1000)
    t_end = int(pd.Timestamp("2026-10-01", tz="UTC").timestamp() * 1000)
    for c in coins:
        if c in data:
            continue
        rows, t = [], t0
        while t < t_end:
            for attempt in range(5):
                try:
                    r = s.get("https://fapi.binance.com/fapi/v1/fundingRate",
                              params={"symbol": c + "USDT", "startTime": t, "endTime": t_end, "limit": 1000}, timeout=20)
                    batch = r.json()
                    break
                except Exception:
                    time.sleep(2 * (attempt + 1))
            else:
                batch = []
            if not isinstance(batch, list) or not batch:
                break
            rows += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in batch]
            t = rows[-1][0] + 1
            if len(batch) < 1000:
                break
            time.sleep(0.2)
        data[c] = rows
    json.dump(data, open(FUND, "w"))
    return data


def funding_cost(fills, cap_curve, fund):
    """Доля капитала, ушедшая на funding за отрезок: Π(1 − плата_t / капитал_t) − 1 → отрицательное число."""
    if fills.empty:
        return 0.0, {}
    fills = fills.sort_values("timestamp")
    t_start, t_stop = cap_curve.index[0], cap_curve.index[-1]
    mult, per_coin, events = 1.0, {}, []
    for c, g in fills.groupby("coin"):
        pos = pd.Series((g["psize"].astype(float) * g["pprice"].astype(float)).values,
                        index=pd.to_datetime(g["timestamp"]))
        pos = pos[~pos.index.duplicated(keep="last")]
        fr = fund.get(c) or []
        if not fr:
            continue
        ft = pd.to_datetime([x[0] for x in fr], unit="ms")
        rate = np.array([x[1] for x in fr])
        m = (ft >= t_start) & (ft <= t_stop)
        ft, rate = ft[m], rate[m]
        if not len(ft):
            continue
        held = pos.reindex(pos.index.union(ft)).ffill().reindex(ft).fillna(0.0).values
        events.append(pd.Series(held * rate, index=ft))
        per_coin[c] = float((held * rate).sum())
    if not events:
        return 0.0, {}
    pay = pd.concat(events).groupby(level=0).sum().sort_index()
    eq = cap_curve.reindex(cap_curve.index.union(pay.index)).ffill().reindex(pay.index).values * STAKE
    for p, e in zip(pay.values, eq):
        if e > 0:
            mult *= 1 - p / e
    return mult - 1, per_coin


def chain(name, extra, coins, period, btc, fund):
    start, end = UC.PERIODS[period]
    start = (pd.Timestamp(start) + pd.Timedelta(days=OFFSET)).strftime("%Y-%m-%d")
    d, cap, pieces, exits, fmult, fills_n, days_run = pd.Timestamp(start), 1.0, [], [], 1.0, 0, 0.0
    by_coin, kept = {}, []
    t_end = pd.Timestamp(end)
    over = {**UC.OVER, "backtest.starting_balance": STAKE, **extra}
    while d < t_end - pd.Timedelta(days=1):
        if DR.crash_minutes(btc, d.strftime("%Y-%m-%d"), 1, 12, 0.05)[0]:
            d += pd.Timedelta(days=1)
            continue
        tag = f"mv_{name}_{abs(hash(period)) % 10**6}_{d:%Y%m%d}"
        res = DS.backtest_curve(dict(over), d.strftime("%Y-%m-%d"), end, tag, base=UT.EM.EMA, coins=coins)
        if isinstance(res, str):
            return res
        rel, rd = res
        fpaths = sorted(glob.glob(os.path.join(rd, "*", "*", "fills.csv*")), key=os.path.getmtime)
        fills = pd.read_csv(fpaths[-1]) if fpaths else pd.DataFrame()
        shutil.rmtree(rd, ignore_errors=True)
        idx = d + pd.to_timedelta(np.arange(len(rel)), unit="min")
        fl = DR.crash_minutes(btc, d.strftime("%Y-%m-%d"), len(rel), 12, 0.05)
        hit = np.flatnonzero(fl)
        cut = int(hit[0]) if hit.size else len(rel)
        seg = pd.Series(cap * rel[:cut + 1], index=idx[:cut + 1])
        if not fills.empty:
            fills = fills[pd.to_datetime(fills["timestamp"]) <= idx[min(cut, len(idx) - 1)]]
            fills_n += len(fills)
            kept.append(fills[["timestamp", "coin", "psize", "pprice", "pnl", "fee_paid", "type"]].assign(seg=len(kept), cap=cap))
        # funding считается на капитал отрезка, без учёта прошлых плат (поправка мала)
        fc, pc = funding_cost(fills, seg / cap, fund) if not fills.empty else (0.0, {})
        fmult *= 1 + fc
        for c, v in pc.items():                     # плата по монете, % капитала начала отрезка (− = получили)
            by_coin[c] = by_coin.get(c, 0.0) + v / STAKE * 100
        days_run += cut / 1440
        if not hit.size:
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        i = cut
        out = cap * float(rel[i:i + 3].min()) * (1 - UC.COST)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        exits.append(str(idx[i]))
        cap = out
        d = (idx[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([cap], index=[d]))
    os.makedirs(FILLS, exist_ok=True)
    if kept:
        pd.concat(kept).to_csv(os.path.join(FILLS, f"{name}_{list(UC.PERIODS).index(period)}.csv.gz"), index=False)
    curve = pd.concat(pieces)
    curve = curve[~curve.index.duplicated(keep="last")].sort_index()
    h = curve.resample("1h").last().ffill()
    days = (h.index[-1] - h.index[0]).total_seconds() / 86400
    ret = h.iloc[-1] / h.iloc[0] - 1
    net = (1 + ret) * fmult - 1
    return {"ret": round(ret * 100, 1), "net": round(net * 100, 1), "funding": round((fmult - 1) * 100, 1),
            "month_net": round(((1 + net) ** (30 / days) - 1) * 100, 1),
            "dd": round(float((1 - h / h.cummax()).max()) * 100, 1), "exits": len(exits),
            "fills_day": round(fills_n / max(days_run, 1), 1),
            "funding_by_coin": {c: round(v, 2) for c, v in sorted(by_coin.items(), key=lambda x: x[1]) if abs(v) >= 0.05}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--only", default="")
    ap.add_argument("--exclude", default="", help="монеты через запятую — проверка «без лучших монет»")
    ap.add_argument("--offset", type=int, default=0, help="сдвиг старта каждого периода, дней")
    a = ap.parse_args()
    global OFFSET
    OFFSET = a.offset
    DS.EP.STAKE = STAKE
    drop = set(filter(None, a.exclude.split(",")))
    coins = [c for c in UT.universes()["оборот ≥ 5 млн $"] if c not in drop]
    suffix = (f"_x{len(drop)}" if drop else "") + (f"_o{a.offset}" if a.offset else "")
    if suffix:
        for k in list(VARIANTS):
            VARIANTS[k + suffix] = (VARIANTS[k][0] + suffix, VARIANTS[k][1])
    fund = funding(coins)
    print(f"funding: {sum(1 for c in coins if fund.get(c))}/{len(coins)} монет с историей", flush=True)
    btc = RR.btc_hourly()
    names = [n + suffix for n in VARIANTS if (not a.only or n in a.only.split(",")) and n + suffix in VARIANTS]
    jobs = [(n, p) for n in names for p in UC.PERIODS]
    path = os.path.join(RES, "main_variants.json")
    saved = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}

    def job(j):
        r = chain(j[0], VARIANTS[j[0]][1], coins, j[1], btc, fund)
        print(f"  готово {j[0]} / {j[1]}: {r if isinstance(r, str) else r['net']}", flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        out = dict(zip(jobs, ex.map(job, jobs)))
    for (n, p), r in out.items():
        saved.setdefault(n, {})[p] = {"error": r[:300]} if isinstance(r, str) else r
    json.dump(saved, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nEMA Anchor, 84 монеты, экспозиция 8, $300, выход по BTC; «чистыми» = после funding\n")
    for n in VARIANTS:
        if n not in saved:
            continue
        tot_g = tot_n = 1.0
        lines = []
        for p in UC.PERIODS:
            x = saved[n].get(p)
            if not x or "error" in x:
                lines.append(f"   {p:<28} {x['error'][:120] if x else '—'}")
                continue
            tot_g *= 1 + x["ret"] / 100
            tot_n *= 1 + x["net"] / 100
            lines.append(f"   {p:<28} {x['ret']:+7.1f}% → чистыми {x['net']:+7.1f}% (funding {x['funding']:+5.1f}%, "
                         f"{x['month_net']:+5.1f}%/мес.), просадка {x['dd']:5.1f}%, выходов {x['exits']}, сделок/день {x['fills_day']}")
        print(f"{n:<9} {VARIANTS[n][0]}: 15 мес. {(tot_g - 1) * 100:+.0f}% → чистыми {(tot_n - 1) * 100:+.0f}%")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
