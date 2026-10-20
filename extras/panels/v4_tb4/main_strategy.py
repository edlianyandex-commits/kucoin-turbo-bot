"""Какая стратегия будет основной: TradeBot v4 на trailing martingale («Осторожный + стоп», сейчас) против EMA Anchor —
бот работает без остановок, прибыль в обороте, правила KuCoin, только лонг, стоп капитала 35% как сейчас.

Каждый вариант — без правил панели и с выходом при обвале BTC (как у Turbo 3.0/4.0: закрытие часа BTC на 5% ниже
максимума за 12 ч → закрыть всё; новый запуск — со следующего дня, когда обвал кончился). Выход — худший капитал из
2 следующих минут минус расходы 0,1% капитала на единицу экспозиции (у Turbo с экспозицией 20 — 2%).
Периоды: II пол. 2025 (с обвалом 10.10.2025), I и II пол. 2026. Капитал поминутно (balance_sample_divider 1).

    venv\\Scripts\\python tb4\\main_strategy.py [--workers 6]
Итог: tb4/results/main_strategy.json и таблица в консоли.
"""
import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tb4"))
import make_config as MC  # noqa: E402

PB = os.path.join(ROOT, "venv", "Scripts", "passivbot.exe")
EMA = os.path.join(ROOT, "configs", "examples", "ema_anchor.json")
RES = os.path.join(ROOT, "tb4", "results")
BTC_CACHE = os.path.join(RES, "btc_1h_2025_2026.json")
BALANCE, DELAY = 300.0, 2
L = "bot.long.risk."
HSL = {"bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.35, "bot.long.hsl.restart_after_red_policy": "always",
       "bot.long.hsl.cooldown_minutes_after_red": 4320.0, "live.hsl_signal_mode": "unified"}
VARIANTS = {"trailing martingale «Осторожный + стоп» (сейчас), эксп. 1,5": (MC.BASE, {**HSL}, 1.5)}
for tw in (1.5, 2.5, 3.5, 5.0):
    VARIANTS[f"EMA Anchor, 7 монет, эксп. {tw:g}"] = (EMA, {**HSL, L + "n_positions": 7.0, L + "total_wallet_exposure_limit": tw,
                                                         "bot.short.risk.total_wallet_exposure_limit": 0.0}, tw)
PERIODS = {"II пол. 2025 (обвал 10.10)": ("2025-07-01", "2026-01-01"), "I пол. 2026": ("2026-01-01", "2026-07-01"),
           "II пол. 2026": ("2026-07-01", "2026-09-30")}


def btc_hourly():
    """Часовые закрытия BTC (Binance USDⓈ-M) с 15.06.2025: Series по времени открытия свечи."""
    if not os.path.exists(BTC_CACHE):
        import requests
        s = requests.Session()
        s.trust_env = False
        t = int(datetime(2025, 6, 15, tzinfo=timezone.utc).timestamp() * 1000)
        t_end = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
        rows = []
        while t < t_end:
            r = s.get("https://fapi.binance.com/fapi/v1/klines", params={"symbol": "BTCUSDT", "interval": "1h",
                                                                         "startTime": t, "limit": 1500}, timeout=20).json()
            if not r:
                break
            rows += [[int(x[0]), float(x[4])] for x in r]
            t = r[-1][0] + 3_600_000
        json.dump(rows, open(BTC_CACHE, "w"))
    rows = json.load(open(BTC_CACHE))
    return pd.Series([c for _, c in rows], index=pd.to_datetime([t for t, _ in rows], unit="ms"))


def crash_flags(btc, start, n):
    """Поминутный флаг «обвал BTC»: закрытие часа на 5% ниже максимума закрытий за 12 ч (известно в конце часа)."""
    flag = (btc / btc.rolling(12, min_periods=1).max() - 1) <= -0.05
    flag.index = flag.index + pd.Timedelta(hours=1)
    idx = pd.Timestamp(start) + pd.to_timedelta(np.arange(n), unit="min")
    flag = flag.astype(float)
    return flag.reindex(flag.index.union(idx)).ffill().reindex(idx).fillna(0.0).values > 0.5


