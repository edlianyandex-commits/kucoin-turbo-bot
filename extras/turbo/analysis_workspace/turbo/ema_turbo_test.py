"""Turbo: движок EMA Anchor (вторая стратегия passivbot) вместо trailing martingale, правила ставки 3.0.

Ставки — те же 31 старт второй половины 2026 (01.07…30.08 каждые 2 дня, как turbo/coin_algos.py) плюс ставки,
захватывающие обвал октября 2025. Экспозиция 20, 3 монеты, только лонг, стоп passivbot как у 3.0; поверх — цель ×2,
стоп ставки ×0,7 и выход при обвале BTC (капитал поминутно). Параметры EMA Anchor — пример автора passivbot.

    venv\\Scripts\\python turbo\\ema_turbo_test.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_algos as CA  # noqa: E402
import coin_pick as CP  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

EMA = os.path.join(CA.ROOT, "configs", "examples", "ema_anchor.json")
SHORT_OFF = {"bot.short.risk.total_wallet_exposure_limit": 0.0}
CRASH_STARTS = ["2025-09-20", "2025-09-25", "2025-09-30", "2025-10-03", "2025-10-06"]


def run(key, start, base):
    path = os.path.join(CA.CURVES, key, f"{start}.npy")
    if not os.path.exists(path):
        res = CA.DS.backtest_curve({**PW.ENGINE3, **SHORT_OFF}, start, CP.end_of(start), f"et_{key}_{start}", base=base)
        if isinstance(res, str):
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.save(path, res[0])
        CA.shutil.rmtree(res[1], ignore_errors=True)
    return np.load(path)


H1_STARTS = CP.grid("2026-01-01", "2026-06-26", step=5)        # проверка: первая половина 2026
HBAR_DAY = ["2026-09-29"]                                         # обвал одной монеты при спокойном BTC


def main():
    btc = RR.btc_hourly()
    jobs = [(eng, s) for eng in ("tm", "ema") for s in CA.BET_STARTS + CRASH_STARTS + H1_STARTS + HBAR_DAY]
    with ThreadPoolExecutor(6) as ex:
        rels = list(ex.map(lambda j: run(f"engine_{j[0]}", j[1], EMA if j[0] == "ema" else None), jobs))
    res = {}
    for (eng, s), rel in zip(jobs, rels):
        if rel is not None:
            r, kind = CP.bet(rel, s, btc)
            res.setdefault(eng, []).append({"start": s, "result_pct": round(r * 100, 1), "kind": kind,
                                            "min_pct": round((float(rel.min()) - 1) * 100, 1)})
    json.dump(res, open(os.path.join(CA.RES, "ema_turbo.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    names = {"tm": "3.0 сейчас (trailing martingale)", "ema": "3.0 на движке EMA Anchor"}
    for eng, bets in res.items():
        for label, sel in (("II пол. 2026", lambda s: s in CA.BET_STARTS), ("I пол. 2026", lambda s: s in H1_STARTS)):
            grp = [b for b in bets if sel(b["start"])]
            s = CP.summary([(b["result_pct"] / 100, b["kind"]) for b in grp])
            print(f"{names[eng]:<36} {label}, {s['bets']} ставок: ×2 {s['p_target']:5.1f}% | в минусе {s['p_loss']:5.1f}% | "
                  f"ср. итог {s['mean']:+6.1f}% | ср. убыток {s['avg_loss']:+6.1f}% | худшая {s['worst']:+6.1f}% | "
                  f"глубже −20% внутри ставки: {sum(b['min_pct'] <= -20 for b in grp)}")
        crash = [b for b in bets if b["start"] in CRASH_STARTS + HBAR_DAY]
        print(f"{'':<36} обвал октября 2025 и день HBAR: " + " | ".join(
            f"{b['start'][2:]} {b['result_pct']:+.0f}% ({b['kind']}, мин. {b['min_pct']:+.0f}%)" for b in crash))


if __name__ == "__main__":
    main()
