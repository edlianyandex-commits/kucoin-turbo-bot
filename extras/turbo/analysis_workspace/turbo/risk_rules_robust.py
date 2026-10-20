"""Разбор turbo/results/risk_rules.json: какие изменения к «Умному ва-банку» (×2, SMA200) помогают
в ОБА периода (2022–24 и 2025–26), а не только в одном. Без новых расчётов."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open(os.path.join(HERE, "results", "risk_rules.json"), encoding="utf-8"))
key = lambda r: (tuple(r["entry"]), tuple(tuple(x) if isinstance(x, list) else x for x in r["exit"]))  # noqa: E731
for r in rows:
    r["entry"] = tuple(r["entry"])
    r["exit"] = tuple(tuple(x) if isinstance(x, list) else x for x in r["exit"])
BASE_EN, BASE_EX = (200, None, None, None), (2.0, None, None, None)
base = next(r for r in rows if r["entry"] == BASE_EN and r["exit"] == BASE_EX)


def label(en, ex):
    sma, vol, fnd, r7 = en
    target, crash, trail, stop = ex
    p = [f"цель ×{target}" if target else "без цели"]
    p += [f"SMA{sma}"] if sma else ["без фильтра тренда"]
    p += [f"волатильность ≤{vol}×"] if vol else []
    p += [f"финансирование ≤{fnd * 100:.2f}%"] if fnd is not None else []
    p += ["BTC 7 дн. ≥−8%"] if r7 is not None else []
    p += [f"выход при обвале BTC −{crash[1] * 100:.0f}%/{crash[0]}ч"] if crash else []
    p += [f"защита прибыли ×{trail[0]}/откат {trail[1] * 100:.0f}%"] if trail else []
    p += [f"стоп ×{stop}"] if stop else []
    return ", ".join(p)


def fmt(s):
    return (f"рост {s['growth_25']:+5.2f}%/мес | ср.ставка {s['mean_bet']:+6.1f}% | ×цель {s['p_target']:4.1f}% | "
            f"сгорело {s['p_burn']:4.1f}% | потеря прибыли {s['p_giveback']:4.1f}% | −50%+ {s['p_loss50']:4.1f}% | ставок {s['bets']}")


def show(title, rs):
    print(f"\n=== {title}")
    for r in rs:
        print(f"  {label(r['entry'], r['exit'])}")
        print(f"     2022–24: {fmt(r['train'])}")
        print(f"     2025–26: {fmt(r['test'])}")
        print(f"     всё:     {fmt(r['all'])}")


show("СЕЙЧАС", [base])
# одна мера поверх текущих правил
single = []
for r in rows:
    en, ex = r["entry"], r["exit"]
    diff = sum(a != b for a, b in zip(en, BASE_EN)) + sum(a != b for a, b in zip(ex, BASE_EX))
    if diff == 1:
        single.append(r)
single.sort(key=lambda r: -min(r["train"]["growth_25"], r["test"]["growth_25"]))
show("Одна мера поверх текущих правил (по худшему из двух периодов)", single[:12])


def ok(r):
    t, s = r["train"], r["test"]
    bt, bs = base["train"], base["test"]
    return (t["growth_25"] >= bt["growth_25"] * 0.95 and s["growth_25"] >= bs["growth_25"] * 0.95
            and t["p_burn"] <= bt["p_burn"] and s["p_burn"] <= bs["p_burn"]
            and t["p_giveback"] <= bt["p_giveback"] and s["p_giveback"] <= bs["p_giveback"]
            and (t["p_burn"] < bt["p_burn"] or s["p_burn"] < bs["p_burn"]
                 or t["p_giveback"] < bt["p_giveback"] or s["p_giveback"] < bs["p_giveback"]))


robust = sorted([r for r in rows if ok(r)],
                key=lambda r: (r["all"]["p_burn"] + r["all"]["p_giveback"], -r["all"]["growth_25"]))
show(f"Устойчивые: в ОБА периода рост не ниже 95% текущего, сгорает и теряет прибыль не чаще (найдено {len(robust)})",
     robust[:10])
