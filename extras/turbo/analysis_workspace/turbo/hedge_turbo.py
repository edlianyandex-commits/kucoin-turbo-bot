"""Широкий Turbo с шортами (пользователь, 01.10.2026): лонг + шорт одновременно (hedge mode — KuCoin его поддерживает)
и только шорт, EMA Anchor (параметры шорта — те же из примера автора passivbot). Гипотеза: шорты сами страхуют от
обвала (10.10.2025 альты упали — шорты в плюсе), может быть, даже без выхода по BTC.

Плечо: бэктест passivbot маржу не проверяет. На KuCoin максимальное плечо по нашим монетам 50 (10 монет) / 75 (23) /
100–125 (ETH, BTC): лонг 30 требует плеча ~50, лонг 30 + шорт 30 — ~75 (под маржу ушло бы 80% счёта).

Шаг 1 — отбор (как turbo/wide_turbo.py: один 30-дневный прогон, старт каждые 3 дня); --chain KEY,KEY — шаг 2:
ставки подряд со старта каждого дня (как turbo/wide_chain.py).

    venv\\Scripts\\python turbo\\hedge_turbo.py [--workers 8] [--chain h30s15,...]
Итог: turbo/results/hedge_turbo.json / hedge_chain.json.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import drawdown_study as DS  # noqa: E402
import risk_rules as RR  # noqa: E402
import wide_turbo as WT  # noqa: E402  (варианты только лонг 10/15 монет)

EM = WT.EM
L, S = "bot.long.", "bot.short."
HSL_SHORT = {S + "hsl.enabled": True, S + "hsl.red_threshold": 0.4, S + "hsl.restart_after_red_policy": "never",
             S + "hsl.tier_ratios": {"yellow": 0.3, "orange": 0.5}}


def variant(key, name, n, tw_long, tw_short):
    over = {L + "risk.total_wallet_exposure_limit": float(tw_long), L + "risk.n_positions": float(n),
            S + "risk.total_wallet_exposure_limit": float(tw_short), S + "risk.n_positions": float(n),
            "live.hedge_mode": True, **HSL_SHORT}
    EM.VARIANTS[key] = (name, over, EM.EMA)
    WT.TW[key] = tw_long + tw_short


variant("h30s15", "лонг 15×30 + шорт 15×15", 15, 30, 15)
variant("h30s30", "лонг 15×30 + шорт 15×30", 15, 30, 30)
variant("h20s20n10", "лонг 10×20 + шорт 10×20", 10, 20, 20)
variant("s30n15", "только шорт 15×30", 15, 0, 30)
EM.VARIANTS["ema30n15"] = ("только лонг 15×30 (прошлый тест)", *EM.VARIANTS["ema30n15"][1:])
SCREEN = ("ema30n15", "h30s15", "h30s30", "h20s20n10", "s30n15")
# Плечо на бирже не больше 10× (пользователь, 01.10.2026): позиций в сумме (лонг + шорт) не больше ~10× баланса —
# иначе KuCoin не даст маржи на новые входы. Turbo 4.0 (экспозиция 20) при таком плече в задуманном виде невозможен.
for n, tw in ((15, 10), (10, 10), (15, 8)):
    variant(f"l{tw}n{n}", f"лонг {n}×{tw} (плечо 10)", n, tw, 0)
variant("h7s3", "лонг 15×7 + шорт 15×3 (плечо 10)", 15, 7, 3)
variant("h5s5", "лонг 15×5 + шорт 15×5 (плечо 10)", 15, 5, 5)
EM.VARIANTS["ema10"] = ("Turbo 4.0 при плече 10: 3 монеты × 10", *EM.VARIANTS["ema10"][1:])
WT.TW["ema10"] = 10
SETS = {"hedge": SCREEN, "lev10": ("ema10", "l10n15", "l10n10", "l8n15", "h7s3", "h5s5")}
RULES = {"без правил": None, "выход по BTC (закрыть всё)": (0.0, None)}
CHAIN_RULES = {"без правил панели": {"mode": "none", "crash": False},
               "выход по BTC (закрыть всё), запуск со следующего дня": {"mode": "none"}}


def screen(workers, keys=SCREEN, out="hedge_turbo.json"):
    jobs = [(k, s) for k in keys for h in EM.HALVES.values() for s in h]
    with ThreadPoolExecutor(workers) as ex:
        curves = dict(zip(jobs, ex.map(lambda j: EM.run(*j), jobs)))
    btc = RR.btc_hourly()
    rows = []
    for k in keys:
        for rname, rule in RULES.items():
            row = {"key": k, "name": EM.VARIANTS[k][0], "rule": rname}
            all26 = []
            for half, starts in EM.HALVES.items():
                m = [WT.month(curves[(k, s)], s, btc, rule, 0.001 * WT.TW[k]) for s in starts if curves[(k, s)] is not None]
                row[half] = WT.stats(m)
                if "2026" in half:
                    all26 += m
            row["2026"] = WT.stats(all26)
            rows.append(row)
    json.dump(rows, open(os.path.join(HERE, "results", out), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    H = list(EM.HALVES)
    print("Отбор: один 30-дневный прогон, прибыль в обороте, $200; цель +22%/мес (+1000%/год)\n")
    for r in rows:
        x, a1, a2, c = r["2026"], r[H[0]], r[H[1]], r[H[2]]
        print(f"{r['name'][:34]:<34} {r['rule'][:26]:<26} | 2026: геом. {x['geo']:+5.1f}%/мес (год {x['year']:+6d}%), "
              f"≥+22% {x['p_goal']:4.1f}%, ×2 {x['p_x2']:4.1f}%, в минусе {x['p_loss']:4.1f}%, ≤½ {x['p_half']:4.1f}%, "
              f"худш. {x['worst']:+4d}% | I {a1['geo']:+5.1f}% II {a2['geo']:+5.1f}% | обвал 2025: геом. {c['geo']:+5.1f}%, "
              f"худш. {c['worst']:+4d}%")


def chain(keys, workers):
    import ema_chain as EC
    import month_paths as MP
    btc = RR.btc_hourly()
    windows = {"2026": CP.grid("2026-01-01", "2026-08-31", 1), "I пол. 2026": CP.grid("2026-01-01", "2026-05-31", 1),
               "II пол. 2026": CP.grid("2026-07-01", "2026-08-31", 1), "обвал 10.10.2025": CP.grid("2025-09-10", "2025-10-10", 1)}
    rows = []
    for key in keys:
        E = MP.Engine(0.001 * WT.TW[key], EC.bets(key, EC.build(key, workers), btc))
        for rname, rule in CHAIN_RULES.items():
            row = {"key": key, "name": EM.VARIANTS[key][0], "rule": rname}
            for w, starts in windows.items():
                row[w] = WT.stats([E.month(s, rule)[0] for s in starts])
            ys = np.array([E.month(s, rule, int((np.datetime64("2026-09-28") - np.datetime64(s)).astype(int)))[0]
                           for s in CP.grid("2026-01-01", "2026-01-15", 1)])
            months = (np.datetime64("2026-09-28") - np.datetime64("2026-01-08")).astype(int) / 30.4
            row["year"] = {"chain_median": round(float(np.median(ys)), 2), "chain_min": round(float(ys.min()), 2),
                           "chain_max": round(float(ys.max()), 2),
                           "annual_median": round((float(np.median(ys)) ** (12 / months) - 1) * 100)}
            rows.append(row)
    path = os.path.join(HERE, "results", "hedge_chain.json")
    old = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    old = [r for r in old if r["key"] not in keys]
    json.dump(old + rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("Ставки подряд, прибыль в обороте, $200; месяц = 30 дней со старта каждого дня\n")
    for r in rows:
        x, c, y = r["2026"], r["обвал 10.10.2025"], r["year"]
        print(f"{r['name'][:34]:<34} {r['rule'][:50]:<50}\n"
              f"   2026: геом. {x['geo']:+5.1f}%/мес, средн. {x['mean']:+5.1f}%, медиана {x['median']:+5.1f}%, ≥+22% {x['p_goal']:4.1f}%, "
              f"×2 {x['p_x2']:4.1f}%, в минусе {x['p_loss']:4.1f}%, ≤½ {x['p_half']:4.1f}%, худш. {x['worst']:+4d}%, лучш. {x['best']:+4d}%\n"
              f"   I пол. {r['I пол. 2026']['geo']:+5.1f}%/мес, II пол. {r['II пол. 2026']['geo']:+5.1f}%/мес | обвал 2025: геом. "
              f"{c['geo']:+5.1f}%, худш. {c['worst']:+4d}% | 01.01→28.09.2026 подряд: ×{y['chain_median']} (×{y['chain_min']}…×{y['chain_max']}), "
              f"≈ в год {y['annual_median']:+d}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--chain", default="")
    ap.add_argument("--set", default="hedge", choices=list(SETS))
    a = ap.parse_args()
    if a.chain:
        chain([k for k in a.chain.split(",") if k], a.workers)
    else:
        screen(a.workers, SETS[a.set], "hedge_turbo.json" if a.set == "hedge" else f"{a.set}_turbo.json")


if __name__ == "__main__":
    main()
