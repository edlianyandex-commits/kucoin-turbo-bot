r"""Проверка «как на реальных деньгах» (пользователь, 01.10.2026): Turbo 5.0 (EMA Anchor 7 × 9) и основной бот v4 (10 × 8),
84 монеты, $300, HSL 35% с перезапуском, выход при обвале BTC с запуском со следующего дня (как turbo/main_variants.py),
но с ухудшенным исполнением:
  как в бэктесте — комиссии KuCoin 0,02/0,06%, проскальзывание рыночных 0,05%, лимитный ордер исполняется при касании,
                   выход по BTC — худшая минута из 3 после сигнала − 0,8% капитала;
  реалистично   — лимитный исполняется, только если цена прошла его на 0,02% (очередь в стакане), проскальзывание 0,2%,
                   выход — худшая минута из 5, на всю открытую позицию комиссия 0,06% + проскальзывание 0,3%;
  пессимистично — прохождение 0,1%, проскальзывание 0,5%, комиссии 0,04/0,10%, выход — худшая из 10 минут, проскальзывание 1%.
Funding — по настоящим ставкам KuCoin (turbo/results/funding_kucoin.json) на позиции из сделок.
Риски: просадка по минутам, худший 30-дневный отрезок, запас до ликвидации (капитал / объём позиций в худшую минуту),
наибольшая позиция в одной монете и «что, если эта монета упадёт ещё на 70%».

    venv\Scripts\python turbo\real_test.py [--workers 6]
Итог: turbo/results/real_test.json и таблица.
"""
import argparse
import glob
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import funding_check as FC  # noqa: E402
import main_variants as MV  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

L = "bot.long.risk."
BOTS = {"Turbo 5.0 (7 × 9)": {L + "n_positions": 7.0, L + "total_wallet_exposure_limit": 9.0},
        "Основной v4 (10 × 8)": {}}
LEVELS = {
    "как в бэктесте": dict(over={}, exit_min=3, exit_cost=None),
    "реалистично": dict(over={"backtest.limit_order_fill_buffer_pct": 0.0002, "backtest.market_order_slippage_pct": 0.002},
                        exit_min=5, exit_cost=0.0006 + 0.003),
    "пессимистично": dict(over={"backtest.limit_order_fill_buffer_pct": 0.001, "backtest.market_order_slippage_pct": 0.005,
                                "backtest.maker_fee_override": 0.0004, "backtest.taker_fee_override": 0.001},
                          exit_min=10, exit_cost=0.001 + 0.01),
}
OUT = os.path.join(MV.RES, "real_test")


