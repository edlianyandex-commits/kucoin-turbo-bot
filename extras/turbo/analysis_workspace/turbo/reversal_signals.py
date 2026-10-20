r"""Сигналы reversal-бота (C:\reversal-bot, strategy.py) для наших продуктов (пользователь, 01.10.2026: «проверь сигналы
этого бота для наших продуктов»).

Сигнал — ровно условия strategy._evaluate с настройками по умолчанию (StrategyParams, settings.DEFAULTS):
3-минутные свечи; за последние 48 свечей движение ≥ 10 «средних свечей» монеты (средний размах 50 свечей до окна),
экстремум 1–10 свечей назад, откат от экстремума 1,5–4%, объём у экстремума ≥ 1,3 × средний, свеча закрылась против
движения. Лонг — после падения, шорт — после роста. Свечи — минутные Binance из кэша passivbot (high, low, close, volume;
открытие 3-минутной свечи = закрытие предыдущей), монеты ботов, 01.07.2025–30.09.2026.

1) Сила сигнала сама по себе: доход через 15 мин / 1 ч / 4 ч / 24 ч после сигнала против всех моментов тех же монет;
   сделка reversal-бота: тейк 3%, стоп 3%, не дольше 24 ч, комиссии 0,06% × 2 + проскальзывание 0,05%.
2) Сигналы и сделки наших ботов (цепочки turbo/real_test.py, «реалистично»): позиции Turbo 5.0 и основного v4,
   открытые рядом с сигналом разворота (за 60 мин до — 15 мин после) на той же монете, против остальных;
   позиции, во время которых на монете был сигнал «рост и разворот вниз».

    venv\Scripts\python turbo\reversal_signals.py
Итог: turbo/results/reversal_signals.json (+ сигналы в reversal_signals.csv.gz) и таблицы.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view as win

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import main_variants as MV  # noqa: E402
import real_test as RT  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

ROOTS = [os.path.join(os.path.dirname(HERE), "caches", "ohlcvs", "data", "binance", "1m"),
         r"C:\tradebot v4\caches\ohlcvs\data\binance\1m"]
MONTHS = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 10)]
LOOKBACK, MOVE_ATR, MAX_BARS, RET_MIN, RET_MAX, VOL_AVG, VOL_MULT, TF = 48, 10.0, 10, 1.5, 4.0, 50, 1.3, 3
TP, SL, HOLD_MIN, COST = 0.03, 0.03, 24 * 60, 0.0006 * 2 + 0.0005
HORIZONS = {"15 мин": 15, "1 ч": 60, "4 ч": 240, "24 ч": 1440}


def minutes(coin):
    """Минутные high/low/close/volume монеты за 15 месяцев (NaN, где данных нет) и время (UTC, без пояса)."""
    parts = []
    for y, m in MONTHS:
        start = pd.Timestamp(y, m, 1)
        n = int(((start + pd.offsets.MonthBegin(1)) - start).total_seconds() // 60)
        arr = np.full((n, 4), np.nan, dtype=np.float32)
        for r in ROOTS:
            p = os.path.join(r, f"{coin}_USDT_USDT", str(y), f"{m:02d}.npy")
            if os.path.exists(p):
                a = np.load(p)
                v = np.load(p.replace(".npy", ".valid.npy")) if os.path.exists(p.replace(".npy", ".valid.npy")) else None
                k = min(n, len(a))
                arr[:k] = a[:k]
                if v is not None:
                    arr[:k][~v[:k].astype(bool)] = np.nan
                break
        parts.append((start, arr))
    t0 = parts[0][0]
    data = np.concatenate([a for _, a in parts])
    return t0, data


def to_3m(data):
    n = len(data) // TF * TF
    d = data[:n].reshape(-1, TF, 4)
    with np.errstate(all="ignore"):
        high = np.nanmax(d[:, :, 0], axis=1)
        low = np.nanmin(d[:, :, 1], axis=1)
        close = d[:, -1, 2]
        vol = np.nansum(d[:, :, 3], axis=1)
    bad = np.isnan(d[:, :, 2]).any(axis=1)
    high[bad] = low[bad] = close[bad] = np.nan
    opn = np.r_[np.nan, close[:-1]]
    return opn, high, low, close, vol


def signals(opn, high, low, close, vol):
    """Индексы 3-минутных свечей с сигналом лонг / шорт (условия strategy._evaluate, по умолчанию)."""
    n = len(close)
    L = LOOKBACK
    out = {}
    rng = (high - low) / close * 100
    cs_r = np.r_[0, np.nancumsum(np.nan_to_num(rng))]
    cs_v = np.r_[0, np.nancumsum(np.nan_to_num(vol))]
    nan_c = np.r_[0, np.cumsum(np.isnan(close))]
    idx = np.arange(L + VOL_AVG - 1, n)                               # конец окна i: нужны 98 свечей истории
    a, b = idx + 1 - (L + VOL_AVG), idx + 1 - L
    range_avg = (cs_r[b] - cs_r[a]) / VOL_AVG
    vol_avg = (cs_v[b] - cs_v[a]) / VOL_AVG
    clean = (nan_c[idx + 1] - nan_c[a]) == 0
    vol4 = pd.Series(vol).rolling(4, min_periods=1).max().values
    hw, lw = win(high, L)[idx - L + 1], win(low, L)[idx - L + 1]
    for side in ("long", "short"):
        if side == "long":
            ext = np.argmin(lw, axis=1)
            extreme = lw[np.arange(len(idx)), ext]
            base = np.maximum.accumulate(hw, axis=1)[np.arange(len(idx)), ext]
            move = (base - extreme) / base * 100
            retrace = (close[idx] - extreme) / extreme * 100
            candle = close[idx] > opn[idx]
        else:
            ext = np.argmax(hw, axis=1)
            extreme = hw[np.arange(len(idx)), ext]
            base = np.minimum.accumulate(lw, axis=1)[np.arange(len(idx)), ext]
            move = (extreme - base) / base * 100
            retrace = (extreme - close[idx]) / extreme * 100
            candle = close[idx] < opn[idx]
        bars = L - 1 - ext
        ext_abs = idx - L + 1 + ext
        with np.errstate(all="ignore"):
            strength = move / range_avg
            vr = vol4[ext_abs] / np.where(vol_avg > 0, vol_avg, 1e-12)
            ok = (clean & (strength >= MOVE_ATR) & (bars >= 1) & (bars <= MAX_BARS) & (retrace >= RET_MIN)
                  & (retrace <= RET_MAX) & (vr >= VOL_MULT) & candle)
        sig = idx[ok]
        keep, last = [], -10 ** 9                                     # одна сделка за раз: следующая — не раньше 30 мин
        for i in sig:
            if i - last >= MAX_BARS:
                keep.append(i)
                last = i
        out[side] = np.array(keep, dtype=np.int64)
    return out


def trade(c1, h1, l1, m0, side):
    """Сделка reversal-бота от минуты m0 (вход по закрытию сигнальной свечи): тейк/стоп 3%, 24 ч; при касании обоих
    в одну минуту — стоп. Доход за вычетом расходов."""
    entry = c1[m0]
    seg = slice(m0 + 1, min(len(c1), m0 + 1 + HOLD_MIN))
    hh, ll, cc = h1[seg], l1[seg], c1[seg]
    if side == "long":
        tp_hit, sl_hit = hh >= entry * (1 + TP), ll <= entry * (1 - SL)
    else:
        tp_hit, sl_hit = ll <= entry * (1 - TP), hh >= entry * (1 + SL)
    t_tp = np.argmax(tp_hit) if tp_hit.any() else None
    t_sl = np.argmax(sl_hit) if sl_hit.any() else None
    if t_sl is not None and (t_tp is None or t_sl <= t_tp):
        r = -SL
    elif t_tp is not None:
        r = TP
    else:
        last = cc[~np.isnan(cc)][-1] if (~np.isnan(cc)).any() else entry
        r = (last / entry - 1) * (1 if side == "long" else -1)
    return r - COST


def main():
    coins = [c for c in UT.universes()["оборот ≥ 5 млн $"] if c not in ("BTC",)]
    rows, fwd_all = [], {h: [] for h in HORIZONS}
    for c in coins:
        t0, d = minutes(c)
        if np.isnan(d[:, 2]).all():
            continue
        c1, h1, l1 = d[:, 2].astype(float), d[:, 0].astype(float), d[:, 1].astype(float)
        opn, high, low, close, vol = to_3m(d.astype(float))
        sig = signals(opn, high, low, close, vol)
        for h, m in HORIZONS.items():                                  # все моменты (каждые 3 мин) — для сравнения
            base = c1[2::TF]
            j = np.arange(len(base)) * TF + 2
            ok = j + m < len(c1)
            fr = c1[j[ok] + m] / c1[j[ok]] - 1
            fwd_all[h].append(fr[~np.isnan(fr)])
        for side, ids in sig.items():
            for i in ids:
                m0 = int(i) * TF + TF - 1                              # последняя минута сигнальной свечи
                rec = {"coin": c, "side": side, "t": t0 + pd.Timedelta(minutes=m0 + 1), "entry": c1[m0]}
                for h, m in HORIZONS.items():
                    rec[h] = (c1[m0 + m] / c1[m0] - 1) * (1 if side == "long" else -1) if m0 + m < len(c1) else np.nan
                rec["trade"] = trade(c1, h1, l1, m0, side)
                rows.append(rec)
        print(f"  {c}: лонг {len(sig['long'])}, шорт {len(sig['short'])}", flush=True)
    S = pd.DataFrame(rows)
    S.to_csv(os.path.join(MV.RES, "reversal_signals.csv.gz"), index=False)
    base = {h: float(np.mean(np.concatenate(v))) for h, v in fwd_all.items()}
    rep = {"signals": {}, "bots": {}}
    print(f"\nСигналов: {len(S)} (лонг {int((S.side == 'long').sum())}, шорт {int((S.side == 'short').sum())}) на {S.coin.nunique()} монетах; "
          f"в среднем {len(S) / S.coin.nunique() / 15:.1f} на монету в месяц")
    S["period"] = pd.cut(S["t"], [pd.Timestamp(s) for s, _ in UC.PERIODS.values()] + [pd.Timestamp("2026-10-01")],
                         labels=list(UC.PERIODS), right=False)
    print("\n1) Сила сигнала: средний доход в сторону сигнала (без расходов) против всех моментов, и сделка бота (тейк/стоп 3%, с расходами)")
    for side in ("long", "short"):
        x = S[S.side == side]
        sgn = 1 if side == "long" else -1
        line = "; ".join(f"{h}: {x[h].mean() * 100:+.2f}% (все {sgn * base[h] * 100:+.2f}%)" for h in HORIZONS)
        tr = x["trade"]
        rep["signals"][side] = {"n": int(len(x)), **{h: round(float(x[h].mean()) * 100, 3) for h in HORIZONS},
                                "trade_mean": round(float(tr.mean()) * 100, 3), "trade_win": round(float((tr > 0).mean()) * 100, 1),
                                "by_period": {str(p): round(float(g["trade"].mean()) * 100, 3) for p, g in x.groupby("period", observed=True)}}
        print(f"  {side}: {len(x)} сигналов | {line}")
        print(f"     сделка: в среднем {tr.mean() * 100:+.2f}%, в плюсе {(tr > 0).mean() * 100:.0f}%, сумма {tr.sum() * 100:+.0f}% "
              f"| по периодам: " + ", ".join(f"{p}: {g['trade'].mean() * 100:+.2f}% ({len(g)})" for p, g in x.groupby("period", observed=True)))
    print("\n2) Сигналы и позиции наших ботов (позиция = от первой покупки до полного закрытия монеты)")
    for bi, bot in enumerate(RT.BOTS):
        eps = []
        for pi in range(len(UC.PERIODS)):
            f = pd.read_csv(os.path.join(RT.OUT, f"{bi}_{list(RT.LEVELS).index('реалистично')}_{pi}_fills.csv.gz"))
            f["t"] = pd.to_datetime(f["timestamp"])
            for (seg, coin), g in f.sort_values("t").groupby(["seg", "coin"]):
                st = None
                for _, r in g.iterrows():
                    if st is None and r.psize > 0:
                        st = {"coin": coin, "start": r.t, "bal": float(r.usd_total_balance), "pnl": 0.0, "max_we": 0.0}
                    if st is not None:
                        st["pnl"] += float(r.pnl) + float(r.fee_paid)
                        st["max_we"] = max(st["max_we"], float(r.wallet_exposure))
                        if r.psize == 0:
                            st["end"] = r.t
                            eps.append(st)
                            st = None
        E = pd.DataFrame(eps)
        E["pnl_pct"] = E["pnl"] / E["bal"] * 100
        sl = S[S.side == "long"].sort_values("t")
        ss = S[S.side == "short"].sort_values("t")
        near_long, during_short = [], []
        for _, e in E.iterrows():
            a = sl[(sl.coin == e.coin) & (sl.t >= e.start - pd.Timedelta(minutes=60)) & (sl.t <= e.start + pd.Timedelta(minutes=15))]
            b = ss[(ss.coin == e.coin) & (ss.t >= e.start) & (ss.t <= e.end)]
            near_long.append(len(a) > 0)
            during_short.append(len(b) > 0)
        E["near_long"], E["during_short"] = near_long, during_short
        res = {}
        for name, sel in (("все позиции", np.ones(len(E), bool)), ("рядом с сигналом разворота вверх", E.near_long.values),
                          ("без такого сигнала", ~E.near_long.values),
                          ("во время позиции был сигнал «рост и разворот вниз»", E.during_short.values)):
            x = E[sel]
            res[name] = {"n": int(len(x)), "mean": round(float(x.pnl_pct.mean()), 3), "median": round(float(x.pnl_pct.median()), 3),
                         "loss_share": round(float((x.pnl_pct < 0).mean()) * 100, 1), "worst": round(float(x.pnl_pct.min()), 2),
                         "sum": round(float(x.pnl_pct.sum()), 1)}
        cov = sl.apply(lambda s: ((E.coin == s.coin) & (E.start >= s.t - pd.Timedelta(minutes=60))
                                  & (E.start <= s.t + pd.Timedelta(minutes=60))).any(), axis=1).mean() * 100
        rep["bots"][bot] = {"episodes": res, "long_signals_with_bot_entry_pct": round(float(cov), 1)}
        print(f"  {bot}: позиций {len(E)}; из сигналов разворота вверх бот и так открывал позицию на этой монете в ±1 ч в {cov:.0f}% случаев")
        for name, x in res.items():
            print(f"    {name:<52} {x['n']:>5} шт., в среднем {x['mean']:+.3f}% баланса, медиана {x['median']:+.3f}%, "
                  f"в минусе {x['loss_share']:.0f}%, худшая {x['worst']:+.2f}%, сумма {x['sum']:+.0f}%")
    json.dump(rep, open(os.path.join(MV.RES, "reversal_signals.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
