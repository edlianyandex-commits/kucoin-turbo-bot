"""Турбо: какие настройки были бы в плюсе с начала бумажной торговли (29.09.2026) — и не подгонка ли это.

1) scan — сетка вариантов движка с 29.09 00:00 UTC по конец вчерашнего дня (капитал поминутно), поверх — правила
   панели 3.0 (цель ×2, стоп ставки ×0,7; выход при обвале BTC в эти дни не срабатывал). Свечей за текущий день
   в бэктесте нет: в 00:00 сегодня данные кончаются, passivbot закрывает позиции по рынку (close_panic_long) —
   поэтому отрезок обрезается на конце вчерашнего дня.
2) robust --ids 5,17,… — те же варианты на 122 месячных окнах 2025–2026 (как turbo/drawdown_study.py) с правилами 3.0:
   вариант, который выиграл полтора дня, но хуже на двух годах, — везение, а не находка.

    venv\\Scripts\\python turbo\\paper_window.py scan [--ids 1,30] [--workers 6]
    venv\\Scripts\\python turbo\\paper_window.py robust --ids 3,7
Итог: turbo/results/paper_window.json, paper_window_robust.json.
"""
import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import risk_rules as RR  # noqa: E402

MC, ROOT = DS.MC, DS.ROOT
START, END = "2026-09-29", "2026-10-01"
RES = os.path.join(HERE, "results")
CURVES = os.path.join(RES, "paper_window")
L, H, U, F = "bot.long.risk.", "bot.long.hsl.", "bot.long.unstuck.", "bot.long.forager."
EN = "bot.long.strategy.trailing_martingale.entry."
CL = "bot.long.strategy.trailing_martingale.close."
ENGINE3 = {**DS.ENGINE, H + "tier_ratios": {"yellow": 0.3, "orange": 0.5}}
TUNED = os.path.join(RES, "tuned")      # подобранные конфиги (из панели убраны 02.10.2026)
RULES3 = (2.0, 0.7, (12, 0.05))


def top_coins(n):
    ku = json.load(open(os.path.join(ROOT, "tb4", "kucoin_coins.json"), encoding="utf-8"))
    return sorted(MC.default_coins(), key=lambda c: -ku["kucoin"][c]["turnover24h"])[:n]


