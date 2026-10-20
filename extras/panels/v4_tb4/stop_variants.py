"""Основной бот: варианты стопа по просадке на истории (настоящее ядро passivbot).

  venv\\Scripts\\python tb4\\stop_variants.py
Итог: tb4/results/stop_variants.json и таблица в консоли.
"""
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "panel"))
sys.path.insert(0, HERE)
import make_config as MC  # noqa: E402
from run_tests import equity_stats  # noqa: E402

PRESETS = json.load(open(os.path.join(HERE, "panel", "presets.json"), encoding="utf-8-sig"))
BASE_OVER = dict(PRESETS["careful_stop"]["overrides"])
H = "bot.long.hsl."
VARIANTS = {
    "сейчас (стоп −35%, сглаживание 12 ч)": {},
    "быстрый стоп (сглаживание 1 ч)": {H + "ema_span_minutes": 60.0},
    "ранний стоп −25%": {H + "red_threshold": 0.25},
    "быстрый и ранний (1 ч, −25%)": {H + "ema_span_minutes": 60.0, H + "red_threshold": 0.25},
}
PERIODS = {"2022–2026 подряд": ("2022-01-01", "2026-09-29"), "обвал февраля 2025": ("2025-01-01", "2025-02-20")}
MISSING = re.compile(r"No exchange data found for coin (\w+)")
PASSIVBOT = os.path.join(ROOT, "venv", "Scripts", "passivbot.exe")


def run(tag, start, end, over):
    base_dir = f"backtests/tb4/stops/{tag}"
    coins = MC.default_coins()
    for _ in range(10):
        cfg = MC.build(start, end, 300.0, overrides={"backtest.filter_by_min_effective_cost": True,
                                                     "backtest.base_dir": base_dir, **BASE_OVER, **over}, coins=coins)
        cfg.pop("_source", None)
        path = os.path.join(ROOT, "configs", "tb4", f"stops_{tag}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(cfg, open(path, "w", encoding="utf-8"))
        before = set(glob.glob(os.path.join(ROOT, base_dir, "*", "*")))
        p = subprocess.run([PASSIVBOT, "backtest", path, "-dp", "y"], cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        runs = sorted(set(glob.glob(os.path.join(ROOT, base_dir, "*", "*"))) - before, key=os.path.getmtime)
        if p.returncode == 0 and runs:
            break
        m = MISSING.search(p.stdout + p.stderr)
        if not m:
            raise RuntimeError((p.stdout + p.stderr)[-1500:])
        coins = [c for c in coins if c != m.group(1)]
    run_dir = runs[-1]
    days = (MC_date(end) - MC_date(start)).days
    st = equity_stats(os.path.join(run_dir, "balance_and_equity.csv.gz"), days)
    fills = open(os.path.join(run_dir, "fills.csv"), encoding="utf-8").read()
    st["stops"] = len(set(line.split(",")[2][:10] for line in fills.splitlines() if "panic" in line))
    return st


def MC_date(s):
    from datetime import datetime
    return datetime.fromisoformat(s)


def main():
    out, res_path = {}, os.path.join(HERE, "results", "stop_variants.json")
    for pname, (start, end) in PERIODS.items():
        for i, (vname, over) in enumerate(VARIANTS.items()):
            tag = f"{'full' if '2022' in pname else 'feb25'}_{i}"
            st = run(tag, start, end, over)
            out[f"{pname} | {vname}"] = st
            json.dump(out, open(res_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"{pname:<20} | {vname:<38} | итог {st['return_pct']:+8.1f}% | в месяц {st['month_pct']:+5.1f}% | "
                  f"худшая просадка {st['max_dd_pct']:5.1f}% | худшие 30 дн. {st['worst_30d_pct']:+6.1f}% | "
                  f"30-дневок в плюсе {st['share_30d_positive']:5.1f}% | дней со стопом {st['stops']}", flush=True)
    print("Готово:", res_path)


if __name__ == "__main__":
    main()