def chain(bot, level, period, coins, btc):
    start, end = UC.PERIODS[period]
    lv = LEVELS[level]
    over = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **BOTS[bot], **lv["over"]}
    d, cap, pieces, exits, kept = pd.Timestamp(start), 1.0, [], [], []
    t_end = pd.Timestamp(end)
    while d < t_end - pd.Timedelta(days=1):
        if DR.crash_minutes(btc, d.strftime("%Y-%m-%d"), 1, 12, 0.05)[0]:
            d += pd.Timedelta(days=1)
            continue
        tag = f"rt_{abs(hash((bot, level, period))) % 10**7}_{d:%Y%m%d}"
        res = DS.backtest_curve(dict(over), d.strftime("%Y-%m-%d"), end, tag, base=UT.EM.EMA, coins=coins)
        if isinstance(res, str):
            return res
        rel, rd = res
        fp = sorted(glob.glob(os.path.join(rd, "*", "*", "fills.csv*")), key=os.path.getmtime)
        fills = pd.read_csv(fp[-1]) if fp else pd.DataFrame()
        shutil.rmtree(rd, ignore_errors=True)
        idx = d + pd.to_timedelta(np.arange(len(rel)), unit="min")
        hit = np.flatnonzero(DR.crash_minutes(btc, d.strftime("%Y-%m-%d"), len(rel), 12, 0.05))
        cut = int(hit[0]) if hit.size else len(rel) - 1
        if not fills.empty:
            fills["t"] = pd.to_datetime(fills["timestamp"])
            fills = fills[fills["t"] <= idx[cut]]
            kept.append(fills.assign(seg=len(kept), cap=cap))
        if not hit.size:
            pieces.append(pd.Series(cap * rel, index=idx))
            break
        i = cut
        twe = float(fills["twe_long"].iloc[-1]) if not fills.empty else 0.0       # открытая позиция к балансу на выходе
        worst = float(rel[i:i + lv["exit_min"]].min())
        cost = UC.COST if lv["exit_cost"] is None else twe * lv["exit_cost"]
        out = cap * worst * (1 - cost)
        pieces.append(pd.Series(np.r_[cap * rel[:i], out], index=idx[:i + 1]))
        exits.append({"t": str(idx[i]), "twe": round(twe, 2), "cost_pct": round(cost * 100, 2),
                      "slip_pct": round((worst / float(rel[i]) - 1) * 100, 2)})
        cap = out
        d = (idx[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([cap], index=[d]))
    curve = pd.concat(pieces)
    curve = curve[~curve.index.duplicated(keep="last")].sort_index()
    fills = pd.concat(kept) if kept else pd.DataFrame()
    os.makedirs(OUT, exist_ok=True)
    key = f"{list(BOTS).index(bot)}_{list(LEVELS).index(level)}_{list(UC.PERIODS).index(period)}"
    np.save(os.path.join(OUT, f"{key}_curve.npy"), np.c_[curve.index.values.astype("int64") // 60_000_000_000, curve.values])
    if not fills.empty:
        fills[["timestamp", "coin", "type", "psize", "pprice", "pnl", "fee_paid", "wallet_exposure", "twe_long",
               "usd_total_balance", "seg", "cap"]].to_csv(os.path.join(OUT, f"{key}_fills.csv.gz"), index=False)
    return {"exits": exits}


def stats(curve, fills, ku):
    """Итоги отрезка по поминутной кривой (капитал к старту) и сделкам."""
    eq = curve.values
    ret = eq[-1] / eq[0] - 1
    dd_min = float((1 - eq / np.maximum.accumulate(eq)).max())
    h = curve.resample("1h").last().ffill()
    dd_h = float((1 - h / h.cummax()).max())
    fund_pct, by_coin = FC.cost(fills, ku) if len(fills) else (0.0, {})
    out = {"ret": ret * 100, "fund": fund_pct, "dd_min": dd_min * 100, "dd_hour": dd_h * 100}
    if len(fills):
        f = fills.assign(t=pd.to_datetime(fills["timestamp"]))
        out["max_we"] = float(f["wallet_exposure"].max())
        mw = f.loc[f["wallet_exposure"].idxmax()]
        out["max_we_coin"], out["max_we_t"] = str(mw["coin"]), str(mw["t"])[:16]
        out["max_twe"] = float(f["twe_long"].max())
        pan = f[f["type"].astype(str).str.contains("panic")]
        out["hsl_panics"] = int(pan["t"].dt.floor("D").nunique())          # дней со стопом passivbot (закрытие panic)
        out["fills_day"] = len(f) / max(1.0, (curve.index[-1] - curve.index[0]).total_seconds() / 86400)
        # запас до ликвидации в худшую минуту: капитал / объём позиций (по последней сделке до этой минуты)
        i = int(np.argmin(eq / np.maximum.accumulate(eq)))
        t_worst = curve.index[i]
        before = f[f["t"] <= t_worst]
        if len(before):
            last = before.iloc[-1]
            bal = float(last["usd_total_balance"])
            notional = float(last["twe_long"]) * bal
            # капитал в $ отрезка: кривая — в долях стартовых $300, отрезок начинался с капитала cap
            seg_cap = float(last["cap"])
            eq_seg_usd = float(eq[i]) / seg_cap * MV.STAKE
            out["worst_t"] = str(t_worst)[:16]
            out["margin_ratio"] = eq_seg_usd / notional if notional > 0 else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    DS.EP.STAKE = MV.STAKE
    coins = UT.universes()["оборот ≥ 5 млн $"]
    btc = RR.btc_hourly()
    jobs = [(b, lv, p) for b in BOTS for lv in LEVELS for p in UC.PERIODS]

    def job(j):
        r = chain(*j, coins, btc)
        print("  готово", j, r if isinstance(r, str) else len(r["exits"]), flush=True)
        return r
    with ThreadPoolExecutor(a.workers) as ex:
        res = dict(zip(jobs, ex.map(job, jobs)))
    ku = json.load(open(FC.KU))
    report = {}
    for b in BOTS:
        for lv in LEVELS:
            parts, curves = [], []
            total, total_net = 1.0, 1.0
            for p in UC.PERIODS:
                r = res[(b, lv, p)]
                if isinstance(r, str):
                    parts.append({"period": p, "error": r[:300]})
                    continue
                key = f"{list(BOTS).index(b)}_{list(LEVELS).index(lv)}_{list(UC.PERIODS).index(p)}"
                arr = np.load(os.path.join(OUT, f"{key}_curve.npy"))
                curve = pd.Series(arr[:, 1], index=pd.to_datetime(arr[:, 0].astype("int64"), unit="m"))
                fp = os.path.join(OUT, f"{key}_fills.csv.gz")
                fills = pd.read_csv(fp) if os.path.exists(fp) else pd.DataFrame()
                st = stats(curve, fills, ku)
                st.update(period=p, exits=r["exits"])
                parts.append(st)
                total *= 1 + st["ret"] / 100
                total_net *= (1 + st["ret"] / 100) * (1 + st["fund"] / 100)
                curves.append(curve / curve.iloc[0])
            # 30-дневные отрезки по склеенной кривой (каждый период — с капиталом на конец прошлого)
            glued, k = [], 1.0
            for c in curves:
                glued.append(c * k)
                k = float((c * k).iloc[-1])
            g = pd.concat(glued)
            g = g[~g.index.duplicated(keep="last")].sort_index().resample("1D").last().ffill()
            m30 = (g.shift(-30) / g).dropna() - 1
            report[f"{b} | {lv}"] = {"bot": b, "level": lv, "periods": parts, "total": (total - 1) * 100,
                                    "total_net": (total_net - 1) * 100,
                                    "month_geo": (total_net ** (30 / max(1, (g.index[-1] - g.index[0]).days)) - 1) * 100,
                                    "m30_neg": float((m30 < 0).mean() * 100), "m30_worst": float(m30.min() * 100),
                                    "m30_median": float(m30.median() * 100)}
    json.dump(report, open(os.path.join(MV.RES, "real_test.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1,
              default=str)
    for k, r in report.items():
        print(f"\n{k}: 15 мес. {r['total']:+.0f}% → с funding KuCoin {r['total_net']:+.0f}% (≈{r['month_geo']:+.1f}%/мес.), "
              f"30-дневных отрезков в минусе {r['m30_neg']:.0f}%, худший {r['m30_worst']:+.1f}%, медиана {r['m30_median']:+.1f}%")
        for x in r["periods"]:
            if "error" in x:
                print(f"   {x['period']}: {x['error'][:150]}")
                continue
            print(f"   {x['period']:<28} {x['ret']:+7.1f}% (funding {x['fund']:+5.1f}%), просадка по минутам {x['dd_min']:5.1f}% "
                  f"(по часам {x['dd_hour']:4.1f}%), выходов по BTC {len(x['exits'])}, стопов passivbot {x.get('hsl_panics', 0)}, "
                  f"наиб. позиция {x.get('max_we', 0):.2f}× ({x.get('max_we_coin')} {x.get('max_we_t')}), "
                  f"капитал/объём в худшую минуту {x.get('margin_ratio') or 0:.2f}")


if __name__ == "__main__":
    main()
