r"""От какой монеты наши боты зависят больше всего (пользователь, 01.10.2026: «найдём крипту, от которой наши боты зависят
больше всего»; продолжение turbo/btc_link.py).

Цены: часовые закрытия Binance USDⓈ-M с 25.06.2025 — BTC, ETH и все 84 монеты ботов (turbo/results/coins_1h_2025-06.npz),
плюс равновзвешенный индекс этих 84 монет («рынок альтов»).
Кривые ботов — цепочки turbo/real_test.py, условия «реалистично»: Turbo 5.0 (7 × 9) и основной v4 (10 × 8).

1) Связь с рынком: для каждой монеты — корреляция доходов бота и монеты по часам и по дням, бета по дням, средний доход
   бота в 10 худших дней монеты, средний ход монеты в 20 худших дней бота.
2) Связь через позиции: средняя экспозиция бота в монете (доля баланса, по времени) и прибыль/убыток по ней
   (сделки цепочек real_test, % стартового капитала периода).

    venv\Scripts\python turbo\coin_link.py
Итог: turbo/results/coin_link.json и таблицы.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import btc_link as BL  # noqa: E402
import main_variants as MV  # noqa: E402
import real_test as RT  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

PRICES = os.path.join(MV.RES, "coins_1h_2025-06.npz")
T_FROM = datetime(2025, 6, 25, tzinfo=timezone.utc)
T_TO = datetime(2026, 10, 1, tzinfo=timezone.utc)


def prices():
    """Часовые закрытия: DataFrame (время открытия часа, UTC без пояса) × монеты; кэш на диске."""
    if os.path.exists(PRICES):
        z = np.load(PRICES, allow_pickle=True)
        return pd.DataFrame(z["c"], index=pd.to_datetime(z["t"].astype("int64"), unit="ms"), columns=list(z["coins"]))
    coins = ["BTC", "ETH"] + [c for c in UT.universes()["оборот ≥ 5 млн $"] if c not in ("BTC", "ETH")]
    s = RR.http()
    t_end = int(T_TO.timestamp() * 1000)
    cols = {}
    for c in coins:
        d, t = {}, int(T_FROM.timestamp() * 1000)
        while t < t_end:
            for attempt in range(5):
                try:
                    rows = s.get("https://fapi.binance.com/fapi/v1/klines",
                                 params={"symbol": c + "USDT", "interval": "1h", "startTime": t, "limit": 1500},
                                 timeout=20).json()
                    break
                except Exception:
                    time.sleep(2 * (attempt + 1))
            else:
                rows = []
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                d[int(r[0])] = float(r[4])
            t = rows[-1][0] + 3_600_000
            time.sleep(0.1)
        cols[c] = pd.Series(d, dtype=float)
        print(f"  {c}: {len(d)} ч", flush=True)
    df = pd.DataFrame(cols).sort_index()
    np.savez_compressed(PRICES, t=df.index.values.astype("int64"), c=df.values, coins=np.array(df.columns))
    df.index = pd.to_datetime(df.index, unit="ms")
    return df


def exposure_pnl(bot):
    """Средняя экспозиция по монетам (доля баланса, по времени) и прибыль по монетам (% стартового капитала периода)."""
    rows = []
    for pi in range(len(UC.PERIODS)):
        f = pd.read_csv(os.path.join(RT.OUT, f"{list(RT.BOTS).index(bot)}_{list(RT.LEVELS).index(BL.LEVEL)}_{pi}_fills.csv.gz"))
        f["t"] = pd.to_datetime(f["timestamp"])
        start, end = (pd.Timestamp(x) for x in list(UC.PERIODS.values())[pi])
        hours = (end - start).total_seconds() / 3600
        for (seg, coin), g in f.groupby(["seg", "coin"]):
            g = g.sort_values("t")
            seg_end = f[f["seg"] == seg]["t"].max()
            nxt = list(g["t"].iloc[1:]) + [seg_end]
            held = sum(float(we) * max(0.0, (t2 - t1).total_seconds() / 3600)
                       for we, t1, t2 in zip(g["wallet_exposure"], g["t"], nxt))
            cap = float(g["cap"].iloc[0])
            rows.append({"period": pi, "coin": coin, "exp_hours": held / hours,
                         "pnl_pct": float((g["pnl"] + g["fee_paid"]).sum()) / MV.STAKE * cap * 100})
    df = pd.DataFrame(rows)
    out = df.groupby("coin").agg(exp=("exp_hours", "sum"), pnl=("pnl_pct", "sum"))
    out["exp"] = out["exp"] / len(UC.PERIODS)            # средняя за три периода
    return out.sort_values("exp", ascending=False)


def main():
    px = prices()
    px = px[(px.index >= "2025-06-30") & (px.index < "2026-09-30")]
    hr = px.pct_change(fill_method=None)
    idx = (1 + hr.drop(columns=["BTC", "ETH"]).mean(axis=1, skipna=True).fillna(0)).cumprod()   # равновзвешенный индекс 84 монет
    px["ИНДЕКС альтов"] = idx * 100
    dr = px.resample("1D").last().pct_change(fill_method=None)
    hr = px.pct_change(fill_method=None)
    report = {}
    for bot in RT.BOTS:
        curve = BL.chain_curve(bot)
        bh = curve.resample("1h").last().ffill().pct_change()
        bd = curve.resample("1D").last().ffill().pct_change()
        worst_bot = bd.dropna().nsmallest(20).index
        rows = []
        for c in px.columns:
            jd = pd.concat([bd.rename("bot"), dr[c].rename("coin")], axis=1, join="inner").dropna()
            jh = pd.concat([bh.rename("bot"), hr[c].rename("coin")], axis=1, join="inner").dropna()
            if len(jd) < 120:
                continue
            worst_coin = jd["coin"].nsmallest(10).index
            wb = jd.reindex(worst_bot).dropna()
            rows.append({"coin": c, "days": len(jd), "corr_day": jd["bot"].corr(jd["coin"]), "corr_hour": jh["bot"].corr(jh["coin"]),
                         "beta_day": float(np.cov(jd["bot"], jd["coin"])[0, 1] / np.var(jd["coin"], ddof=1)),
                         "bot_on_coin_worst10": float(jd.loc[worst_coin, "bot"].mean()) * 100,
                         "coin_worst10": float(jd.loc[worst_coin, "coin"].mean()) * 100,
                         "coin_on_bot_worst20": float(wb["coin"].mean()) * 100,
                         "coin_down3_on_bot_worst20": float((wb["coin"] < -0.03).mean()) * 100})
        mk = pd.DataFrame(rows).set_index("coin").sort_values("corr_day", ascending=False)
        ex = exposure_pnl(bot)
        report[bot] = {"market": mk.round(3).reset_index().to_dict("records"),
                       "exposure": ex.round(3).reset_index().to_dict("records")}
        print(f"\n=== {bot}: связь с монетами (дней {int(mk['days'].median())}), сортировка по корреляции по дням")
        print(f"  {'монета':<16}{'корр. дни':>10}{'корр. часы':>11}{'бета':>7}{'бот в 10 худш. дней монеты':>28}"
              f"{'монета в 20 худш. дней бота':>29}{'из них монета ≤ −3%':>21}")
        show = list(mk.index[:12])
        for c in ("BTC", "ETH", "ИНДЕКС альтов"):
            if c not in show:
                show.append(c)
        for c in show:
            r = mk.loc[c]
            print(f"  {c:<16}{r.corr_day:>10.2f}{r.corr_hour:>11.2f}{r.beta_day:>7.2f}"
                  f"{r.bot_on_coin_worst10:>+21.1f}% (монета {r.coin_worst10:+.0f}%)"
                  f"{r.coin_on_bot_worst20:>+22.1f}%{r.coin_down3_on_bot_worst20:>19.0f}%")
        rank = {c: i + 1 for i, c in enumerate(mk.index)}
        rank_h = {c: i + 1 for i, c in enumerate(mk.sort_values("corr_hour", ascending=False).index)}
        print(f"  места по дням: BTC {rank.get('BTC')}, ETH {rank.get('ETH')}, индекс {rank.get('ИНДЕКС альтов')} из {len(mk)}; "
              f"по часам: BTC {rank_h.get('BTC')}, ETH {rank_h.get('ETH')}, индекс {rank_h.get('ИНДЕКС альтов')}")
        share = ex["exp"] / ex["exp"].sum() * 100
        print(f"  через позиции (доля монеты во всей загрузке бота за 15 мес. и прибыль по ней, % стартового капитала периода):")
        for c, r in ex.head(12).iterrows():
            print(f"    {c:<10} доля загрузки {share[c]:4.1f}%, прибыль {r.pnl:+.1f}%")
        print(f"  всего монет в сделках: {len(ex)}; на 10 крупнейших — {share.head(10).sum():.0f}% загрузки; "
              f"BTC в сделках: {'да' if 'BTC' in ex.index else 'нет'}, ETH: {'да, ' + format(share.get('ETH', 0), '.1f') + '%' if 'ETH' in ex.index else 'нет'}")
    json.dump(report, open(os.path.join(MV.RES, "coin_link.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1,
              default=float)


if __name__ == "__main__":
    main()
