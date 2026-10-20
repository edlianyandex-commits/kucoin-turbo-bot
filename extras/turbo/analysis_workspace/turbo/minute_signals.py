r"""Минутные сигналы обвала для выхода «по минуткам» (03.10.2026): выход при обвале BTC по закрытию часа не успевает за
проколами альтов на 25–35% за минуты (03.01.2024, 12.04.2024, 09.12.2024). Сигналы по минутным свечам Binance из кэша
TB Core (caches/ohlcvs/data/binance/1m/<SYM>/<Y>/<MM>.npy: high, low, close, volume):
  btc   — закрытие минуты BTC на X% ниже максимума закрытий за N минут;
  alt   — то же для равновзвешенной корзины монет из списка месяца (честный список Turbo, фильтр разогнанных ×3).
После сигнала бот закрывает всё и ждёт до 00:00 UTC следующего дня (как сейчас при обвале BTC), поэтому повторные
сигналы до конца суток не считаются.

    set TB_FULL=1 && venv\Scripts\python turbo\minute_signals.py scan
Итог: results/minute_signals.json (минуты сигналов по вариантам) и таблица: сколько сигналов в год и за сколько минут до
худшей минуты известных обвалов они срабатывают.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

os.environ.setdefault("TB_FULL", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CACHE = os.path.join(os.path.dirname(HERE), "caches", "ohlcvs", "data", "binance", "1m")
OUT = os.path.join(HERE, "results", "minute_signals.json")
T0, T1 = pd.Timestamp("2024-01-01"), pd.Timestamp("2026-10-01")
EVENTS = {"03.01.2024": "2024-01-03 12:07", "12.04.2024": "2024-04-12 18:31", "09.12.2024": "2024-12-09 21:02",
          "10.10.2025": "2025-10-10 21:15", "UXLINK 22.09.2025": "2025-09-22 23:30"}


def month_close(coin, m):
    """Минутные закрытия монеты за месяц m («ГГГГ-ММ»), NaN где свечи нет."""
    y, mm = m.split("-")
    for sym in (f"{coin}_USDT_USDT", f"1000{coin}_USDT_USDT"):
        p = os.path.join(CACHE, sym, y, f"{mm}.npy")
        if os.path.exists(p):
            x = np.load(p)[:, 2].astype("float64")
            v = p.replace(".npy", ".valid.npy")
            if os.path.exists(v):
                x[~np.load(v)] = np.nan
            x[x <= 0] = np.nan
            return x
    return None


def series():
    """Минутные ряды за 01.2024–09.2026: закрытие BTC и уровень корзины альтов (сумма средних лог-доходностей)."""
    import turnover_rule as T
    lists = T.month_lists(f"hot:{T.MONTHLY}:3")
    btc, alt = [], []
    for ms in pd.date_range(T0, T1 - pd.Timedelta(days=1), freq="MS"):
        m = f"{ms:%Y-%m}"
        b = month_close("BTC", m)
        n = len(b)
        btc.append(b)
        rets = []
        for c in lists.get(m, []):
            x = month_close(c, m)
            if x is None or len(x) != n:
                continue
            r = np.diff(np.log(x), prepend=np.nan)
            rets.append(r)
        if rets:
            R = np.vstack(rets)
            with np.errstate(all="ignore"):
                mean = np.nanmean(R, axis=0)
            alt.append(np.nan_to_num(mean, nan=0.0))
        else:
            alt.append(np.zeros(n))
        print(f"  {m}: альтов в корзине {len(rets)}", flush=True)
    idx = pd.date_range(T0, periods=sum(len(x) for x in btc), freq="min")
    b = pd.Series(np.concatenate(btc), index=idx).ffill()
    a = pd.Series(np.exp(np.cumsum(np.concatenate(alt))), index=idx)
    return b, a


def triggers(s, x, n):
    """Минуты, когда закрытие на x ниже максимума закрытий за n минут; после сигнала — тишина до конца суток."""
    peak = s.rolling(n, min_periods=1).max()
    hit = (s <= peak * (1 - x)).values
    idx = s.index
    out, block_until = [], None
    for i in np.flatnonzero(hit):
        t = idx[i]
        if block_until is not None and t < block_until:
            continue
        out.append(t)
        block_until = t.normalize() + pd.Timedelta(days=1)
    return out


def scan():
    b, a = series()
    res, rows = {}, []
    for kind, s in (("btc", b), ("alt", a)):
        for x in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08):
            for n in (5, 15, 30, 60):
                tr = triggers(s, x, n)
                key = f"{kind}|{x}|{n}"
                res[key] = [str(t) for t in tr]
                years = (T1 - T0).days / 365
                lead = {}
                for ev, tw in EVENTS.items():
                    tw = pd.Timestamp(tw)
                    before = [t for t in tr if tw - pd.Timedelta(hours=6) <= t <= tw + pd.Timedelta(minutes=10)]
                    lead[ev] = int((tw - before[0]) / pd.Timedelta(minutes=1)) if before else None
                rows.append((key, len(tr) / years, lead))
    json.dump(res, open(OUT, "w", encoding="utf-8"))
    print("\nсигнал | срабатываний в год | минут до худшей минуты события (− = после; нет = не сработал)")
    for key, per, lead in rows:
        cells = "  ".join(f"{ev[:10]}: {('нет' if v is None else v):>4}" for ev, v in lead.items())
        print(f"  {key:16s} {per:6.1f}/год | {cells}")


if __name__ == "__main__":
    scan()
