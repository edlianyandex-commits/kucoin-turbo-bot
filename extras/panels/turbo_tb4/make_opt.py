"""Конфиг оптимизатора passivbot под маленький счёт на KuCoin Futures.

    python tb4/make_opt.py [--balance 300] [--iters 4000] [--out configs/tb4/opt_small.json]
Подбор — на старой части истории (TRAIN_START..TRAIN_END); последние месяцы оставлены для проверки.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_config as MC  # noqa: E402

TRAIN_START, TRAIN_END = "2025-12-03", "2026-07-31"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=300.0)
    ap.add_argument("--iters", type=int, default=4000)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "configs", "tb4", "opt_small.json"))
    a = ap.parse_args()
    cfg = MC.build(TRAIN_START, TRAIN_END, a.balance, overrides={
        "backtest.filter_by_min_effective_cost": True, "backtest.base_dir": "backtests/tb4/opt"})
    o = cfg["optimize"]
    o["n_cpus"] = 10
    o["iters"] = a.iters
    o["scoring"] = [{"metric": "adg_strategy_eq", "goal": "max"},
                    {"metric": "drawdown_worst_strategy_eq", "goal": "min"}]
    o["limits"] = [
        {"metric": "drawdown_worst_strategy_eq", "penalize_if": "greater_than", "value": 0.35},
        {"metric": "backtest_completion_ratio", "penalize_if": "less_than", "value": 1.0},
        {"metric": "adg_strategy_eq", "penalize_if": "less_than", "value": 0.0002},   # не «ни одной сделки»
    ]
    risk = o["bounds"]["long"]["risk"]
    risk["n_positions"] = [3, 8, 1]
    risk["total_wallet_exposure_limit"] = [0.8, 3.0, 0.05]
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("записан", a.out, "монет:", len(cfg["live"]["approved_coins"]["long"]))


if __name__ == "__main__":
    main()
