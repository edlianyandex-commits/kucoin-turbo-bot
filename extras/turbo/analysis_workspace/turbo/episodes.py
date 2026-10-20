"""Турбо: шансы удвоить ставку за 30 дней.

Каждый вариант настроек прогоняется на отдельных 30-дневных отрезках истории (старт каждые STEP_DAYS дней),
каждый раз с чистого счёта STAKE. Правило ставки: как только капитал достиг +100% — фиксируем удвоение и выходим;
если потеряно 90% и больше — ставка сгорела; иначе — результат на конец отрезка.

    python turbo/episodes.py [--only 1,2] [--workers 4]
Результат: turbo/results/episodes.json и таблица в консоли.
"""
import argparse
import copy
import glob
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tb4"))
import make_config as MC  # noqa: E402

PASSIVBOT = os.path.join(ROOT, "venv", "Scripts", "passivbot.exe")
STAKE = 200.0
FIRST, LAST_START, STEP_DAYS, WINDOW = "2025-12-03", "2026-08-30", 10, 30
TARGET, BURN = 2.0, 0.10          # удвоение / остаток 10% и меньше = сгорела
OUT = os.path.join(HERE, "results", "episodes.json")
TUNED_GROWTH = os.path.join(HERE, "results", "tuned", "tuned_growth.json")      # из панели убраны 02.10.2026
TUNED_MODERATE = os.path.join(HERE, "results", "tuned", "tuned_moderate.json")
L = "bot.long.risk."
HSL = {"bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.5,
       "bot.long.hsl.restart_after_red_policy": "never", "live.hsl_signal_mode": "unified"}   # стоп по всему счёту

# (название, базовый конфиг, правки)
VARIANTS = [
    ("Автор, экспозиция 5, 7 монет", MC.BASE, {L + "total_wallet_exposure_limit": 5.0}),
    ("Автор, экспозиция 8, 7 монет", MC.BASE, {L + "total_wallet_exposure_limit": 8.0}),
    ("Автор, экспозиция 12, 7 монет", MC.BASE, {L + "total_wallet_exposure_limit": 12.0}),
    ("Автор, экспозиция 8, 3 монеты", MC.BASE, {L + "total_wallet_exposure_limit": 8.0, L + "n_positions": 3.0}),
    ("Автор, экспозиция 12, 3 монеты", MC.BASE, {L + "total_wallet_exposure_limit": 12.0, L + "n_positions": 3.0}),
    ("Автор, экспозиция 20, 3 монеты", MC.BASE, {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0}),
    ("Подобранный доходный ×3 (экспозиция 3,6)", TUNED_GROWTH, {L + "total_wallet_exposure_limit": 3.6}),
    ("Подобранный доходный ×5 (экспозиция 6)", TUNED_GROWTH, {L + "total_wallet_exposure_limit": 6.0}),
    ("Подобранный доходный ×8 (экспозиция 9,6)", TUNED_GROWTH, {L + "total_wallet_exposure_limit": 9.6}),
    ("Подобранный умеренный ×5 (экспозиция 4,75)", TUNED_MODERATE, {L + "total_wallet_exposure_limit": 4.75}),
    ("Подобранный умеренный ×10 (экспозиция 9,5)", TUNED_MODERATE, {L + "total_wallet_exposure_limit": 9.5}),
    ("Подобранный доходный ×5 + стоп −50%", TUNED_GROWTH, {L + "total_wallet_exposure_limit": 6.0, **HSL}),
    ("Автор, экспозиция 12, 3 монеты + стоп −50%", MC.BASE,
     {L + "total_wallet_exposure_limit": 12.0, L + "n_positions": 3.0, **HSL}),
    ("Автор, экспозиция 20, 3 монеты + стоп −50%", MC.BASE,
     {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0, **HSL}),
    ("Автор, экспозиция 20, 3 монеты + стоп −40%", MC.BASE,
     {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0, **HSL, "bot.long.hsl.red_threshold": 0.4}),
    ("Автор, экспозиция 16, 3 монеты + стоп −50%", MC.BASE,
     {L + "total_wallet_exposure_limit": 16.0, L + "n_positions": 3.0, **HSL}),
    ("Автор, экспозиция 12, 3 монеты + стоп −40%", MC.BASE,
     {L + "total_wallet_exposure_limit": 12.0, L + "n_positions": 3.0, **HSL, "bot.long.hsl.red_threshold": 0.4}),
    # защита от обвала (turbo/risk_rules.py): быстрый стоп и потолок на одну монету
    ("Экспозиция 20, 3 монеты + быстрый стоп −40% (сглаживание 1 ч)", MC.BASE,
     {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0, **HSL, "bot.long.hsl.red_threshold": 0.4,
      "bot.long.hsl.ema_span_minutes": 60.0}),
    ("Экспозиция 20, 3 монеты + стоп −40% + потолок на монету", MC.BASE,
     {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0, **HSL, "bot.long.hsl.red_threshold": 0.4,
      L + "we_excess_allowance_pct": 0.0}),
    ("Экспозиция 20, 3 монеты + быстрый стоп −40% + потолок на монету", MC.BASE,
     {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0, **HSL, "bot.long.hsl.red_threshold": 0.4,
      "bot.long.hsl.ema_span_minutes": 60.0, L + "we_excess_allowance_pct": 0.0}),
]