def backtest(tag, base, over, start, end, balance):
    """Прогон passivbot → поминутный капитал (Series) или текст ошибки."""
    base_dir = f"backtests/tb4/ms_{tag}"
    coins = list(MC.default_coins())
    for _ in range(12):
        cfg = MC.build(start, end, balance, base_path=base, coins=coins,
                       overrides={"backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir,
                                  "backtest.balance_sample_divider": 1, **over})
        cfg.pop("_source", None)
        path = os.path.join(ROOT, "configs", "tb4", f"ms_{tag}.json")
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        p = subprocess.run([PB, "backtest", path, "-dp", "y"], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import re
        m = re.search(r"No exchange data found for coin (\w+)", p.stdout + p.stderr) if p.returncode else None
        if not m or m.group(1) not in coins:
            break
        coins.remove(m.group(1))
    runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*", "balance_and_equity.csv.gz")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        return "ERROR " + (p.stdout + p.stderr)[-300:]
    df = pd.read_csv(runs[-1])
    eq = pd.Series(df["usd_total_equity"].astype(float).values, index=pd.to_datetime(df.iloc[:, 0], utc=True).dt.tz_localize(None))
    shutil.rmtree(os.path.join(ROOT, base_dir), ignore_errors=True)
    return eq


def chain(key, period, btc, with_exit):
    """Капитал по минутам за период: без правил — один прогон; с выходом по BTC — прогоны между обвалами."""
    base, over, tw = VARIANTS[key]
    start, end = PERIODS[period]
    cost = 0.001 * tw
    d, cap, pieces, exits = pd.Timestamp(start), BALANCE, [], []
    t_end = pd.Timestamp(end)
    while d < t_end - pd.Timedelta(days=1):
        if with_exit and crash_flags(btc, d, 1)[0]:                 # обвал ещё идёт — бот не запускается
            d += pd.Timedelta(days=1)
            continue
        tag = f"{abs(hash((key, period, with_exit))) % 10**8}_{d:%Y%m%d}"
        eq = backtest(tag, base, over, d.strftime("%Y-%m-%d"), end, cap)
        if isinstance(eq, str):
            return eq
        i = None
        if with_exit:
            fl = crash_flags(btc, d, len(eq))
            hits = np.flatnonzero(fl)
            i = int(hits[0]) if hits.size else None
        if i is None:
            pieces.append(eq)
            break
        out = float(eq.iloc[i:i + DELAY + 1].min()) * (1 - cost)
        pieces.append(pd.concat([eq.iloc[:i], pd.Series([out], index=[eq.index[i]])]))
        exits.append(eq.index[i].strftime("%d.%m.%Y %H:%M"))
        cap = out
        nxt = (eq.index[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([out], index=[nxt]))               # без позиций до следующего запуска
        d = nxt
        if cap < 1:
            break
    curve = pd.concat(pieces)
    curve = curve[~curve.index.duplicated(keep="last")].sort_index()
    return curve, exits


def stats(curve):
    h = curve.resample("1h").last().ffill()
    ret = h.iloc[-1] / h.iloc[0] - 1
    dd = float((1 - h / h.cummax()).max())
    m30 = (h.shift(-30 * 24) / h).dropna()
    days = (h.index[-1] - h.index[0]).total_seconds() / 86400
    return {"ret": round(ret * 100, 1), "month": round(((1 + ret) ** (30 / days) - 1) * 100, 1) if ret > -1 else -100.0,
            "dd": round(dd * 100, 1), "worst30": round((float(m30.min()) - 1) * 100, 1) if len(m30) else None,
            "min": round((float(h.min()) / h.iloc[0] - 1) * 100, 1),
            "calmar": round(ret / dd, 2) if dd > 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    btc = btc_hourly()
    jobs = [(k, p, w) for k in VARIANTS for p in PERIODS for w in (False, True)]
    with ThreadPoolExecutor(a.workers) as ex:
        out = dict(zip(jobs, ex.map(lambda j: chain(j[0], j[1], btc, j[2]), jobs)))
    rows = []
    for k in VARIANTS:
        for w in (False, True):
            row = {"name": k, "btc_exit": w}
            for p in PERIODS:
                r = out[(k, p, w)]
                row[p] = {"error": r[:200]} if isinstance(r, str) else {**stats(r[0]), "exits": r[1]}
            rows.append(row)
    os.makedirs(RES, exist_ok=True)
    json.dump(rows, open(os.path.join(RES, "main_strategy.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Бот без остановок, прибыль в обороте, ${BALANCE:g}; доход за период / в месяц, худшая просадка, худшие 30 дней\n")
    for r in rows:
        print(f"{r['name'][:60]}{' + выход по BTC' if r['btc_exit'] else ''}")
        for p in PERIODS:
            x = r[p]
            print(f"   {p:<26} " + (x["error"][:120] if "error" in x else
                  f"{x['ret']:+7.1f}% ({x['month']:+5.1f}%/мес.), просадка {x['dd']:5.1f}%, худшие 30 дн. {x['worst30']:+6.1f}%"
                  + (f", выходов по BTC {len(x['exits'])}" if r["btc_exit"] else "")))


if __name__ == "__main__":
    main()
