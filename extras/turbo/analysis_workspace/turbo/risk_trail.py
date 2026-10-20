"""Турбо №15 + стоп ставки −40% панелью: защита прибыли (после ×A выход при откате G от пика) и цель ставки."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import risk_compare as RC  # noqa: E402
import risk_rules as RR  # noqa: E402

data = json.load(open(os.path.join(RR.RES, "episodes_risk.json"), encoding="utf-8"))
starts = sorted({e["start"] for e in data["15"]["episodes_detail"] if "outcome" in e})
btc, fund = RR.btc_hourly(), RR.btc_funding()
eps = [e for e in RC.curves(15, starts) if RR.entry_features(btc, fund, e["start"])["sma"][200]]
print(f"ставок с фильтром SMA200: {len(eps)}")
out = []
for target in (2.0, 1.75, 1.5):
    for trail in (None, (1.3, 0.2), (1.3, 0.3), (1.5, 0.2), (1.5, 0.3), (1.5, 0.4)):
        if trail and target <= trail[0]:
            continue
        for stop in (None, 0.6):
            per = {"2022–24": [], "2025–26": []}
            for e in eps:
                per["2022–24" if e["start"] < RR.SPLIT else "2025–26"].append(RR.outcome(e["rel"], None, target, trail, stop))
            s = {k: RR.summarize(v) for k, v in per.items()}
            s["всё"] = RR.summarize(per["2022–24"] + per["2025–26"])
            lab = (f"цель ×{target}, стоп {'−40% панелью' if stop else 'только passivbot'}, защита "
                   + (f"после ×{trail[0]} откат {trail[1] * 100:.0f}%" if trail else "—"))
            out.append((lab, s))
for lab, s in sorted(out, key=lambda x: -x[1]["всё"]["growth_25"]):
    a, b, c = s["2022–24"], s["2025–26"], s["всё"]
    print(f"{lab:<62} | рост {a['growth_25']:+5.2f} / {b['growth_25']:+5.2f} / {c['growth_25']:+5.2f} | "
          f"сгорело {c['p_burn']:4.1f}% | потеря прибыли {a['p_giveback']:4.1f} / {b['p_giveback']:4.1f} / {c['p_giveback']:4.1f}% | "
          f"−50%+ {c['p_loss50']:4.1f}% | ×цель {c['p_target']:4.1f}% | ср. ставка {c['mean_bet']:+5.1f}%")
print("(рост и потеря прибыли: 2022–24 / 2025–26 / всё)")
