"""Турбо «как в жизни»: ставки одна за другой за период (по умолчанию 01.01–29.09.2026).

Ставка — настоящий прогон passivbot (капитал поминутно) на 30 дней или до срабатывания правила панели
(цель, стоп ставки, обвал BTC) — как в turbo/drawdown_study.py. Следующая ставка — со следующего дня.
Если за ставку сработал встроенный стоп passivbot (сделки close_panic_long) — на этом субаккаунте passivbot
30 дней не торгует: следующая ставка ждёт. С фильтром рынка ставка начинается только в день, когда BTC выше
средней за 200 дней. С фильтром монет на старте ставки отсекаются монеты, которые на coin_dd ниже максимума
часовых закрытий за 30 дней (как в панели). Ставка — фиксированные $200; «капитал» — старт $800 и ставка 25% каждый раз.

    venv\\Scripts\\python turbo\\sequence_2026.py [--only new,cur] [--first 2026-07-01 --last 2026-09-29 --out sequence_h2.json]
Итог: turbo/results/sequence_2026.json (или --out) и таблица в консоли.
"""
import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import risk_rules as RR  # noqa: E402

FIRST, LAST = "2026-01-01", "2026-09-29"
OUT = os.path.join(HERE, "results", "sequence_2026.json")
H = "bot.long.hsl."
SETUPS = {
    "new": ("Умный ва-банк 3.0 (новый)", {H + "tier_ratios": {"yellow": 0.3, "orange": 0.5}},
            {"target": 2.0, "stop": 0.7, "crash": (12, 0.05), "regime": False}),
    "cur": ("Умный ва-банк 2.0 (сейчас)", {}, {"target": 1.75, "stop": 0.6, "crash": None, "regime": True}),
    "cur_nofilter": ("Умный ва-банк 2.0 без фильтра", {}, {"target": 1.75, "stop": 0.6, "crash": None, "regime": False}),
    "new_cf": ("3.0 + фильтр монет 20%/30 дн.", {H + "tier_ratios": {"yellow": 0.3, "orange": 0.5}},
               {"target": 2.0, "stop": 0.7, "crash": (12, 0.05), "regime": False, "coin_dd": 0.2}),
    **{f"new_p{n}": (f"3.0 + пауза {n} дн. после проигрыша", {H + "tier_ratios": {"yellow": 0.3, "orange": 0.5}},
                     {"target": 2.0, "stop": 0.7, "crash": (12, 0.05), "regime": False, "pause_after_loss": n})
       for n in (3, 7, 14)},
    **{f"new_pump{L}": (f"3.0 без монет, падающих после пампа ({L} дн., ≥30%, −20%)",
                        {H + "tier_ratios": {"yellow": 0.3, "orange": 0.5}},
                        {"target": 2.0, "stop": 0.7, "crash": (12, 0.05), "regime": False, "pump": (L, 0.3, 0.2)})
       for L in (14, 30)},
    # движок EMA Anchor (вторая стратегия passivbot, пример автора) с правилами ставки 3.0 — turbo/ema_turbo_test.py
    "new_ema": ("3.0 на движке EMA Anchor", {H + "tier_ratios": {"yellow": 0.3, "orange": 0.5},
                                            "bot.short.risk.total_wallet_exposure_limit": 0.0},
                {"target": 2.0, "stop": 0.7, "crash": (12, 0.05), "regime": False,
                 "base": os.path.join(DS.ROOT, "configs", "examples", "ema_anchor.json")}),
}
PX = {}


def coin_filter(s, thr):
    """Фильтр монет на старте ставки: (монеты, отсечённые) по часовым ценам до s (turbo/coin_algos.py)."""
    import coin_algos as CA
    if "px" not in PX:
        PX["px"] = CA.prices()
    f = CA.features(PX["px"], s)
    coins = DS.MC.default_coins()
    dd = {c: float(f.loc[c, "dd30"]) for c in coins if c in f.index and pd.notna(f.loc[c, "dd30"])}
    return [c for c in coins if dd.get(c, 0.0) < thr], sorted((c for c in coins if dd.get(c, 0.0) >= thr), key=lambda c: -dd[c])


