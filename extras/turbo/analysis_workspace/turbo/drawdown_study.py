"""Турбо: как уменьшить минус в неудачных ставках. Капитал — поминутно (backtest.balance_sample_divider = 1).

Прежние расчёты (risk_rules.py) видели капитал раз в час и брали значение на конец часа, поэтому стоп ставки
панелью (капитал ≤ старт × S) срабатывал «на часовой точке» и провалы внутри часа не видел.
Только рынок 2025–2026: 30-дневные окна со стартом каждые 5 дней (01.01.2025 … 30.08.2026). Итоги — отдельно
за 2025 и 2026 (улучшение должно быть в обоих), по месяцам со ставкой (BTC выше средней за 200 дней) и по всем.
Правила панели (цель ×T, стоп ×S) проверяются по минутам:
выход — худший капитал из двух следующих минут (панель проверяет раз в 15 с, перезапуск в panic занимает время)
минус расходы на закрытие (COST от капитала: комиссия taker + проскальзывание при экспозиции ~20).

    venv\\Scripts\\python turbo\\drawdown_study.py run [--only 1,2] [--workers 6]
    venv\\Scripts\\python turbo\\drawdown_study.py report
Кривые: turbo/results/dd_curves/<вариант>/<старт>.npy, итог: turbo/results/drawdown_study.json.
"""
import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import episodes as EP  # noqa: E402
import risk_rules as RR  # noqa: E402

MC, ROOT = EP.MC, EP.ROOT
CURVES = os.path.join(HERE, "results", "dd_curves")
OUT = os.path.join(HERE, "results", "drawdown_study.json")
COST = 0.02                 # расходы на закрытие по панели, доля капитала
DELAY = 2                   # минут от сигнала до закрытия
L, H = "bot.long.risk.", "bot.long.hsl."
ENGINE = {L + "total_wallet_exposure_limit": 20.0, L + "n_positions": 3.0, H + "enabled": True,
          H + "red_threshold": 0.4, H + "restart_after_red_policy": "never", "live.hsl_signal_mode": "unified",
          "backtest.balance_sample_divider": 1}
ST = "bot.long.strategy.trailing_martingale."
VARIANTS = {
    1: ("Сейчас (turbo_smart2: эксп. 20, 3 монеты, стоп движка 40%)", {}),
    2: ("Раньше «только тейки»: при просадке 20%", {H + "tier_ratios": {"yellow": 0.3, "orange": 0.5}}),
    3: ("«Только тейки» при 20%, быстрый (1 ч), стоп движка 60%",
        {H + "red_threshold": 0.6, H + "ema_span_minutes": 60.0, H + "tier_ratios": {"yellow": 0.2, "orange": 0.33}}),
    4: ("«Только тейки» при 12%, быстрый (1 ч), стоп движка 60%",
        {H + "red_threshold": 0.6, H + "ema_span_minutes": 60.0, H + "tier_ratios": {"yellow": 0.1, "orange": 0.2}}),
    5: ("Без новых монет при 20%, быстрый (1 ч), стоп движка 60%",
        {H + "red_threshold": 0.6, H + "ema_span_minutes": 60.0, H + "tier_ratios": {"yellow": 0.2, "orange": 0.33},
         H + "orange_tier_mode": "graceful_stop"}),
    6: ("4 монеты (эксп. 20)", {L + "n_positions": 4.0}),
    7: ("Мягче усреднение (double_down 0,94 → 0,7)", {ST + "entry.double_down_factor": 0.7}),
    8: ("Экспозиция 16", {L + "total_wallet_exposure_limit": 16.0}),
    9: ("Активнее разгрузка зависших позиций (unstuck)",
        {"bot.long.unstuck.threshold": 0.3, "bot.long.unstuck.close_pct": 0.1, "bot.long.unstuck.loss_allowance_pct": 0.03}),
}
PANEL = [(1.75, None), (1.75, 0.6), (1.75, 0.65), (1.75, 0.7), (1.75, 0.75), (2.0, 0.6), (1.5, 0.7)]


FIRST, LAST_START, STEP = "2025-01-01", "2026-08-30", 5     # только рынок 2025–2026, старт каждые 5 дней
YEAR_SPLIT = "2026-01-01"