def variants():
    """[(название, правки, базовый конфиг, монеты)] — №1 = «Умный ва-банк 3.0» как есть."""
    v = [("3.0 как есть (эксп. 20, 3 монеты)", {}, None, None)]
    for twel in (5, 8, 12, 16, 20):
        for n in (2, 3, 4, 5, 7):
            if (twel, n) != (20, 3):
                v.append((f"эксп. {twel}, монет {n}", {L + "total_wallet_exposure_limit": float(twel), L + "n_positions": float(n)},
                          None, None))
    for twel in (12, 16, 20):
        for n in (3, 5):
            v.append((f"эксп. {twel}, монет {n}, потолок на монету без превышения",
                      {L + "total_wallet_exposure_limit": float(twel), L + "n_positions": float(n), L + "we_excess_allowance_pct": 0.0},
                      None, None))
    for x in (0.5, 0.7, 1.2):
        v.append((f"докупки: множитель {x} (было 0,94)", {EN + "double_down_factor": x}, None, None))
    for x in (0.01, 0.03, 0.05):
        v.append((f"докупки: шаг {x * 100:g}% (было 1,9%)", {EN + "threshold_base_pct": x}, None, None))
    for x in (0.004, 0.02):
        v.append((f"первый вход {x * 100:g}% (было 0,81%)", {EN + "initial_qty_pct": x}, None, None))
    for x in (60.0, 240.0):
        v.append((f"пауза между входами {x:g} мин (было 24)", {L + "entry_cooldown_minutes": x}, None, None))
    for x in (0.5, 1.0):
        v.append((f"тейк: закрывать {x * 100:g}% позиции (было 84%)", {CL + "qty_pct": x}, None, None))
    for x in (0.002, 0.008):
        v.append((f"тейк: порог {x * 100:g}% (было −0,27%)", {CL + "threshold_base_pct": x}, None, None))
    for x in (0.2, 0.3, 0.7):
        v.append((f"разгрузка зависших: порог {x:g} (было 0,466)", {U + "threshold": x}, None, None))
    for x in (0.02, 0.05):
        v.append((f"разгрузка зависших: убыток до {x * 100:g}% (было 0,52%)", {U + "loss_allowance_pct": x}, None, None))
    v.append(("разгрузка зависших выключена", {U + "enabled": False}, None, None))
    v.append(("стоп passivbot 20%", {H + "red_threshold": 0.2}, None, None))
    v.append(("стоп passivbot 30%", {H + "red_threshold": 0.3}, None, None))
    v.append(("стоп passivbot 30%, быстрый (1 ч)", {H + "red_threshold": 0.3, H + "ema_span_minutes": 60.0}, None, None))
    v.append(("стоп passivbot выключен", {H + "enabled": False}, None, None))
    for name, w in (("выбор монет: больше по объёму", {"ema_readiness": 0.2, "volatility": 0.2, "volume": 0.6}),
                    ("выбор монет: только по объёму", {"ema_readiness": 0.0, "volatility": 0.0, "volume": 1.0}),
                    ("выбор монет: только по волатильности", {"ema_readiness": 0.0, "volatility": 1.0, "volume": 0.0})):
        v.append((name, {F + "score_weights": w}, None, None))
    v.append(("только 10 самых ликвидных монет", {}, None, top_coins(10)))
    v.append(("только 20 самых ликвидных монет", {}, None, top_coins(20)))
    for fname, twel in (("tuned_growth", 6.0), ("tuned_growth", 9.6), ("tuned_moderate", 4.75), ("tuned_moderate", 9.5)):
        v.append((f"подобранный {fname.split('_')[1]} (эксп. {twel:g})", {L + "total_wallet_exposure_limit": twel},
                  os.path.join(TUNED, f"{fname}.json"), None))
    # потолок на одну монету (сейчас 20 / 3 × 1,37 ≈ 9,1× баланса) — №65…70
    for twel, n, allow in ((20, 4, 0.0), (15, 3, 0.0), (20, 5, 0.0), (20, 3, 0.1)):
        cap = twel / n * (1 + allow)
        v.append((f"потолок {cap:.1f}× на монету: эксп. {twel}, монет {n}, превышение {allow * 100:g}%",
                  {L + "total_wallet_exposure_limit": float(twel), L + "n_positions": float(n), L + "we_excess_allowance_pct": allow},
                  None, None))
    v.append(("потолок 5× на монету: урезать позицию выше (enforcer 0,55)",
              {L + "position_exposure_enforcer_enabled": True, L + "position_exposure_enforcer_threshold": 0.55}, None, None))
    v.append(("потолок 6,7× на монету: урезать позицию выше (enforcer 0,73)",
              {L + "position_exposure_enforcer_enabled": True, L + "position_exposure_enforcer_threshold": 0.73}, None, None))
    return v


