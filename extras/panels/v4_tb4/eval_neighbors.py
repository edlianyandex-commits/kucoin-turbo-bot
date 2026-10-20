"""Устойчивость кандидатов: сдвигаем важные параметры на ±20% (позиции — на +1/+2) и смотрим, не разваливается ли результат.

    python tb4/eval_neighbors.py 19 18 13      номера кандидатов из tb4/results/pareto_eval.json
"""
import copy
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from eval_pareto import run_cfg, WINDOWS  # noqa: E402

OUT = os.path.join(HERE, "results", "neighbors_eval.json")
E = ("bot", "long", "strategy", "trailing_martingale", "entry")
R = ("bot", "long", "risk")
SHIFTS = [
    ("экспозиция −20%", R, "total_wallet_exposure_limit", lambda v: round(v * 0.8, 3)),
    ("экспозиция +20%", R, "total_wallet_exposure_limit", lambda v: round(v * 1.2, 3)),
    ("позиций +1", R, "n_positions", lambda v: v + 1),
    ("позиций +2", R, "n_positions", lambda v: v + 2),
    ("первый вход −20%", E, "initial_qty_pct", lambda v: round(v * 0.8, 5)),
    ("первый вход +20%", E, "initial_qty_pct", lambda v: round(v * 1.2, 5)),
    ("докупка ×0,8", E, "double_down_factor", lambda v: round(v * 0.8, 3)),
    ("докупка ×1,2", E, "double_down_factor", lambda v: round(v * 1.2, 3)),
    ("порог докупки −20%", E, "threshold_base_pct", lambda v: round(v * 0.8, 5)),
    ("порог докупки +20%", E, "threshold_base_pct", lambda v: round(v * 1.2, 5)),
]


def node(cfg, path):
    for k in path:
        cfg = cfg[k]
    return cfg


def main():
    ev = json.load(open(os.path.join(HERE, "results", "pareto_eval.json"), encoding="utf-8"))
    run_dir = max(glob.glob(os.path.join(ROOT, "optimize_results", "*")), key=os.path.getmtime)
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    for num in sys.argv[1:]:
        row = next(r for r in ev if r["name"] == f"Кандидат {num}")
        base = json.load(open(os.path.join(run_dir, "pareto", row["file"]), encoding="utf-8"))
        res = []
        print(f"--- Кандидат {num}: 300д {row['full']['return_pct']:+.1f}% / {row['full']['max_dd_pct']:.1f}%, "
              f"новые {row['oos']['return_pct']:+.1f}%", flush=True)
        for j, (label, path, key, fn) in enumerate(SHIFTS, 1):
            cfg = copy.deepcopy(base)
            n = node(cfg, path)
            n[key] = fn(n[key])
            r = {"label": label, "value": n[key]}
            for w, (a, b) in WINDOWS.items():
                r[w] = run_cfg(cfg, f"n{num}_{j:02d}_{w}", a, b)
            res.append(r)
            f_, o = r["full"], r["oos"]
            if "error" in f_ or "error" in o:
                print(f"  {label}: ошибка", flush=True)
                continue
            print(f"  {label:<20} ({key}={n[key]}) 300д {f_['return_pct']:+7.1f}% просадка {f_['max_dd_pct']:5.1f}% | "
                  f"новые {o['return_pct']:+6.1f}% просадка {o['max_dd_pct']:4.1f}%", flush=True)
        out[num] = {"base": row, "neighbors": res}
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
