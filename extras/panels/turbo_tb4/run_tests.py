"""Прогон набора настроек passivbot на 160 и 300 днях и сводка под цель «+100% за 30 дней».

    python tb4/run_tests.py            все варианты из VARIANTS
    python tb4/run_tests.py --only 1,3 только выбранные номера
Результаты: tb4/results/tests.json и таблица в консоли.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_config as MC  # noqa: E402

END = "2026-09-29"
PERIODS = {160: "2026-04-22", 300: "2025-12-03"}
BALANCE = 10000.0   # при $1000 настройки по умолчанию не проходят минимальный ордер KuCoin (см. варианты «счёт $1000»)
PASSIVBOT = os.path.join(ROOT, "venv", "Scripts", "passivbot.exe")
OUT_DIR = os.path.join(HERE, "results")
TARGET = 1.0   # +100% за 30 дней

L = "bot.long.risk."
COMMON = {"backtest.filter_by_min_effective_cost": True}   # как в живой торговле: монеты дороже минимального ордера не берутся
VARIANTS = [
    ("Автор passivbot: лонг, экспозиция 1,5, 7 позиций", {}),
    ("Экспозиция 3", {L + "total_wallet_exposure_limit": 3.0}),
    ("Экспозиция 5", {L + "total_wallet_exposure_limit": 5.0}),
    ("Экспозиция 8", {L + "total_wallet_exposure_limit": 8.0}),
    ("Экспозиция 3, 3 позиции", {L + "total_wallet_exposure_limit": 3.0, L + "n_positions": 3.0}),
    ("Экспозиция 5 + аварийный стоп капитала 25%", {L + "total_wallet_exposure_limit": 5.0,
                                                    "bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.25}),
    ("Малый счёт: настройки автора", {"_balance": 300.0}),
    ("Малый счёт: экспозиция 3", {"_balance": 300.0, L + "total_wallet_exposure_limit": 3.0}),
    ("Малый счёт: экспозиция 5 + аварийный стоп 25%", {"_balance": 300.0, L + "total_wallet_exposure_limit": 5.0,
                                                     "bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.25}),
    ("Малый счёт: экспозиция 3, 4 позиции", {"_balance": 300.0, L + "total_wallet_exposure_limit": 3.0,
                                           L + "n_positions": 4.0}),
    ("Малый счёт: экспозиция 5", {"_balance": 300.0, L + "total_wallet_exposure_limit": 5.0}),
    ("Малый счёт: экспозиция 2", {"_balance": 300.0, L + "total_wallet_exposure_limit": 2.0}),
    ("Малый счёт: экспозиция 3 + аварийный стоп 25%", {"_balance": 300.0, L + "total_wallet_exposure_limit": 3.0,
                                                     "bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.25}),
]


def equity_stats(path, days):
    df = pd.read_csv(path)
    df["t"] = pd.to_datetime(df.iloc[:, 0])
    eq = df.set_index("t")["usd_total_equity"].astype(float)
    start, end = eq.iloc[0], eq.iloc[-1]
    peak = eq.cummax()
    dd = float(((peak - eq) / peak).max())
    win = 30 * 24   # почасовые точки
    r30 = (eq.shift(-win) / eq - 1).dropna() if len(eq) > win else pd.Series(dtype=float)
    monthly = {}
    for m, g in eq.groupby(eq.index.to_period("M")):
        monthly[str(m)] = round((g.iloc[-1] / g.iloc[0] - 1) * 100, 1)
    return {
        "return_pct": round((end / start - 1) * 100, 1),
        "month_pct": round(((end / start) ** (30 / days) - 1) * 100, 1) if end > 0 else -100.0,
        "max_dd_pct": round(dd * 100, 1),
        "min_equity_pct": round((eq.min() / start - 1) * 100, 1),
        "best_30d_pct": round(float(r30.max()) * 100, 1) if len(r30) else None,
        "worst_30d_pct": round(float(r30.min()) * 100, 1) if len(r30) else None,
        "median_30d_pct": round(float(r30.median()) * 100, 1) if len(r30) else None,
        "share_30d_target": round(float((r30 >= TARGET).mean()) * 100, 1) if len(r30) else None,
        "share_30d_positive": round(float((r30 > 0).mean()) * 100, 1) if len(r30) else None,
        "monthly": monthly,
    }


def run_one(idx, name, over, days):
    start = PERIODS[days]
    tag = f"v{idx:02d}_{days}d"
    base_dir = f"backtests/tb4/{tag}"
    over = dict(over)
    balance = over.pop("_balance", BALANCE)
    cfg = MC.build(start, END, balance, overrides={**COMMON, **over, "backtest.base_dir": base_dir})
    os.makedirs(os.path.join(ROOT, "configs", "tb4"), exist_ok=True)
    cfg_path = os.path.join(ROOT, "configs", "tb4", f"{tag}.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    t0 = time.time()
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run([PASSIVBOT, "backtest", cfg_path, "-dp", "y"], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        tail = (p.stdout + p.stderr)[-1500:]
        print(f"  !! ошибка {tag}:\n{tail}", flush=True)
        return {"idx": idx, "name": name, "days": days, "error": tail}
    run_dir = runs[-1]
    with open(os.path.join(run_dir, "analysis.json"), encoding="utf-8") as f:
        an = json.load(f)
    st = equity_stats(os.path.join(run_dir, "balance_and_equity.csv.gz"), days)
    st.update(idx=idx, name=name, days=days, balance=balance, trades=int(an.get("n_fills", 0) or 0) or None, run_dir=os.path.relpath(run_dir, ROOT), seconds=round(time.time() - t0),
              adg_pct=round(an.get("adg_strategy_eq", 0) * 100, 3),
              completion=an.get("backtest_completion_ratio"),
              held_days_max=round(an.get("position_held_days_max", 0), 1),
              loss_profit_ratio=round(an.get("loss_profit_ratio", 0), 3))
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--days", default="300,160")
    a = ap.parse_args()
    only = {int(x) for x in a.only.split(",")} if a.only else None
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "tests.json")
    results = json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else []
    for days in (int(x) for x in a.days.split(",")):
        for i, (name, over) in enumerate(VARIANTS, 1):
            if only and i not in only:
                continue
            print(f"[{days} дн.] №{i} {name} …", flush=True)
            r = run_one(i, name, over, days)
            results = [x for x in results if not (x["idx"] == i and x["days"] == days)] + [r]
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1)
            if "error" not in r:
                print(f"   итог {r['return_pct']:+.1f}% | в месяц {r['month_pct']:+.1f}% | просадка {r['max_dd_pct']:.1f}% | "
                      f"30 дней: лучшие {r['best_30d_pct']:+.1f}%, худшие {r['worst_30d_pct']:+.1f}%, "
                      f"окон с +100%: {r['share_30d_target']:.0f}% | доработал до конца: {r['completion']} "
                      f"({r['seconds']} с)", flush=True)
    print("\nГотово:", out_path)


if __name__ == "__main__":
    main()