def cutoff_minutes():
    """Последняя минута вчерашнего дня (UTC) от начала отрезка: свечей за сегодня в бэктесте нет."""
    today = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    return int((today - datetime.fromisoformat(START)).total_seconds() // 60) - 1


def run_variant(i, v, start, end, out_npy, tag):
    if os.path.exists(out_npy):
        return "cached"
    name, over, base, coins = v
    res = DS.backtest_curve({**ENGINE3, **over}, start, end, tag, base=base, coins=coins)
    if isinstance(res, str):
        return res
    os.makedirs(os.path.dirname(out_npy), exist_ok=True)
    np.save(out_npy, res[0])
    shutil.rmtree(res[1], ignore_errors=True)
    return "ok"


def scan(workers, only=None):
    vs = [(i, v) for i, v in enumerate(variants(), 1) if not only or i in only]
    cut = cutoff_minutes()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    print(f"вариантов {len(vs)}; отрезок {START} 00:00 UTC → +{(cut + 1) / 60:.0f} ч (конец вчерашнего дня UTC)", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda iv: run_variant(iv[0], iv[1], START, END, os.path.join(CURVES, stamp, f"{iv[0]}.npy"),
                                                 f"pw{iv[0]:03d}"), vs))
    print(f"прогоны: {time.time() - t0:.0f} с, ошибок {sum(r.startswith('ERROR') for r in res)}", flush=True)
    btc = RR.btc_hourly()
    rows = []
    for i, v in vs:
        f = os.path.join(CURVES, stamp, f"{i}.npy")
        if not os.path.exists(f):
            continue
        rel = np.load(f)[:cut + 1]
        fl = DR.crash_minutes(btc, START, len(rel), *RULES3[2])
        r3, k3 = DS.outcome(rel, RULES3[0], RULES3[1], None, fl)
        rows.append({"id": i, "name": v[0], "now_pct": round((float(rel[-1]) - 1) * 100, 1),
                     "min_pct": round((float(rel.min()) - 1) * 100, 1), "max_pct": round((float(rel.max()) - 1) * 100, 1),
                     "rules3_pct": round(r3 * 100, 1), "rules3_kind": k3})
    json.dump({"start": START, "hours": round(cut / 60, 1), "until_utc": stamp, "rows": rows},
              open(os.path.join(RES, "paper_window_part.json" if only else "paper_window.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n{'№':>3} {'вариант':<58} {'сейчас':>7} {'минимум':>8} {'максимум':>8} | с правилами 3.0")
    for r in sorted(rows, key=lambda r: -r["rules3_pct"]):
        print(f"{r['id']:>3} {r['name']:<58} {r['now_pct']:+6.1f}% {r['min_pct']:+7.1f}% {r['max_pct']:+7.1f}% | "
              f"{r['rules3_pct']:+6.1f}% ({r['rules3_kind']})")


def robust(ids, workers):
    vs = variants()
    wins = DS.starts()
    btc = RR.btc_hourly()
    out = json.load(open(os.path.join(RES, "paper_window_robust.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(RES, "paper_window_robust.json")) else {}
    base_curves = {s: np.load(os.path.join(DS.CURVES, "2", f"{s}.npy")) for s, _, _ in wins}   # 3.0 = движок №2

    def summary(curves):
        per = {"2025": [], "2026": []}
        for s, _, _ in wins:
            rel = curves[s]
            fl = DR.crash_minutes(btc, s, len(rel), *RULES3[2])
            per["2025" if s < DS.YEAR_SPLIT else "2026"].append(DS.outcome(rel, RULES3[0], RULES3[1], None, fl))
        return {k: DS.summarize(x) for k, x in per.items()}

    out["3.0"] = {"name": "3.0 как есть", **summary(base_curves)}
    for i in ids:
        t0 = time.time()
        with ThreadPoolExecutor(workers) as ex:
            res = list(ex.map(lambda w: run_variant(i, vs[i - 1], w[0], w[1], os.path.join(DS.CURVES, f"pw{i}", f"{w[0]}.npy"),
                                                    f"pw{i:03d}_{w[0]}"), wins))
        errs = [r for r in res if r.startswith("ERROR")]
        curves = {s: np.load(os.path.join(DS.CURVES, f"pw{i}", f"{s}.npy")) for s, _, _ in wins}
        out[str(i)] = {"name": vs[i - 1][0], **summary(curves)}
        print(f"№{i} {vs[i - 1][0]}: {len(res) - len(errs)} ок, ошибок {len(errs)}, {time.time() - t0:.0f} с", flush=True)
        json.dump(out, open(os.path.join(RES, "paper_window_robust.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n122 окна 2025–2026, правила 3.0 (пары: 2025 / 2026)")
    for k, v in out.items():
        a, b = v["2025"], v["2026"]
        print(f"  {k:>4} {v['name']:<58} рост {a['growth_25']:+5.2f}/{b['growth_25']:+5.2f} | ср. {a['mean']:+5.1f}/{b['mean']:+5.1f}% | "
              f"ср. убыток {a['avg_loss']:+5.1f}/{b['avg_loss']:+5.1f}% | худшие 20% {a['worst20']:+5.1f}/{b['worst20']:+5.1f}% | "
              f"×цель {a['p_target']:4.1f}/{b['p_target']:4.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "robust"])
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(a.workers, {int(x) for x in a.ids.split(",") if x})
    else:
        robust([int(x) for x in a.ids.split(",") if x], a.workers)


if __name__ == "__main__":
    main()
