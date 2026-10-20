r"""Проверка ботов на отрезке полного честного периода (03.10.2026, пользователь: «проверим турбо 5.0 на годах 2025-2026»,
«сделаем так же проверку на 2026 году для супер турбо»). Месяцы из кэша полного периода (TB_FULL=1: список монет по
месяцам того времени, со снятыми с торгов), капитал с 1,0 в начале отрезка.

    set TB_FULL=1 && venv\Scripts\python turbo\subperiod_check.py
Итог: таблицы в журнал и results/subperiod_check.json.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

os.environ["TB_FULL"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import turnover_rule as T  # noqa: E402


def run(name, over, lm, first, last):
    lists = T.month_lists(lm)
    starts = {m: v for m, v in T.month_starts().items() if first <= m <= last}
    cap, pieces, monthly, exits = 1.0, [], [], 0
    for m, (t, me) in starts.items():
        r = T.month_job(over, lists[m], f"sub|{name}|{m}", t, me)
        if isinstance(r, str):
            return None
        c = r["curve"] * cap
        pieces.append(c)
        end = float(c.iloc[-1]) * r["close"]
        monthly.append((end / cap - 1) * 100)
        cap = end
        exits += r["exits"]
    c = pd.concat(pieces)
    c = c[~c.index.duplicated(keep="last")].sort_index()
    v = c.values
    ddv = 1 - v / np.maximum.accumulate(v)
    j = int(np.argmax(ddv))
    h = c.resample("1h").last().ffill()
    lo = c.resample("1h").min().reindex(h.index).ffill()
    days = pd.date_range(h.index[0].normalize() + pd.Timedelta(days=1), h.index[-1] - pd.Timedelta(days=30), freq="1D")
    r30, t2, half = [], [], []
    for d in days:
        b = h.asof(d)
        w = h[(h.index >= d) & (h.index <= d + pd.Timedelta(days=30))]
        wl = lo[(lo.index >= d) & (lo.index <= d + pd.Timedelta(days=30))]
        r30.append(float(w.iloc[-1] / b))
        t2.append(bool((w / b >= 2).any()))
        half.append(bool((wl / b <= 0.5).any()))
    r30 = np.array(r30) if r30 else np.array([1.0])
    mk = list(starts)
    years = {}
    for m, x in zip(mk, monthly):
        years[m[:4]] = years.get(m[:4], 1.0) * (1 + x / 100)
    return {"total": (cap - 1) * 100, "geo": (cap ** (1 / len(monthly)) - 1) * 100, "years": {k: (x - 1) * 100 for k, x in years.items()},
            "halves": T.halves_of(mk, monthly), "monthly": dict(zip(mk, [round(x, 1) for x in monthly])),
            "worst_month": min(monthly), "worst_m": mk[int(np.argmin(monthly))], "best_month": max(monthly), "neg": sum(x < 0 for x in monthly),
            "dd": float(ddv.max()) * 100, "dd_when": str(c.index[j])[:16], "exits": exits,
            "w_median": (np.median(r30) - 1) * 100, "w_neg": float(np.mean(r30 < 1)) * 100, "w_worst": (r30.min() - 1) * 100,
            "w_x2": float(np.mean(t2)) * 100 if t2 else 0.0, "w_half": float(np.mean(half)) * 100 if half else 0.0}


def table(title, rows):
    print(f"\n=== {title}")
    for name, r in rows.items():
        if not r:
            print(f"  {name}: нет данных")
            continue
        y = " / ".join(f"{k} {v:+.0f}%" for k, v in r["years"].items())
        hv = " / ".join(f"{v:+.0f}" for v in r["halves"].values())
        print(f"  {name:62s} итог {r['total']:+7.0f}% (в мес. {r['geo']:+5.1f}%) | {y} | полугодия {hv} | "
              f"в минусе {r['neg']} мес., худший {r['worst_month']:+.1f}% ({r['worst_m']}), лучший {r['best_month']:+.0f}% | "
              f"худшая минута −{r['dd']:.0f}% ({r['dd_when']}) | 30 дн.: медиана {r['w_median']:+.1f}%, в минусе {r['w_neg']:.0f}%, "
              f"худшие {r['w_worst']:+.0f}%, ×2 {r['w_x2']:.1f}%, −50% {r['w_half']:.1f}%", flush=True)
        print("      " + "  ".join(f"{m[2:]} {x:+.0f}%" for m, x in r["monthly"].items()))


def main():
    V = {**T.full_variants(), **T.protect_variants(), **T.smart_variants()}
    turbo = ["Turbo 5.0 (как сейчас)", "Turbo 5.0 + быстрый стоп по монете", "Turbo 5.0, экспозиция 6", "Turbo 5.0, экспозиция 4",
             "Turbo 5.0 + минутный выход: альты −6% за 60 мин", "Основной v4 (как сейчас)"]
    sup = ["Супер Турбо (как сейчас: сдвиг 0,143)", "Супер Турбо + быстрый стоп по монете", "Супер Турбо, экспозиция 6",
           "Супер Турбо, экспозиция 4", "Супер Турбо, сдвиг 0,229 (×0,8)", "Супер Турбо, сдвиг 0,286 (как до 03.10)",
           "Супер Турбо без снятых с торгов монет", "Turbo 5.0 (как сейчас)", "Turbo 5.0 + быстрый стоп по монете", "Основной v4 (как сейчас)"]
    out = {"turbo_2025_2026": {}, "super_2026": {}}
    for n in turbo:
        over, lm = V[n]
        out["turbo_2025_2026"][n] = run(n, over, lm, "2025-01", "2026-09")     # как в прогоне — из кэша
    table("Turbo 5.0 на 2025–2026 (01.2025–09.2026, 21 месяц)", out["turbo_2025_2026"])
    for n in sup:
        over, lm = V[n]
        out["super_2026"][n] = run(n, over, lm, "2026-01", "2026-09")
    table("Супер Турбо на 2026 (01–09.2026, 9 месяцев)", out["super_2026"])
    json.dump(out, open(os.path.join(HERE, "results", "subperiod_check.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
