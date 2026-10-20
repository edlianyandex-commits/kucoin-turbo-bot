r"""Привязка ботов к биткоину (пользователь, 01.10.2026: «цифры привязки биткоина к нашей стратегии, это критично?»).

Цепочки из turbo/real_test.py, условия «реалистично»: Turbo 5.0 (7 × 9) и основной v4 (10 × 8), 84 монеты, $300,
выход при обвале BTC с паузой до следующего дня. Против часовых закрытий BTC (Binance) и равновзвешенной корзины
35 альткоинов (turbo/results/ohlc_1h_2025-08.npz, с 01.08.2025):
  корреляция и бета доходов по дням / неделям / месяцам; доход ботов по корзинам дневного хода BTC;
  месяцы ботов против месяцев BTC; выходы по BTC и дни паузы.
Плюс те же периоды БЕЗ выхода по BTC (один прогон passivbot на период, кэш turbo/results/btc_link/) — сколько выход
сберёг или стоил, в целом и на каждом выходе (как двигался капитал «без выхода» за время паузы).

    venv\Scripts\python turbo\btc_link.py [--workers 6]
Итог: turbo/results/btc_link.json и таблицы.
"""
import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_study as DS  # noqa: E402
import main_variants as MV  # noqa: E402
import real_test as RT  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

OUT = os.path.join(MV.RES, "btc_link")
LEVEL = "реалистично"
BOT_KEYS = {"Turbo 5.0 (7 × 9)": "turbo", "Основной v4 (10 × 8)": "v4"}


def chain_curve(bot):
    """Склеенная поминутная кривая цепочки с выходом по BTC (доли стартового капитала) по трём периодам."""
    parts, k = [], 1.0
    for pi in range(len(UC.PERIODS)):
        arr = np.load(os.path.join(RT.OUT, f"{list(RT.BOTS).index(bot)}_{list(RT.LEVELS).index(LEVEL)}_{pi}_curve.npy"))
        c = pd.Series(arr[:, 1], index=pd.to_datetime(arr[:, 0].astype("int64"), unit="m"))
        parts.append(c / c.iloc[0] * k)
        k = float(parts[-1].iloc[-1])
    g = pd.concat(parts)
    return g[~g.index.duplicated(keep="last")].sort_index()


def no_exit(bot, period, coins):
    """Тот же период без выхода по BTC: один прогон passivbot, поминутно (доли старта)."""
    path = os.path.join(OUT, f"noexit_{BOT_KEYS[bot]}_{list(UC.PERIODS).index(period)}.npy")
    start, end = UC.PERIODS[period]
    if not os.path.exists(path):
        over = {**UC.OVER, "backtest.starting_balance": MV.STAKE, **RT.BOTS[bot], **RT.LEVELS[LEVEL]["over"]}
        res = DS.backtest_curve(over, start, end, f"bl_{BOT_KEYS[bot]}_{list(UC.PERIODS).index(period)}",
                                base=UT.EM.EMA, coins=coins)
        if isinstance(res, str):
            return res
        rel, rd = res
        shutil.rmtree(rd, ignore_errors=True)
        os.makedirs(OUT, exist_ok=True)
        np.save(path, rel)
    rel = np.load(path)
    return pd.Series(rel, index=pd.Timestamp(start) + pd.to_timedelta(np.arange(len(rel)), unit="min"))


def alt_basket_daily():
    z = np.load(os.path.join(MV.RES, "ohlc_1h_2025-08.npz"), allow_pickle=True)
    t = z["t"]
    idx = pd.to_datetime(t.astype("int64"), unit="ms") if np.issubdtype(t.dtype, np.number) else pd.to_datetime(t)
    c = pd.DataFrame(z["c"], index=idx, columns=list(z["coins"]))
    d = c.resample("1D").last()
    return d.pct_change().mean(axis=1, skipna=True).dropna()


