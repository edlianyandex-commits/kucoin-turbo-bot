"""Turbo 3.0: правила ставки на ставках подряд («как в жизни») — цель/стоп, выход по сроку у «потолка боковика»,
дробление ставки по датам. Только 2026 год, каждая половина отдельно.

1) Один раз: настоящий прогон passivbot (движок 3.0, капитал поминутно, $200) со старта каждого дня 2026 —
   turbo/results/daily_curves/<день>.npy + минута стопа passivbot «без перезапуска» (daily_panic.json).
2) Дальше цепочки собираются из этих кривых: ставка → выход по правилам → следующая со следующего дня
   (после стопа passivbot — через 30 дней). Итог цепочки — сумма $ при ставке $200 (как turbo/sequence_2026.py).
Правила выхода (самое раннее событие): сгорела, цель ×T, стоп ставки ×S, обвал BTC (−5% за 12 ч), стоп passivbot,
выход по сроку: за W дней до срока X — «откат» (капитал на δ ниже максимума с начала окна) или «верх» (капитал
на максимуме последних L часов); в момент X — закрыть в любом случае. Выход — худшая из 2 следующих минут минус 2%.

    venv\\Scripts\\python turbo\\chain_rules.py [--workers 6]
Итог: turbo/results/chain_rules.json и таблицы в консоли.
"""
import argparse
import glob
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coin_pick as CP  # noqa: E402
import drawdown_rules as DR  # noqa: E402
import drawdown_study as DS  # noqa: E402
import paper_window as PW  # noqa: E402
import risk_rules as RR  # noqa: E402

STAKES = (200, 100, 50)                     # $200 и части при дроблении — у KuCoin минимальный ордер 1 контракт


def _suffix(stake, horizon):
    return ("" if stake == 200 else f"_{stake}") + ("" if horizon == 30 else f"_h{horizon}")


def curves_dir(stake, horizon=30):
    """horizon — длина прогона в днях: у живой панели срока нет, 30 дней — допущение исследований, 60 — проверка «без срока»."""
    return os.path.join(HERE, "results", "daily_curves" + _suffix(stake, horizon))


def panic_file(stake, horizon=30):
    return os.path.join(HERE, "results", "daily_panic" + _suffix(stake, horizon) + ".json")


DAYS = CP.grid("2026-01-01", "2026-09-28", step=1)
DATA_END = "2026-09-30"
HALVES = {"I пол. 2026": ("2026-01-01", "2026-07-01"), "II пол. 2026": ("2026-07-01", DATA_END)}
STARTS = {"I пол. 2026": CP.grid("2026-01-01", "2026-01-15", 1), "II пол. 2026": CP.grid("2026-07-01", "2026-07-15", 1)}
COST, DELAY = DS.COST, DS.DELAY
MC_PATHS, MC_SEED = 300, {"I пол. 2026": 1, "II пол. 2026": 2}