def windows(first=None, last_start=None):
    d = datetime.fromisoformat(first or FIRST)
    last = datetime.fromisoformat(last_start or LAST_START)
    out = []
    while d <= last:
        out.append((d.strftime("%Y-%m-%d"), (d + timedelta(days=WINDOW)).strftime("%Y-%m-%d")))
        d += timedelta(days=STEP_DAYS)
    return out


# монета без данных: нет на бирже свечей вовсе — или (снятые с торгов, 03.10.2026) свечей нет на Binance
MISSING_COIN = re.compile(r"(?:No exchange data found for coin|Forced exchange \S+ failed for coin) (\w+)")


def episode(vi, base, over, start, end):
    tag = f"v{vi:02d}_{start}"   # дата старта в имени — отрезки разных периодов не пересекаются
    base_dir = f"backtests/turbo/{tag}"
    coins, skipped = MC.default_coins(), []
    for _ in range(12):   # монеты, которых на тот момент ещё не было на бирже, убираем (живой бот их бы не увидел)
        cfg = MC.build(start, end, STAKE, base_path=base, coins=coins,
                       overrides={"backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir, **over})
        cfg.pop("_source", None)
        path = os.path.join(ROOT, "configs", "turbo", f"{tag}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        # отдельная группа процессов без консоли: завершение одного прогона не задевает остальные и сам скрипт
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run([PASSIVBOT, "backtest", path, "-dp", "y"], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags,
                           stdin=subprocess.DEVNULL)
        m = MISSING_COIN.search(p.stdout + p.stderr) if p.returncode != 0 else None
        if not m or m.group(1) not in coins:
            break
        coins = [c for c in coins if c != m.group(1)]
        skipped.append(m.group(1))
    runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        return {"start": start, "error": (p.stdout + p.stderr)[-400:], "skipped": skipped}
    df = pd.read_csv(os.path.join(runs[-1], "balance_and_equity.csv.gz"))
    eq = df["usd_total_equity"].astype(float).values
    e0 = eq[0]
    peak, dd = e0, 0.0
    for i, e in enumerate(eq):
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak if peak > 0 else 1.0)
        if e >= TARGET * e0:
            return {"start": start, "outcome": "double", "result_pct": 100.0, "days": round(i / 24, 1),
                    "max_dd_pct": round(dd * 100, 1)}
        if e <= BURN * e0:
            return {"start": start, "outcome": "burn", "result_pct": round((e / e0 - 1) * 100, 1),
                    "days": round(i / 24, 1), "max_dd_pct": round(dd * 100, 1)}
    return {"start": start, "outcome": "other", "result_pct": round((eq[-1] / e0 - 1) * 100, 1), "days": WINDOW,
            "max_dd_pct": round(dd * 100, 1)}


def summarize(name, eps):
    ok = [e for e in eps if "outcome" in e]
    n = len(ok) or 1
    res = [e["result_pct"] for e in ok]
    s = pd.Series(res) if res else pd.Series([0.0])
    return {"name": name, "episodes": len(ok), "errors": len(eps) - len(ok),
            "p_double": round(sum(e["outcome"] == "double" for e in ok) / n * 100, 1),
            "p_burn": round(sum(e["outcome"] == "burn" for e in ok) / n * 100, 1),
            "p_loss50": round(sum(e["result_pct"] <= -50 for e in ok) / n * 100, 1),
            "p_positive": round(sum(e["result_pct"] > 0 for e in ok) / n * 100, 1),
            "mean_pct": round(float(s.mean()), 1), "median_pct": round(float(s.median()), 1),
            "worst_pct": round(float(s.min()), 1),
            "days_to_double_median": (round(float(pd.Series([e["days"] for e in ok if e["outcome"] == "double"]).median()), 1)
                                      if any(e["outcome"] == "double" for e in ok) else None),
            "episodes_detail": eps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--first", default=FIRST, help="старт первого отрезка")
    ap.add_argument("--last-start", default=LAST_START, help="старт последнего отрезка")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    only = {int(x) for x in a.only.split(",")} if a.only else None
    out_path = a.out
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    results = json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else {}
    wins = windows(a.first, a.last_start)
    print(f"Отрезков по {WINDOW} дней: {len(wins)} (старт {wins[0][0]} … {wins[-1][0]}), ставка ${STAKE:.0f}", flush=True)
    for vi, (name, base, over) in enumerate(VARIANTS, 1):
        if only and vi not in only:
            continue
        with ThreadPoolExecutor(a.workers) as ex:
            eps = list(ex.map(lambda w: episode(vi, base, over, *w), wins))
        s = summarize(name, eps)
        results[str(vi)] = s
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print(f"№{vi:<2} {name:<46} удвоил {s['p_double']:5.1f}% | сгорел {s['p_burn']:5.1f}% | −50% и хуже {s['p_loss50']:5.1f}% | "
              f"в плюсе {s['p_positive']:5.1f}% | средний итог {s['mean_pct']:+6.1f}% | медиана {s['median_pct']:+6.1f}% | "
              f"худший {s['worst_pct']:+6.1f}% | ошибок {s['errors']}", flush=True)
    print("Готово:", out_path)


if __name__ == "__main__":
    main()