def daily(series):
    return series.resample("1D").last().ffill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    DS.EP.STAKE = MV.STAKE
    coins = UT.universes()["оборот ≥ 5 млн $"]
    jobs = [(b, p) for b in RT.BOTS for p in UC.PERIODS]
    with ThreadPoolExecutor(a.workers) as ex:
        noex = dict(zip(jobs, ex.map(lambda j: no_exit(*j, coins), jobs)))
    btc_h = RR.btc_hourly()
    t0, t1 = pd.Timestamp("2025-07-01"), pd.Timestamp("2026-09-30")
    btc_d = daily(btc_h[(btc_h.index >= t0 - pd.Timedelta(days=1)) & (btc_h.index < t1)])
    btc_r = btc_d.pct_change().dropna()
    alt_r = alt_basket_daily()
    real = json.load(open(os.path.join(MV.RES, "real_test.json"), encoding="utf-8"))
    report = {"btc": {}, "bots": {}}
    for p, (s, e) in UC.PERIODS.items():
        b = btc_h[(btc_h.index >= pd.Timestamp(s)) & (btc_h.index < pd.Timestamp(e))]
        report["btc"][p] = round((b.iloc[-1] / b.iloc[0] - 1) * 100, 1)
    report["btc"]["15 мес."] = round((btc_h[btc_h.index < t1].iloc[-1] / btc_h[btc_h.index >= t0].iloc[0] - 1) * 100, 1)
    months = {}
    for bot in RT.BOTS:
        curve = chain_curve(bot)
        bd = daily(curve)
        br = bd.pct_change().dropna()
        j = pd.concat([br.rename("bot"), btc_r.rename("btc")], axis=1, join="inner").dropna()
        ja = pd.concat([br.rename("bot"), alt_r.rename("alt")], axis=1, join="inner").dropna()
        w = pd.concat([bd.resample("W").last().pct_change().rename("bot"),
                       btc_d.resample("W").last().pct_change().rename("btc")], axis=1).dropna()
        m = pd.concat([bd.resample("MS").last().pct_change().rename("bot"),
                       btc_d.resample("MS").last().pct_change().rename("btc")], axis=1).dropna()
        # месяцы: доход за календарный месяц (последнее значение месяца к последнему значению прошлого)
        bm = bd.resample("ME").last()
        bm = pd.concat([pd.Series([1.0], index=[bm.index[0] - pd.offsets.MonthEnd(1)]), bm]).pct_change().dropna()
        cm = btc_d.resample("ME").last().pct_change()
        cm.iloc[0] = btc_d.resample("ME").last().iloc[0] / btc_d.iloc[0] - 1
        for dt, v in bm.items():
            months.setdefault(dt.strftime("%Y-%m"), {})[bot] = round(v * 100, 1)
            months[dt.strftime("%Y-%m")]["BTC"] = round(float(cm.get(dt, np.nan)) * 100, 1)
        buckets = [(-1, -0.05), (-0.05, -0.02), (-0.02, 0), (0, 0.02), (0.02, 0.05), (0.05, 1)]
        bk = []
        for lo, hi in buckets:
            sel = j[(j["btc"] > lo) & (j["btc"] <= hi)]
            bk.append({"btc_day": f"{lo * 100:+.0f}…{hi * 100:+.0f}%" if abs(lo) < 1 and abs(hi) < 1
                       else (f"≤ {hi * 100:+.0f}%" if lo == -1 else f"> {lo * 100:+.0f}%"),
                       "days": int(len(sel)), "bot_mean": round(float(sel["bot"].mean()) * 100, 2) if len(sel) else None,
                       "bot_pos": round(float((sel["bot"] > 0).mean()) * 100) if len(sel) else None})
        worst_btc = j.nsmallest(10, "btc")
        worst_bot = j.nsmallest(10, "bot")
        beta = float(np.cov(j["bot"], j["btc"])[0, 1] / np.var(j["btc"], ddof=1))
        beta_a = float(np.cov(ja["bot"], ja["alt"])[0, 1] / np.var(ja["alt"], ddof=1))
        up, dn = m[m["btc"] > 0], m[m["btc"] <= 0]
        # выходы по BTC: время, длина паузы (разрыв в кривой), движение капитала «без выхода» за время паузы
        exits = []
        rp = real[f"{bot} | {LEVEL}"]["periods"]
        for pi, (p, (s, e)) in enumerate(UC.PERIODS.items()):
            ne = noex[(bot, p)]
            for x in rp[pi]["exits"]:
                tx = pd.Timestamp(x["t"])
                nxt = curve.index[curve.index > tx + pd.Timedelta(minutes=5)]
                tr = nxt[0] if len(nxt) else pd.Timestamp(e)
                held = None
                if not isinstance(ne, str) and tx >= ne.index[0] and tr <= ne.index[-1]:
                    v0, v1 = float(ne.asof(tx)), float(ne.asof(tr))
                    held = round((v1 / v0 - 1) * 100, 1) if v0 > 0 else None
                elif not isinstance(ne, str) and tr > ne.index[-1]:
                    held = round((float(ne.iloc[-1]) / float(ne.asof(tx)) - 1) * 100, 1)   # без выхода счёт сгорел раньше
                bh = btc_h[(btc_h.index >= tx.floor("h")) & (btc_h.index <= tr)]
                exits.append({"t": str(tx)[:16], "pause_days": round((tr - tx).total_seconds() / 86400, 1),
                              "exit_cost_pct": round(x["cost_pct"] - x["slip_pct"], 2), "held_pct": held,
                              "btc_during_pct": round((bh.iloc[-1] / bh.iloc[0] - 1) * 100, 1) if len(bh) > 1 else None})
        per = {}
        for pi, (p, (s, e)) in enumerate(UC.PERIODS.items()):
            ne = noex[(bot, p)]
            with_exit = rp[pi]["ret"]
            if isinstance(ne, str):
                per[p] = {"with_exit": round(with_exit, 1), "no_exit": None, "error": ne[:200]}
                continue
            liq = len(ne) < (pd.Timestamp(e) - pd.Timestamp(s)).total_seconds() / 60 - 1440
            nd = float((1 - ne / ne.cummax()).max())
            per[p] = {"with_exit": round(with_exit, 1), "no_exit": round((float(ne.iloc[-1]) - 1) * 100, 1),
                      "no_exit_liquidated": bool(liq), "no_exit_dd_min": round(nd * 100, 1),
                      "with_exit_dd_min": round(rp[pi]["dd_min"], 1)}
        report["bots"][bot] = {
            "corr_day_btc": round(float(j["bot"].corr(j["btc"])), 2), "beta_day_btc": round(beta, 2),
            "corr_day_alts": round(float(ja["bot"].corr(ja["alt"])), 2), "beta_day_alts": round(beta_a, 2),
            "corr_week_btc": round(float(w["bot"].corr(w["btc"])), 2), "corr_month_btc": round(float(m["bot"].corr(m["btc"])), 2),
            "r2_day_btc": round(float(j["bot"].corr(j["btc"]) ** 2) * 100),
            "months_btc_up": {"n": int(len(up)), "bot_mean": round(float(up["bot"].mean()) * 100, 1),
                              "bot_pos": round(float((up["bot"] > 0).mean()) * 100)},
            "months_btc_down": {"n": int(len(dn)), "bot_mean": round(float(dn["bot"].mean()) * 100, 1),
                                "bot_pos": round(float((dn["bot"] > 0).mean()) * 100)},
            "buckets": bk,
            "worst10_btc_days": {"btc_mean": round(float(worst_btc["btc"].mean()) * 100, 1),
                                 "bot_mean": round(float(worst_btc["bot"].mean()) * 100, 1)},
            "worst10_bot_days": {"bot_mean": round(float(worst_bot["bot"].mean()) * 100, 1),
                                 "btc_mean": round(float(worst_bot["btc"].mean()) * 100, 1),
                                 "btc_down_share": round(float((worst_bot["btc"] < 0).mean()) * 100),
                                 "days": [f"{d:%d.%m.%y}: бот {r.bot * 100:+.1f}%, BTC {r.btc * 100:+.1f}%"
                                          for d, r in worst_bot.iterrows()]},
            "exits": exits, "pause_days_total": round(sum(x["pause_days"] for x in exits), 1), "periods": per}
    report["months"] = months
    json.dump(report, open(os.path.join(MV.RES, "btc_link.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("BTC:", ", ".join(f"{k} {v:+.1f}%" for k, v in report["btc"].items()))
    for bot, r in report["bots"].items():
        print(f"\n=== {bot} (условия «{LEVEL}»)")
        print(f"  корреляция с BTC: по дням {r['corr_day_btc']}, по неделям {r['corr_week_btc']}, по месяцам {r['corr_month_btc']}; "
              f"бета по дням {r['beta_day_btc']} (BTC объясняет {r['r2_day_btc']}% дневных колебаний)")
        print(f"  с корзиной альтов: корреляция {r['corr_day_alts']}, бета {r['beta_day_alts']}")
        u, d = r["months_btc_up"], r["months_btc_down"]
        print(f"  месяцы роста BTC ({u['n']}): бот в среднем {u['bot_mean']:+.1f}%, в плюсе {u['bot_pos']}%; "
              f"месяцы падения BTC ({d['n']}): {d['bot_mean']:+.1f}%, в плюсе {d['bot_pos']}%")
        print("  дневной ход BTC → бот в тот же день: " + "; ".join(
            f"{x['btc_day']} ({x['days']} дн.) {x['bot_mean']:+.2f}%, в плюсе {x['bot_pos']}%" for x in r["buckets"] if x["days"]))
        w1, w2 = r["worst10_btc_days"], r["worst10_bot_days"]
        print(f"  10 худших дней BTC (в среднем {w1['btc_mean']:+.1f}%): бот {w1['bot_mean']:+.1f}%; "
              f"10 худших дней бота ({w2['bot_mean']:+.1f}%): BTC {w2['btc_mean']:+.1f}%, BTC падал в {w2['btc_down_share']}% из них")
        print("    " + " | ".join(w2["days"]))
        print(f"  выходов по BTC {len(r['exits'])}, на паузе всего {r['pause_days_total']} дн.")
        for x in r["exits"]:
            print(f"    {x['t']}: пауза {x['pause_days']} дн., BTC за паузу {x['btc_during_pct']:+.1f}%, "
                  f"«без выхода» капитал за это время {x['held_pct'] if x['held_pct'] is not None else '—'}%")
        for p, x in r["periods"].items():
            if x.get("no_exit") is None:
                print(f"  {p}: с выходом {x['with_exit']:+.1f}%, без выхода — ошибка {x.get('error')}")
                continue
            print(f"  {p}: с выходом {x['with_exit']:+.1f}% (минутная просадка {x['with_exit_dd_min']}%), без выхода {x['no_exit']:+.1f}%"
                  f"{' — счёт сгорел' if x['no_exit_liquidated'] else ''} (просадка {x['no_exit_dd_min']}%)")
    print("\nМесяцы (BTC / Turbo 5.0 / основной v4), %:")
    for mo, v in sorted(months.items()):
        print(f"  {mo}: BTC {v.get('BTC', float('nan')):+6.1f} | Turbo {v.get('Turbo 5.0 (7 × 9)', float('nan')):+6.1f} | "
              f"v4 {v.get('Основной v4 (10 × 8)', float('nan')):+6.1f}")


if __name__ == "__main__":
    main()