def starts():
    """[(старт, конец, ставка разрешена фильтром SMA200)] — все 30-дневные окна 2025–2026."""
    from datetime import datetime, timedelta
    btc, fund = RR.btc_hourly(), RR.btc_funding()
    d, last, out = datetime.fromisoformat(FIRST), datetime.fromisoformat(LAST_START), []
    while d <= last:
        s = d.strftime("%Y-%m-%d")
        out.append((s, (d + timedelta(days=EP.WINDOW)).strftime("%Y-%m-%d"), RR.entry_features(btc, fund, s)["sma"][200]))
        d += timedelta(days=STEP)
    return out


def run_one(vid, start, end):
    path_npy = os.path.join(CURVES, str(vid), f"{start}.npy")
    if os.path.exists(path_npy):
        return "cached"
    res = backtest_curve({**ENGINE, **VARIANTS[vid][1]}, start, end, f"dd{vid:02d}_{start}")
    if isinstance(res, str):
        return res
    os.makedirs(os.path.dirname(path_npy), exist_ok=True)
    np.save(path_npy, res[0])
    shutil.rmtree(res[1], ignore_errors=True)   # поминутные файлы большие, кривая сохранена
    return "ok"


def backtest_curve(over, start, end, tag, base=None, coins=None):
    """Прогон passivbot с поминутным капиталом → (капитал / капитал на старте, папка прогона) или текст ошибки.
    Монеты, которых на тот момент не было на бирже, убираются (живой бот их бы не увидел)."""
    base_dir = f"backtests/turbo_dd/{tag}"
    coins = list(coins or MC.default_coins())
    for _ in range(12):
        cfg = MC.build(start, end, EP.STAKE, base_path=base or MC.BASE, coins=coins,
                       overrides={"backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir, **over})
        cfg.pop("_source", None)
        cpath = os.path.join(ROOT, "configs", "turbo_dd", f"{tag}.json")
        os.makedirs(os.path.dirname(cpath), exist_ok=True)
        json.dump(cfg, open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run([EP.PASSIVBOT, "backtest", cpath, "-dp", "y"], cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags,
                           stdin=subprocess.DEVNULL)
        m = EP.MISSING_COIN.search(p.stdout + p.stderr) if p.returncode != 0 else None
        if not m or m.group(1) not in coins:
            break
        coins = [c for c in coins if c != m.group(1)]
        if not coins:            # пустой список MC.build заменил бы всеми монетами
            return f"ERROR нет данных по монетам на {start}"
    runs = sorted(glob.glob(os.path.join(ROOT, base_dir, "*", "*", "balance_and_equity.csv.gz")), key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        return "ERROR " + (p.stdout + p.stderr)[-300:]
    eq = pd.read_csv(runs[-1])["usd_total_equity"].astype(float).values
    if not len(eq):
        shutil.rmtree(os.path.join(ROOT, base_dir), ignore_errors=True)
        return f"ERROR пустой прогон на {start} (нет торгуемых свечей)"
    return (eq / eq[0]).astype(np.float32), os.path.join(ROOT, base_dir)


def first(mask, frm=0):
    """Первый индекс ≥ frm, где mask истинна, иначе None."""
    m = mask[frm:]
    i = int(m.argmax()) if m.size else 0
    return frm + i if m.size and m[i] else None


def outcome(rel, target, stop, lock=None, crash=None, cost=COST, delay=DELAY):
    """Итог ставки по поминутной кривой: самое раннее из событий — сгорела (≤ BURN), цель ×target, стоп ×stop,
    «безубыток» lock=(A, B): после ×A стоп поднимается до ×B, crash — поминутный флаг выхода (обвал BTC).
    Выход — худший капитал из delay следующих минут минус расходы cost."""
    ev = {"burn": first(rel <= RR.BURN)}
    if target:
        ev["target"] = first(rel >= target)
    if stop:
        ev["stop"] = first(rel <= stop)
    if lock:
        ia = first(rel >= lock[0])
        j = first(rel <= lock[1], ia) if ia is not None else None
        if j is not None and (ev.get("stop") is None or j < ev["stop"]):
            ev["stop"] = j
    if crash is not None:
        ev["crash"] = first(crash)
    ev = {k: v for k, v in ev.items() if v is not None}
    if ev:
        order = ["burn", "target", "stop", "crash"]          # одна минута: сгорела > цель > стоп > обвал
        kind = min(ev, key=lambda k: (ev[k], order.index(k)))
        i = ev[kind]
        if kind == "burn":
            return float(rel[i]) - 1, "burn"
        return float(rel[i:i + delay + 1].min()) * (1 - cost) - 1, kind
    if len(rel) < 30 * 1440 * 0.95 and rel[-1] < 0.5:     # движок оборвал прогон — ликвидация
        return -0.95, "burn"
    return float(rel[-1]) - 1, "end"


def summarize(items):
    res = np.array([r for r, _ in items])
    n = len(res)
    if not n:
        return None
    kinds = [k for _, k in items]
    worst = np.sort(res)[:max(1, int(round(n * 0.2)))]
    losses = res[res < 0]
    g = sum(math.log(max(1e-9, 1 + 0.25 * r)) for r in res) / n
    return {"bets": n, "mean": round(res.mean() * 100, 1), "median": round(float(np.median(res)) * 100, 1),
            "p_target": round(kinds.count("target") / n * 100, 1), "p_stop": round(kinds.count("stop") / n * 100, 1),
            "p_burn": round(kinds.count("burn") / n * 100, 1), "p_loss": round(len(losses) / n * 100, 1),
            "p_loss50": round(float((res <= -0.5).mean()) * 100, 1),
            "avg_loss": round(float(losses.mean()) * 100, 1) if len(losses) else 0.0,
            "worst20": round(float(worst.mean()) * 100, 1), "worst": round(float(res.min()) * 100, 1),
            "growth_25": round((math.exp(g) - 1) * 100, 2)}


def report():
    wins = starts()
    out = {}
    for vid, (name, _) in VARIANTS.items():
        curves = {}
        for s, _, ok in wins:
            f = os.path.join(CURVES, str(vid), f"{s}.npy")
            if os.path.exists(f):
                curves[s] = (np.load(f), ok)
        if len(curves) < len(wins):
            print(f"№{vid} {name}: кривых {len(curves)}/{len(wins)} — пропускаю")
            continue
        out[vid] = {"name": name, "rules": {}}
        for target, stop in PANEL:
            r = {}
            for scope in ("по фильтру", "все месяцы"):
                per = {"2025": [], "2026": []}
                for s, (rel, ok) in curves.items():
                    if scope == "по фильтру" and not ok:
                        continue
                    per["2025" if s < YEAR_SPLIT else "2026"].append(outcome(rel, target, stop))
                r[scope] = {k: summarize(v) for k, v in per.items()}
                r[scope]["оба"] = summarize(per["2025"] + per["2026"])
            out[vid]["rules"][f"×{target} стоп {stop or '—'}"] = r
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n_ok = sum(ok for *_, ok in wins)
    print(f"окон 2025–2026: {len(wins)} (старт каждые {STEP} дн.), ставка разрешена фильтром SMA200: {n_ok}")
    for scope in ("по фильтру", "все месяцы"):
        print(f"\n######## {scope}")
        for vid, v in out.items():
            print(f"\n№{vid} {v['name']}")
            for rule, r in v["rules"].items():
                a, b, c = r[scope]["2025"], r[scope]["2026"], r[scope]["оба"]
                print(f"  {rule:<16} рост {a['growth_25']:+5.2f}/{b['growth_25']:+5.2f} | ср. {a['mean']:+5.1f}/{b['mean']:+5.1f}% "
                      f"| ×цель {c['p_target']:4.1f}% | стоп {c['p_stop']:4.1f}% | сгор. {c['p_burn']:4.1f}% | в минусе {c['p_loss']:4.1f}% "
                      f"| ср. убыток {a['avg_loss']:+5.1f}/{b['avg_loss']:+5.1f}% | худшие 20% {a['worst20']:+5.1f}/{b['worst20']:+5.1f}% "
                      f"| −50%+ {c['p_loss50']:4.1f}% | худшая {c['worst']:+5.1f}%")
    print("\n(пары чисел: 2025 / 2026; рост — капитала при ставке 25% в месяц, %/мес)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, help="только первые N месяцев (проверка скорости)")
    a = ap.parse_args()
    if a.cmd == "report":
        return report()
    wins = starts()[:a.limit] if a.limit else starts()
    only = [int(x) for x in a.only.split(",")] if a.only else list(VARIANTS)
    print(f"окон 2025–2026: {len(wins)}; вариантов: {only}", flush=True)
    for vid in only:
        t0 = time.time()
        with ThreadPoolExecutor(a.workers) as ex:
            res = list(ex.map(lambda w: run_one(vid, w[0], w[1]), wins))
        errs = [r for r in res if r.startswith("ERROR")]
        print(f"№{vid} {VARIANTS[vid][0]}: {len(res) - len(errs)} ок, ошибок {len(errs)}, {time.time() - t0:.0f} с", flush=True)
        for e in errs[:2]:
            print("   ", e.replace("\n", " ")[:300], flush=True)


if __name__ == "__main__":
    main()