def run(key, btc, fund, first=None, last=None, tag="", verbose=True):
    """Цепочка ставок стратегии key с first по last (по умолчанию FIRST/LAST); tag — к именам папок прогонов
    (несколько цепочек параллельно)."""
    name, engine, rules = SETUPS[key]
    d, last, bets = datetime.fromisoformat(first or FIRST), datetime.fromisoformat(last or LAST), []
    while d < last:
        s = d.strftime("%Y-%m-%d")
        if rules["regime"] and not RR.entry_features(btc, fund, s)["sma"][200]:
            d += timedelta(days=1)
            continue
        coins, cut = coin_filter(s, rules["coin_dd"]) if rules.get("coin_dd") else (None, [])
        if rules.get("pump"):                  # не брать монеты, падающие после пампа (turbo/pump_filter.py)
            import pump_filter as PF
            if "pump_px" not in PX:
                PX["pump_px"] = PF.prices()
            cut = PF.excluded(PX["pump_px"], s, *rules["pump"])
            coins = sorted(set(DS.MC.default_coins()) - set(cut)) if cut else None
        if coins == []:                        # все монеты упали — панель не запустит ставку
            d += timedelta(days=1)
            continue
        end = min(d + timedelta(days=30), last + timedelta(days=1))
        res = DS.backtest_curve({**DS.ENGINE, **engine}, s, end.strftime("%Y-%m-%d"), f"seq{tag}_{key}_{s}", coins=coins,
                                base=rules.get("base"))
        if isinstance(res, str):
            print("  ошибка", s, res[:200])
            d += timedelta(days=1)
            continue
        rel, run_dir = res
        fl = DR.crash_minutes(btc, s, len(rel), *rules["crash"]) if rules["crash"] else None
        if fl is not None and fl[0]:          # обвал BTC уже идёт — панель не запустит ставку, ждём следующий день
            shutil.rmtree(run_dir, ignore_errors=True)
            d += timedelta(days=1)
            continue
        # минута выхода: самое раннее событие правил панели, иначе — весь прогон
        r, kind = DS.outcome(rel, rules["target"], rules["stop"], None, fl)
        ev = [DS.first(rel >= rules["target"]), DS.first(rel <= rules["stop"]) if rules["stop"] else None,
              DS.first(fl) if fl is not None else None, DS.first(rel <= RR.BURN)]
        ev = [e for e in ev if e is not None]
        i_end = min(ev) if ev and kind != "end" else len(rel) - 1
        t_end = d + timedelta(minutes=int(i_end))
        fills = pd.read_csv(sorted(glob.glob(os.path.join(run_dir, "*", "*", "fills.csv")))[-1])
        fills["t"] = pd.to_datetime(fills["timestamp"])
        panic = fills[(fills["type"] == "close_panic_long") & (fills["t"] <= t_end)]
        shutil.rmtree(run_dir, ignore_errors=True)
        open_bet = kind == "end" and end > last          # ставка ещё идёт на сегодня
        bets.append({"start": s, "end": t_end.strftime("%Y-%m-%d %H:%M"), "days": round(i_end / 1440, 1),
                     "kind": "идёт" if open_bet else {"target": "цель", "stop": "стоп", "crash": "обвал BTC",
                                                       "burn": "сгорела", "end": "30 дней"}[kind],
                     "result_pct": round(r * 100, 1), "min_pct": round((float(rel[:i_end + 1].min()) - 1) * 100, 1),
                     "hsl_latch": bool(len(panic)), "coins_cut": cut})
        b = bets[-1]
        if verbose:
            print(f"  {b['start']} → {b['end']} ({b['days']:4.1f} дн.) {b['kind']:<10} {b['result_pct']:+6.1f}%  "
                  f"минимум {b['min_pct']:+6.1f}%" + ("  стоп passivbot: 30 дней пауза" if b["hsl_latch"] else "")
                  + (f"  | фильтр монет: не берём {len(cut)} ({', '.join(cut[:6])}{' …' if len(cut) > 6 else ''})" if cut else ""),
                  flush=True)
        pause = 30 if len(panic) else 1              # стоп passivbot «без перезапуска» держит субаккаунт ~30 дней
        if rules.get("pause_after_loss") and r < 0:  # пауза после проигранной ставки (как CooldownPeriod у Freqtrade)
            pause = max(pause, rules["pause_after_loss"])
        d = (t_end + timedelta(days=pause)).replace(hour=0, minute=0)
    stake = sum(b["result_pct"] for b in bets) * 2.0          # $200 × %/100
    cap = 800.0
    for b in bets:
        cap += cap * 0.25 * b["result_pct"] / 100
    closed = [b for b in bets if b["kind"] != "идёт"]
    losses = [b["result_pct"] for b in closed if b["result_pct"] < 0]
    return {"name": name, "rules": rules, "bets": bets, "n": len(closed), "pnl_200": round(stake, 1),
            "capital_800": round(cap, 1), "wins": sum(b["result_pct"] > 0 for b in closed),
            "avg_loss": round(float(np.mean(losses)), 1) if losses else 0.0,
            "worst": min((b["result_pct"] for b in closed), default=0.0),
            "worst_min": min((b["min_pct"] for b in bets), default=0.0)}


def main():
    global FIRST, LAST, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--first", default=FIRST)
    ap.add_argument("--last", default=LAST)
    ap.add_argument("--out", default=os.path.basename(OUT))
    a = ap.parse_args()
    FIRST, LAST, OUT = a.first, a.last, os.path.join(HERE, "results", a.out)
    btc, fund = RR.btc_hourly(), RR.btc_funding()
    if btc.index[-1] < pd.Timestamp(LAST):
        print(f"внимание: часовые свечи BTC только до {btc.index[-1]} — удалите turbo/results/btc_1h.json для обновления")
    only = [k for k in a.only.split(",") if k in SETUPS]
    out = json.load(open(OUT, encoding="utf-8")) if only and os.path.exists(OUT) else {}
    for key in only or SETUPS:
        print(f"\n=== {SETUPS[key][0]}", flush=True)
        out[key] = run(key, btc, fund)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nИтог {FIRST} … {LAST}:")
    for k, v in out.items():
        print(f"  {v['name']:<32} ставок {v['n']:2d} (в плюсе {v['wins']}) | итог при ставке $200: {v['pnl_200']:+8.1f} $ | "
              f"капитал $800 → {v['capital_800']:7.1f} $ | средний убыток {v['avg_loss']:+5.1f}% | худшая {v['worst']:+5.1f}% | "
              f"самый глубокий минус внутри ставки {v['worst_min']:+5.1f}%")


if __name__ == "__main__":
    main()
