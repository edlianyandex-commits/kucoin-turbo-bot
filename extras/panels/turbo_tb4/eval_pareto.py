"""Проверка кандидатов оптимизатора на данных, которых он не видел, и на всей истории.

    python tb4/eval_pareto.py [папка optimize_results/...]   (по умолчанию — последняя)
Для каждого кандидата: 300 дней целиком (03.12.2025–29.09.2026) и «новые данные» после подбора (31.07–29.09).
Для сравнения — пресеты панели. Результат: tb4/results/pareto_eval.json.
"""
import copy
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_config as MC  # noqa: E402
from run_tests import equity_stats, PASSIVBOT  # noqa: E402

BALANCE = 300.0
WINDOWS = {"full": ("2025-12-03", "2026-09-29"), "oos": ("2026-07-31", "2026-09-29")}
OUT = os.path.join(HERE, "results", "pareto_eval.json")
L = "bot.long.risk."


def run_cfg(cfg, tag, start, end):
    cfg = copy.deepcopy(cfg)
    bt = cfg["backtest"]
    bt.update(start_date=start, end_date=end, starting_balance=BALANCE, base_dir=f"backtests/tb4/eval/{tag}",
              filter_by_min_effective_cost=True)
    path = os.path.join(ROOT, "configs", "tb4", "eval", f"{tag}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run([PASSIVBOT, "backtest", path, "-dp", "y"], cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    runs = sorted(glob.glob(os.path.join(ROOT, bt["base_dir"], "*", "*")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        return {"error": (p.stdout + p.stderr)[-600:]}
    days = (time.mktime(time.strptime(end, "%Y-%m-%d")) - time.mktime(time.strptime(start, "%Y-%m-%d"))) / 86400
    st = equity_stats(os.path.join(runs[-1], "balance_and_equity.csv.gz"), days)
    an = json.load(open(os.path.join(runs[-1], "analysis.json"), encoding="utf-8"))
    st["completion"] = an.get("backtest_completion_ratio")
    return st


def params(cfg):
    lr = cfg["bot"]["long"]["risk"]
    e = cfg["bot"]["long"]["strategy"]["trailing_martingale"]["entry"]
    return {"exposure": lr["total_wallet_exposure_limit"], "positions": lr["n_positions"],
            "initial_pct": e["initial_qty_pct"], "ddf": e["double_down_factor"]}


def main():
    run_dir = sys.argv[1] if len(sys.argv) > 1 else max(glob.glob(os.path.join(ROOT, "optimize_results", "*")),
                                                         key=os.path.getmtime)
    cands = []
    for i, f in enumerate(sorted(glob.glob(os.path.join(run_dir, "pareto", "*.json"))), 1):
        cfg = json.load(open(f, encoding="utf-8"))
        obj = (cfg.get("metrics") or {}).get("objectives") or {}
        cands.append({"name": f"Кандидат {i}", "file": os.path.basename(f), "cfg": cfg, "train": obj})
    base = MC.build("2025-12-03", "2026-09-29", BALANCE)
    for name, over in (("Пресет «Осторожный» (автор)", {}), ("Пресет «Сбалансированный» (экспозиция 3)",
                                                              {L + "total_wallet_exposure_limit": 3.0})):
        c = copy.deepcopy(base)
        for k, v in over.items():
            MC.set_path(c, k, v)
        cands.append({"name": name, "file": None, "cfg": c, "train": {}})
    rows = []
    for i, c in enumerate(cands, 1):
        r = {"name": c["name"], "file": c["file"], "params": params(c["cfg"]), "train": c["train"]}
        for w, (a, b) in WINDOWS.items():
            r[w] = run_cfg(c["cfg"], f"c{i:02d}_{w}", a, b)
        rows.append(r)
        f_, o = r["full"], r["oos"]
        if "error" in f_ or "error" in o:
            print(f"{c['name']}: ошибка {f_.get('error') or o.get('error')}", flush=True)
            continue
        p = r["params"]
        print(f"{c['name']:<42} эксп {p['exposure']:<5} поз {p['positions']:<4} вход {p['initial_pct']:.4f} | "
              f"300д {f_['return_pct']:+7.1f}% просадка {f_['max_dd_pct']:5.1f}% худш.30д {f_['worst_30d_pct']:+6.1f}% | "
              f"НОВЫЕ {o['return_pct']:+6.1f}% просадка {o['max_dd_pct']:4.1f}%", flush=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
    print("Готово:", OUT)


if __name__ == "__main__":
    main()
