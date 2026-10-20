"""TradeBot v4: стратегия EMA Anchor (вторая стратегия движка passivbot) против нынешней trailing martingale.

Счёт $300, правила KuCoin, только лонг, стоп по просадке 35% как у «Осторожный + стоп». Параметры EMA Anchor —
пример автора passivbot (configs/examples/ema_anchor.json, подобран для SOL), меняются только монеты и экспозиция.

    venv\\Scripts\\python tb4\\ema_anchor_test.py [--windows 2026-07-01:2026-09-29:II пол.,…] [--only 0,6]
Без --windows — обе половины 2026 года.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tb4"))
import make_config as MC  # noqa: E402

PB = os.path.join(ROOT, "venv", "Scripts", "passivbot.exe")
EMA = os.path.join(ROOT, "configs", "examples", "ema_anchor.json")
HSL = {"bot.long.hsl.enabled": True, "bot.long.hsl.red_threshold": 0.35, "bot.long.hsl.restart_after_red_policy": "always",
       "bot.long.hsl.cooldown_minutes_after_red": 4320.0, "live.hsl_signal_mode": "unified"}
L = "bot.long.risk."
WINDOWS = "2026-07-01:2026-09-29:II пол. 2026,2026-01-01:2026-06-30:I пол. 2026"
VARIANTS = {"Осторожный + стоп (trailing martingale, сейчас)": (MC.BASE, {**HSL}, None)}
for n in (3, 7):
    for twel in (1.5, 2.5, 3.5, 5.0):
        VARIANTS[f"EMA Anchor: лонг, {n} монет, эксп. {twel:g}, стоп 35%"] = (EMA, {**HSL, L + "n_positions": float(n),
            L + "total_wallet_exposure_limit": twel, "bot.short.risk.total_wallet_exposure_limit": 0.0}, None)


def run(tag, base, over, coins, start, end):
    base_dir = f"backtests/tb4/ema_{tag}"
    cfg = MC.build(start, end, 300.0, base_path=base, coins=coins,
                   overrides={"backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir, **over})
    cfg.pop("_source", None)
    path = os.path.join(ROOT, "configs", "tb4", f"ema_{tag}.json")
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    p = subprocess.run([PB, "backtest", path, "-dp", "y"], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*", "balance_and_equity.csv.gz")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        return {"error": (p.stdout + p.stderr)[-300:]}
    eq = pd.read_csv(runs[-1])["usd_total_equity"].astype(float)
    fills = pd.read_csv(os.path.join(os.path.dirname(runs[-1]), "fills.csv"))
    an = json.load(open(os.path.join(os.path.dirname(runs[-1]), "analysis.json"), encoding="utf-8"))
    dd = float((1 - eq / eq.cummax()).max())
    cr = an.get("backtest_completion_ratio")
    return {"ret": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 1), "dd": round(dd * 100, 1), "fills": len(fills),
            "coins": int(fills["coin"].nunique()) if len(fills) else 0, "min": round((eq.min() / eq.iloc[0] - 1) * 100, 1),
            "stops": int((fills["type"] == "close_panic_long").sum()) if len(fills) else 0,
            "liquidated": bool(cr is not None and cr < 0.999)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default=WINDOWS)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = {int(x) for x in a.only.split(",") if x}
    wins = [w.split(":", 2) for w in a.windows.split(",")]
    for i, (name, (base, over, coins)) in enumerate(VARIANTS.items()):
        if only and i not in only:
            continue
        for start, end, lab in wins:
            r = run(f"{i}_{start}_{end}", base, over, coins, start, end)
            print(f"{i:>2} {name:<50} {lab}: " + (
                f"итог {r['ret']:+6.1f}% | худшая просадка {r['dd']:5.1f}% | минимум {r['min']:+6.1f}% | сделок {r['fills']} | "
                f"монет {r['coins']} | аварийных закрытий {r['stops']}" + (" | ЛИКВИДАЦИЯ" if r["liquidated"] else "")
                if "ret" in r else "ОШИБКА " + r["error"]), flush=True)
