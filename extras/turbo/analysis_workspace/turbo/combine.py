"""Сводит шансы по двум периодам (2025 и 2025–2026) в один файл для панели: turbo/results/episodes_combined.json."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from episodes import summarize  # noqa: E402

A = json.load(open(os.path.join(HERE, "results", "episodes_2025.json"), encoding="utf-8"))
B = json.load(open(os.path.join(HERE, "results", "episodes.json"), encoding="utf-8"))
out = {}
for idx in sorted(set(A) & set(B), key=int):
    eps = A[idx]["episodes_detail"] + B[idx]["episodes_detail"]
    s = summarize(B[idx]["name"], eps)
    s["periods"] = {"2025": {k: A[idx][k] for k in ("p_double", "p_burn", "mean_pct", "episodes")},
                    "2025-26": {k: B[idx][k] for k in ("p_double", "p_burn", "mean_pct", "episodes")}}
    out[idx] = s
    print(f"№{idx:<2} {s['name']:<44} отрезков {s['episodes']:2d} | удвоил {s['p_double']:5.1f}% | сгорел {s['p_burn']:5.1f}% | "
          f"−50%+ {s['p_loss50']:5.1f}% | средний {s['mean_pct']:+6.1f}% | медиана {s['median_pct']:+6.1f}% | "
          f"2025: ×2 {A[idx]['p_double']:.0f}%/сгор {A[idx]['p_burn']:.0f}%/ср {A[idx]['mean_pct']:+.0f}% | "
          f"2025-26: ×2 {B[idx]['p_double']:.0f}%/сгор {B[idx]['p_burn']:.0f}%/ср {B[idx]['mean_pct']:+.0f}%")
with open(os.path.join(HERE, "results", "episodes_combined.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
