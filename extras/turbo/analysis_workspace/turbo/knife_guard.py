r"""«Падающий нож» для наших ботов (пользователь, 01.10.2026: reversal-боту «нужна польза»): помогает ли сканер reversal-бота
не покупать монету, которая ещё падает.

«Нож» на 3-минутной свече: за последние 48 свечей монета упала на ≥ X «средних свечей» (средний размах 50 свечей до окна,
как в reversal-боте), а дно — на этой или прошлой свече (разворота ещё нет). Минутные свечи — кэш Binance
(turbo/reversal_signals.py). Позиции ботов — цепочки turbo/real_test.py («реалистично»): от первой покупки монеты до
полного закрытия; прибыль — % баланса на момент открытия.

Сравнение: позиции, открытые, когда на монете за последние N минут горел «нож», против остальных — средняя прибыль,
доля убыточных, худшие; сколько прибыли и убытка у этих позиций в сумме (что даст запрет новых позиций на «ноже»).

    venv\Scripts\python turbo\knife_guard.py
"""
import os
import sys

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view as win

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import real_test as RT  # noqa: E402
import reversal_signals as RS  # noqa: E402
import universe_chain as UC  # noqa: E402

L, VOL_AVG = 48, 50
THRESHOLDS = (10.0, 15.0, 20.0)
LOOK_MIN = (15, 60, 180)


def knife_times(coin):
    """Время закрытия 3-минутных свечей с «ножом» для каждого порога силы падения."""
    t0, d = RS.minutes(coin)
    if np.isnan(d[:, 2]).all():
        return {}
    opn, high, low, close, vol = RS.to_3m(d.astype(float))
    n = len(close)
    rng = (high - low) / close * 100
    cs = np.r_[0, np.cumsum(np.nan_to_num(rng))]
    idx = np.arange(L + VOL_AVG - 1, n)
    a, b = idx + 1 - (L + VOL_AVG), idx + 1 - L
    range_avg = (cs[b] - cs[a]) / VOL_AVG
    hw, lw = win(high, L)[idx - L + 1], win(low, L)[idx - L + 1]
    ext = np.argmin(lw, axis=1)
    rows = np.arange(len(idx))
    extreme = lw[rows, ext]
    base = np.maximum.accumulate(hw, axis=1)[rows, ext]
    with np.errstate(all="ignore"):
        strength = (base - extreme) / base * 100 / range_avg
    fresh = (L - 1 - ext) <= 1
    out = {}
    for th in THRESHOLDS:
        ok = fresh & (strength >= th)
        out[th] = np.sort(np.asarray(t0 + pd.to_timedelta((idx[ok] + 1) * 3, unit="min"), dtype="datetime64[ns]"))
    return out


def episodes(bi):
    eps = []
    for pi in range(len(UC.PERIODS)):
        f = pd.read_csv(os.path.join(RT.OUT, f"{bi}_1_{pi}_fills.csv.gz"))
        f["t"] = pd.to_datetime(f["timestamp"])
        for (seg, coin), g in f.sort_values("t").groupby(["seg", "coin"]):
            st = None
            for r in g.itertuples():
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
    E["p"] = E["pnl"] / E["bal"] * 100
    return E


def main():
    E = {bot: episodes(bi) for bi, bot in enumerate(RT.BOTS)}
    coins = sorted(set().union(*[set(e.coin) for e in E.values()]))
    K = {}
    for c in coins:
        K[c] = knife_times(c)
    for bot, ep in E.items():
        print(f"\n=== {bot}: позиций {len(ep)}, сумма прибыли {ep.p.sum():+.0f}% (сумма % баланса по позициям), "
              f"убыточных {(ep.p < 0).mean() * 100:.0f}%, худшая {ep.p.min():+.2f}%")
        for th in THRESHOLDS:
            for look in LOOK_MIN:
                flag = []
                for e in ep.itertuples():
                    ts = K.get(e.coin, {}).get(th)
                    if ts is None or not len(ts):
                        flag.append(False)
                        continue
                    a, b = np.datetime64(e.start - pd.Timedelta(minutes=look)), np.datetime64(e.start)
                    i = np.searchsorted(ts, a)
                    flag.append(bool(i < len(ts) and ts[i] <= b))
                flag = np.array(flag)
                x, y = ep.p[flag], ep.p[~flag]
                if not len(x):
                    continue
                big = ep.p.nsmallest(20)
                caught = flag[ep.index.get_indexer(big.index)].mean() * 100
                print(f"  нож ≥{th:g} ср. свечей за {look:>3} мин до открытия: {len(x):>4} поз. ({len(x) / len(ep) * 100:.0f}%), "
                      f"в среднем {x.mean():+.3f}% против {y.mean():+.3f}%, убыточных {(x < 0).mean() * 100:.0f}% против {(y < 0).mean() * 100:.0f}%, "
                      f"сумма {x.sum():+.0f}%, из 20 худших позиций — {caught:.0f}%")


if __name__ == "__main__":
    main()
