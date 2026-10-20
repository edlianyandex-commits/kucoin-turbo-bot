"""Основной бот по годам: 2022 (медвежий рынок, крах FTX), 2023, 2024, 2025, 2026 и сплошной прогон 2022–2026.
Ищем самую доходную экспозицию, которая пережила все годы без ликвидации.

    python tb4/multi_year.py [--workers 4] [--only 1,2]
Результат: tb4/results/multi_year.json и таблица в консоли.
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
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_config as MC  # noqa: E402
from run_tests import equity_stats, PASSIVBOT  # noqa: E402

BALANCE = 300.0
OUT = os.path.join(HERE, "results", "multi_year.json")
PERIODS = [("2022", "2022-01-01", "2022-12-31"), ("2023", "2023-01-01", "2023-12-31"),
           ("2024", "2024-01-01", "2024-12-31"), ("2025", "2025-01-01", "2025-12-31"),
           ("2026", "2026-01-01", "2026-09-29"), ("2022–2026 подряд", "2022-01-01", "2026-09-29")]
L = "bot.long.risk."
S = "bot.short."
MISSING = re.compile(r"No exchange data found for coin (\w+)")


def mirror_short(twel):
    """Шорт с зеркальными настройками лонга автора (не оптимизировались под шорт)."""
    base = json.load(open(MC.BASE, encoding="utf-8"))
    lb = base["bot"]["long"]
    over = {}
    for sect in ("strategy", "unstuck", "forager"):
        over[S + sect] = copy.deepcopy(lb[sect])
    risk = copy.deepcopy(lb["risk"])
    risk["total_wallet_exposure_limit"] = twel
    over[S + "risk"] = risk
    return over


VARIANTS = [
    ("Осторожный (автор): экспозиция 1,5", {}),
    ("Экспозиция 2", {L + "total_wallet_exposure_limit": 2.0}),
    ("Экспозиция 2,5", {L + "total_wallet_exposure_limit": 2.5}),
    ("Экспозиция 3", {L + "total_wallet_exposure_limit": 3.0}),
    ("Лонг 1,5 + шорт 0,75 (зеркальный)", mirror_short(0.75)),
    ("Лонг 2 + шорт 1 (зеркальный)", {L + "total_wallet_exposure_limit": 2.0, **mirror_short(1.0)}),
]


def set_over(cfg, over):
    for k, v in over.items():
        MC.set_path(cfg, k, copy.deepcopy(v))


def run(vi, over, label, start, end):
    tag = f"v{vi}_{start[:4]}_{end[:4]}_{'full' if 'подряд' in label else 'y'}"
    base_dir = f"backtests/tb4/years/{tag}"
    coins = MC.default_coins()
    skipped = []
    for _ in range(25):
        cfg = MC.build(start, end, BALANCE, coins=coins,
                       overrides={"backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir})
        set_over(cfg, over)
        path = os.path.join(ROOT, "configs", "tb4", "years", f"{tag}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run([PASSIVBOT, "backtest", path, "-dp", "y"], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags,
                           stdin=subprocess.DEVNULL)
        m = MISSING.search(p.stdout + p.stderr) if p.returncode != 0 else None
        if not m or m.group(1) not in coins:
            break
        coins = [c for c in coins if c != m.group(1)]   # монеты ещё не было на бирже — живой бот её бы не увидел
        skipped.append(m.group(1))
    runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        return {"period": label, "error": (p.stdout + p.stderr)[-500:], "skipped": skipped}
    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    st = equity_stats(os.path.join(runs[-1], "balance_and_equity.csv.gz"), days)
    an = json.load(open(os.path.join(runs[-1], "analysis.json"), encoding="utf-8"))
    st.update(period=label, start=start, end=end, completion=an.get("backtest_completion_ratio"),
              coins_used=len(coins), skipped=skipped, run_dir=runs[-1],
              liquidated=bool((an.get("backtest_completion_ratio") is not None
                              and an.get("backtest_completion_ratio") < 0.999) or st["min_equity_pct"] <= -90))
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only")
    a = ap.parse_args()
    only = {int(x) for x in a.only.split(",")} if a.only else None
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    results = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    for vi, (name, over) in enumerate(VARIANTS, 1):
        if only and vi not in only:
            continue
        with ThreadPoolExecutor(a.workers) as ex:
            rows = list(ex.map(lambda pr: run(vi, over, *pr), PERIODS))
        results[str(vi)] = {"name": name, "rows": rows}
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print(f"№{vi} {name}", flush=True)
        for r in rows:
            if "error" in r:
                print(f"   {r['period']:<18} ошибка: {r['error'][-200:]}", flush=True)
                continue
            print(f"   {r['period']:<18} {r['return_pct']:+9.1f}% | в месяц {r['month_pct']:+6.1f}% | просадка {r['max_dd_pct']:5.1f}% | "
                  f"худшие 30д {r['worst_30d_pct']:+6.1f}% | {'ЛИКВИДАЦИЯ' if r['liquidated'] else 'выжил'} | "
                  f"монет {r['coins_used']}", flush=True)
    print("Готово:", OUT)


if __name__ == "__main__":
    main()
