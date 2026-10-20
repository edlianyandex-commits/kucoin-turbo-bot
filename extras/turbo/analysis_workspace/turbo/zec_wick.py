r"""Минутная просадка на падении ZEC 05.06.2026 (−45% за 7 ч, без обвала BTC) — варианты основного бота и Turbo 5.0.
    venv\Scripts\python turbo\zec_wick.py
"""
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_study as DS  # noqa: E402
import main_variants as MV  # noqa: E402
import universe_chain as UC  # noqa: E402
import universe_test as UT  # noqa: E402

L = "bot.long.risk."
V = {"10×8 (основной бот)": {}, "7×8": MV.VARIANTS["n7"][1], "5×9 (Turbo 5.0)": MV.VARIANTS["n5tw9"][1],
     "5×9 + ограничитель позиции": {**MV.VARIANTS["n5tw9"][1], L + "position_exposure_enforcer_enabled": True,
                                     L + "position_exposure_enforcer_threshold": 1.0},
     "7×9 + ограничитель позиции": {**MV.VARIANTS["n7tw9"][1], L + "position_exposure_enforcer_enabled": True,
                                     L + "position_exposure_enforcer_threshold": 1.0}}


def run(item):
    name, extra = item
    res = DS.backtest_curve({**UC.OVER, "backtest.starting_balance": 300.0, **extra}, "2026-06-01", "2026-06-10",
                            f"zec_{abs(hash(name)) % 10**6}", base=UT.EM.EMA, coins=UT.universes()["оборот ≥ 5 млн $"])
    if isinstance(res, str):
        return name, res
    rel, rd = res
    shutil.rmtree(rd, ignore_errors=True)
    dd = 1 - rel / np.maximum.accumulate(rel)
    h = rel[::60]
    hdd = 1 - h / np.maximum.accumulate(h)
    return name, f"минутная просадка {dd.max() * 100:5.1f}% (по часам {hdd.max() * 100:5.1f}%), итог 9 дней {(rel[-1] - 1) * 100:+6.1f}%"


DS.EP.STAKE = 300.0
with ThreadPoolExecutor(5) as ex:
    for name, r in ex.map(run, V.items()):
        print(f"{name:<28} {r}", flush=True)