def build_one(day, stake, horizon=30):
    path = os.path.join(curves_dir(stake, horizon), f"{day}.npy")
    if os.path.exists(path):
        return day, None, "cached"
    end = min(datetime.fromisoformat(day) + timedelta(days=horizon), datetime.fromisoformat(DATA_END)).strftime("%Y-%m-%d")
    res = DS.backtest_curve(dict(PW.ENGINE3), day, end, f"dc{stake}{_suffix(stake, horizon)}_{day}")
    if isinstance(res, str):
        return day, None, res
    rel, run_dir = res
    panic = None
    try:
        f = pd.read_csv(sorted(glob.glob(os.path.join(run_dir, "*", "*", "fills.csv")))[-1])
        p = f[f["type"] == "close_panic_long"]
        if len(p):
            m = int((pd.Timestamp(p["timestamp"].iloc[0]) - pd.Timestamp(day)).total_seconds() // 60)
            panic = m if m < len(rel) - 5 else None          # закрытие в конце данных — не стоп
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(curves_dir(stake, horizon), exist_ok=True)
    np.save(path, rel)
    return day, panic, "ok"


def build(workers, stake, horizon=30):
    DS.EP.STAKE = float(stake)                  # backtest_curve берёт размер ставки отсюда
    pf = panic_file(stake, horizon)
    panic = json.load(open(pf)) if os.path.exists(pf) else {}
    todo = [d for d in DAYS if not os.path.exists(os.path.join(curves_dir(stake, horizon), f"{d}.npy"))]
    with ThreadPoolExecutor(workers) as ex:
        for day, p, st in ex.map(lambda d: build_one(d, stake, horizon), todo):
            if st == "ok":
                panic[day] = p
            elif st != "cached":
                print("  ошибка", day, st[:150], flush=True)
    json.dump(panic, open(pf, "w"))
    return panic


class Bets:
    """Готовые кривые ставок по дням + флаги обвала BTC."""

    def __init__(self, panic, stake=200, horizon=30):
        self.btc = RR.btc_hourly()
        self.panic = panic
        self.horizon = horizon
        cd = curves_dir(stake, horizon)
        self.rel = {d: np.load(os.path.join(cd, f"{d}.npy")) for d in DAYS if os.path.exists(os.path.join(cd, f"{d}.npy"))}
        self.crash = {d: DR.crash_minutes(self.btc, d, len(r), 12, 0.05) for d, r in self.rel.items()}
        self.memo = {}

    def exit_cached(self, day, r):
        k = (day, json.dumps(r, sort_keys=True))
        if k not in self.memo:
            self.memo[k] = self.exit(day, r)
        return self.memo[k]

    def exit(self, day, r):
        """(минута выхода или None, итог доли, причина, был ли стоп passivbot до выхода)."""
        rel = self.rel[day]
        n = len(rel)
        ev = {"burn": DS.first(rel <= 0.10)}
        if r["target"]:
            ev["target"] = DS.first(rel >= r["target"])
        if r["stop"]:
            ev["stop"] = DS.first(rel <= r["stop"])
        if r.get("crash", True):
            ev["crash"] = DS.first(self.crash[day])
        pm = self.panic.get(day)
        if pm is not None:
            ev["hsl"] = pm
        X = r.get("deadline")
        full = n >= self.horizon * 1440 - 72       # кривая не оборвана ликвидацией или концом данных
        if X and (X * 1440 < n - 1 or (X == self.horizon and full)):
            xm = min(X * 1440, n - 1)              # срок = длина прогона: закрытие в конце как «30 дней», без расходов
            mode, w = r.get("mode"), int(r.get("window", 0) * 1440)
            a = xm - w if mode else xm
            seg = rel[a:xm]
            hit = None
            if mode == "trail" and len(seg):          # откат на delta от максимума с начала окна
                peak = np.maximum.accumulate(seg)
                hit = DS.first(seg <= peak * (1 - r["delta"]))
            elif mode == "top" and len(seg):          # капитал на максимуме последних L часов («верх боковика»)
                L = int(r["lookback_h"] * 60)
                hist = pd.Series(rel[:xm]).rolling(L, min_periods=1).max().shift(1).values[a:xm]
                hit = DS.first(seg >= hist)           # NaN на первой минуте ставки — сравнение ложно
            if hit is not None:
                ev["time"] = a + hit
            elif xm < n - 1:
                ev["time"] = xm
        ev = {k: v for k, v in ev.items() if v is not None}
        if not ev:
            if n < self.horizon * 1440 * 0.95 and rel[-1] < 0.5:           # движок оборвал прогон — ликвидация
                return None, -0.95, "burn", False
            return None, float(rel[-1]) - 1, "end", pm is not None
        order = ["burn", "target", "stop", "crash", "hsl", "time"]
        kind = min(ev, key=lambda k: (ev[k], order.index(k)))
        i = ev[kind]
        if kind == "burn":
            return i, float(rel[i]) - 1, kind, pm is not None and pm <= i
        if kind == "hsl":                       # стоп passivbot уже закрыл позиции — капитал на этой минуте
            return i, float(rel[min(i + DELAY, n - 1)]) - 1, kind, True
        return i, float(rel[i:i + DELAY + 1].min()) * (1 - COST) - 1, kind, pm is not None and pm <= i

    def chain(self, r, first, last, stake=200.0, offset_days=0, rng=None):
        """Ставки подряд с first+offset_days до last; rng — случайный перерыв 1–4 дня между ставками
        (пути с соседних дат иначе быстро сходятся в один)."""
        d = datetime.fromisoformat(first) + timedelta(days=offset_days)
        last_d = datetime.fromisoformat(last)
        pnl, n, kinds = 0.0, 0, []
        while d < last_d:
            day = d.strftime("%Y-%m-%d")
            if day not in self.rel:
                d += timedelta(days=1)
                continue
            if self.crash[day][0]:                  # обвал BTC идёт — панель не запустит ставку
                d += timedelta(days=1)
                continue
            i, res, kind, latched = self.exit_cached(day, r)
            end_m = (len(self.rel[day]) - 1) if i is None else i
            t_end = d + timedelta(minutes=end_m)
            if t_end > last_d:                      # ставка вышла за границу полугодия — считаем по последней минуте внутри
                cut = int((last_d - d).total_seconds() // 60)
                res, kind = float(self.rel[day][max(0, cut - 1)]) - 1, "граница"
                pnl += stake * res
                n += 1
                kinds.append(kind)
                break
            pnl += stake * res
            n += 1
            kinds.append(kind)
            gap = (30 if latched else 1) + (int(rng.integers(0, 4)) if rng is not None else 0)
            d = (t_end + timedelta(days=gap)).replace(hour=0, minute=0, second=0, microsecond=0)
        return pnl, n, kinds


def summarize(bets, r, stagger=1, step_days=7):
    """bets — Bets для ставки 200/stagger (кривые прогнаны на этом размере ставки)."""
    out = {}
    for half, (first, last) in HALVES.items():
        vals = []
        for s in STARTS[half]:
            tot = 0.0
            for k in range(stagger):                 # stagger частей по 200/stagger, старты через step_days
                tot += bets.chain(r, s, last, 200.0 / stagger, k * step_days)[0]
            vals.append(tot)
        out[half] = {"mean": round(float(np.mean(vals)), 1), "min": round(float(np.min(vals)), 1),
                     "max": round(float(np.max(vals)), 1), "pos": int(sum(v > 0 for v in vals)), "n": len(vals),
                     "sd": round(float(np.std(vals)), 1)}
        # 300 случайных путей: старт в первые 15 дней, перерыв между ставками 1–4 дня (одинаковые случайные числа для всех правил)
        rng, mc = np.random.default_rng(MC_SEED[half]), []
        for _ in range(MC_PATHS):
            s0 = (datetime.fromisoformat(first) + timedelta(days=int(rng.integers(0, 15)))).strftime("%Y-%m-%d")
            mc.append(sum(bets.chain(r, s0, last, 200.0 / stagger, k * step_days, rng)[0] for k in range(stagger)))
        mc = np.array(mc)
        out[half].update({"mc_mean": round(float(mc.mean()), 1), "mc_p10": round(float(np.percentile(mc, 10)), 1),
                          "mc_p90": round(float(np.percentile(mc, 90)), 1), "mc_pos": round(float((mc > 0).mean()) * 100, 1)})
        # каждая ставка отдельно: старт с каждого дня полугодия (без обвала BTC на старте)
        days = [d for d in DAYS if first <= d < last and d in bets.rel and not bets.crash[d][0]]
        res = np.array([bets.exit_cached(d, r)[1] for d in days])
        out[half].update({"bet_mean": round(float(res.mean()) * 100, 1), "bet_loss": round(float((res < 0).mean()) * 100, 1),
                          "bet_n": len(res), "bet_res": [round(float(x), 4) for x in res]})
    return out


def paired_ci(diff, seed, block=7, n=2000):
    """Средняя разница (п.п.) и 90% интервал бутстрэпом по блокам из block соседних стартов."""
    blocks = [diff[i:i + block] for i in range(0, len(diff), block)]
    rng = np.random.default_rng(seed)
    bs = [np.concatenate([blocks[j] for j in rng.integers(0, len(blocks), len(blocks))]).mean() for _ in range(n)]
    return [round(float(diff.mean()) * 100, 2), round(float(np.percentile(bs, 5)) * 100, 2), round(float(np.percentile(bs, 95)) * 100, 2)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-build", action="store_true", help="только готовые кривые; дробление — если есть кривые $100/$50")
    a = ap.parse_args()
    by_stake = {}
    for st in STAKES:
        if a.no_build and len(glob.glob(os.path.join(curves_dir(st), "*.npy"))) < len(DAYS):
            continue
        panic = build(a.workers, st)
        by_stake[st] = Bets(panic, st)
        print(f"${st}: кривых {len(by_stake[st].rel)} дней 2026; стопов passivbot «без перезапуска»: "
              f"{sum(v is not None for v in panic.values())}", flush=True)
    bets = by_stake[200]
    base = {"target": 2.0, "stop": 0.7}
    rows = []

    def add(group, name, r, stagger=1, step_days=7, curves=None):
        rows.append({"group": group, "name": name, "rules": r, "stagger": stagger, "step_days": step_days,
                     **summarize(by_stake[curves or 200 // stagger], r, stagger, step_days)})

    add("сейчас", "3.0: ×2, стоп −30%, выход при обвале BTC", base)
    for t in (1.5, 1.75, 2.0, 2.5, 3.0):
        for s in (0.65, 0.7, 0.75):
            if (t, s) != (2.0, 0.7):
                add("цель/стоп", f"×{t:g}, стоп −{round((1 - s) * 100)}%", {"target": t, "stop": s})
    for X in (10, 14, 21, 30):
        add("срок", f"срок {X} дн., закрыть в момент срока", {**base, "deadline": X})
        for W in (2, 4):
            for delta in (0.03, 0.06):
                add("срок", f"срок {X} дн., за {W} дн. до срока — откат {delta * 100:g}% от пика окна",
                    {**base, "deadline": X, "mode": "trail", "window": W, "delta": delta})
            for L in (24, 48):
                add("срок", f"срок {X} дн., за {W} дн. до срока — верх диапазона {L} ч",
                    {**base, "deadline": X, "mode": "top", "window": W, "lookback_h": L})
    for k in (2, 4):
        for step in (3, 7) if 200 // k in by_stake else ():
            add("дробление", f"ставка $200 = {k} части по ${200 // k}, старты через {step} дн.", base, k, step)
        for step in (3, 7):   # то же без помехи минимального ордера: кривые $200, итог в масштабе $200 (как {k}×$200 из ${k * 200})
            add("дробление", f"{k} ставки по $200 из ${k * 200} (итог ÷{k}), старты через {step} дн.", base, k, step, curves=200)
    if len(glob.glob(os.path.join(curves_dir(200, 60), "*.npy"))) >= len(DAYS):
        # у живой панели срока нет: сравнение со ставкой «без срока» на прогонах до 60 дней
        by_stake["h60"] = Bets(json.load(open(panic_file(200, 60))), 200, 60)
        add("срок, прогон 60 дн.", "без срока (как живая панель; прогон до 60 дн.)", base, curves="h60")
        for X in (30, 45):
            add("срок, прогон 60 дн.", f"срок {X} дн., закрыть в момент срока", {**base, "deadline": X}, curves="h60")
            for W in (2, 4):
                add("срок, прогон 60 дн.", f"срок {X} дн., за {W} дн. — верх диапазона 24 ч",
                    {**base, "deadline": X, "mode": "top", "window": W, "lookback_h": 24}, curves="h60")
                add("срок, прогон 60 дн.", f"срок {X} дн., за {W} дн. — откат 3% от пика окна",
                    {**base, "deadline": X, "mode": "trail", "window": W, "delta": 0.03}, curves="h60")
    json.dump(rows, open(os.path.join(HERE, "results", "chain_rules.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    H1, H2 = "I пол. 2026", "II пол. 2026"
    first_of = {}
    for r in rows:
        first_of.setdefault(r["group"], r)

    def base_of(r):
        return first_of[r["group"]] if r["group"] == "срок, прогон 60 дн." else rows[0]

    def better(r):
        """Лучше базы в обеих половинах и по 300 случайным путям, и в среднем на отдельную ставку."""
        b = base_of(r)
        return r is not b and all(r[h]["mc_mean"] > b[h]["mc_mean"] and r[h]["bet_mean"] > b[h]["bet_mean"] for h in (H1, H2))

    for r in rows:       # разница с базой на одних и тех же стартах; 90% — бутстрэп по неделям (соседние старты похожи)
        b = base_of(r)
        for h in (H1, H2):
            x, y = np.array(r[h]["bet_res"]), np.array(b[h]["bet_res"])
            if r is b or len(x) != len(y):
                continue
            r[h]["bet_diff"] = paired_ci(x - y, MC_SEED[h])
    json.dump(rows, open(os.path.join(HERE, "results", "chain_rules.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def line(r):
        x, y = r[H1], r[H2]
        return (f"{'★' if better(r) else ' '} {r['name'][:60]:<60} | I: пути {x['mc_mean']:+6.0f} $ [{x['mc_p10']:+5.0f}..{x['mc_p90']:+5.0f}] "
                f"в плюсе {x['mc_pos']:3.0f}%, ставка {x['bet_mean']:+5.1f}% | II: пути {y['mc_mean']:+6.0f} $ "
                f"[{y['mc_p10']:+5.0f}..{y['mc_p90']:+5.0f}] в плюсе {y['mc_pos']:3.0f}%, ставка {y['bet_mean']:+5.1f}%"
                + "".join(f"\n{'':64}  Δ ставки к базе, {h.split()[0]}: {d[0]:+5.1f} п.п. (90%: {d[1]:+.1f}..{d[2]:+.1f})"
                          for h in (H1, H2) if (d := r[h].get("bet_diff"))))
    print("\nСтавки подряд по $200, 300 случайных путей на половину 2026 (старт в первые 15 дней, перерыв 1–4 дня);\n"
          "[..] — 10% худших / 10% лучших путей; «ставка» — средний итог отдельной ставки со старта каждого дня;\n"
          "★ — лучше 3.0 в обеих половинах и по путям, и по отдельным ставкам\n")
    for g in first_of:
        print(f"=== {g}" + (" (★ — относительно «без срока»)" if g == "срок, прогон 60 дн." else ""))
        grp = [r for r in rows if r["group"] == g]
        b = rows[0] if g != "срок, прогон 60 дн." else first_of[g]
        for r in sorted(grp, key=lambda r: -min(r[H1]["mc_mean"] - b[H1]["mc_mean"], r[H2]["mc_mean"] - b[H2]["mc_mean"])):
            print("  " + line(r))


if __name__ == "__main__":
    main()
