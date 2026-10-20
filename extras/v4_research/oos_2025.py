"""Проверка пресетов панели на 2025 годе — периоде, которого наш оптимизатор не видел.

    python tb4/oos_2025.py [--start 2025-03-01] [--end 2025-12-01] [--balance 300]
Результат: tb4/results/oos_2025.json и таблица в консоли.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_config as MC  # noqa: E402
from run_tests import equity_stats, PASSIVBOT  # noqa: E402

PANEL = os.path.join(HERE, "panel")
OUT = os.path.join(HERE, "results", "oos_2025.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-03-01")
    ap.add_argument("--end", default="2025-12-01")
    ap.add_argument("--balance", type=float, default=300.0)
    a = ap.parse_args()
    presets = {k: v for k, v in json.load(open(os.path.join(PANEL, "presets.json"), encoding="utf-8")).items()
               if not k.startswith("_")}
    days = (datetime.fromisoformat(a.end) - datetime.fromisoformat(a.start)).days
    rows = []
    for key, p in presets.items():
        base = os.path.join(PANEL, p["base"]) if p.get("base") else MC.BASE
        base_dir = f"backtests/tb4/oos2025/{key}"
        cfg = MC.build(a.start, a.end, a.balance, base_path=base, overrides={
            "backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir, **(p.get("overrides") or {})})
        cfg.pop("_source", None)
        path = os.path.join(ROOT, "configs", "tb4", f"oos2025_{key}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        pr = subprocess.run([PASSIVBOT, "backtest", path, "-dp", "y"], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags,
                            stdin=subprocess.DEVNULL)
        runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*")), key=os.path.getmtime)
        if pr.returncode != 0 or not runs:
            print(f"{p['name']}: ошибка\n{(pr.stdout + pr.stderr)[-800:]}", flush=True)
            rows.append({"key": key, "name": p["name"], "error": (pr.stdout + pr.stderr)[-800:]})
            continue
        st = equity_stats(os.path.join(runs[-1], "balance_and_equity.csv.gz"), days)
        an = json.load(open(os.path.join(runs[-1], "analysis.json"), encoding="utf-8"))
        st.update(key=key, name=p["name"], completion=an.get("backtest_completion_ratio"), run_dir=runs[-1])
        rows.append(st)
        print(f"{p['name']:<28} {st['return_pct']:+8.1f}% | в месяц {st['month_pct']:+6.1f}% | просадка {st['max_dd_pct']:5.1f}% | "
              f"30 дней: худшие {st['worst_30d_pct']:+6.1f}%, лучшие {st['best_30d_pct']:+6.1f}%, в плюсе {st['share_30d_positive']:.0f}%, "
              f"с +100%: {st['share_30d_target']:.0f}% | до конца: {st['completion']}", flush=True)
        print("    месяцы: " + " ".join(f"{k[2:]}:{v:+.0f}" for k, v in st["monthly"].items()), flush=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
    print("Готово:", OUT)


if __name__ == "__main__":
    main()
