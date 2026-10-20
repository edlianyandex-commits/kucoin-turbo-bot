r"""Все цифры по Супер Турбо на полном периоде 01.2024–09.2026 (03.10.2026, пользователь: «нужны все цифры по супер
турбо, все наблюдения, подключим 2024 год»). Читает turnover_rule.json["full"] (команда TB_FULL=1 turnover_rule.py full),
пересобирает поминутные цепочки из кэша отрезков и пишет отчёт в C:\tradebot v4\боты\Супер Турбо — все цифры.md.

    set TB_FULL=1 && venv\Scripts\python turbo\super_full_report.py
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

OUT_MD = r"C:\tradebot v4\боты\Супер Турбо — все цифры.md"
SUPER = "Супер Турбо (как сейчас: сдвиг 0,143)"
TURBO = "Turbo 5.0 (как сейчас)"
V4 = "Основной v4 (как сейчас)"


def chain(name):
    """Поминутная цепочка варианта по месяцам (из кэша) + выходы по BTC и средняя загрузка."""
    over, lm = T.full_variants()[name]
    lists = T.month_lists(lm)
    starts = T.month_starts()
    cap, pieces, monthly, exits, expo = 1.0, [], [], 0, []
    for m, (t, me) in starts.items():
        r = T.month_job(over, lists[m], f"rep|{name}|{m}", t, me)
        if isinstance(r, str):
            raise RuntimeError(f"{name} {m}: {r[:200]}")
        c = r["curve"] * cap
        pieces.append(c)
        end = float(c.iloc[-1]) * r["close"]
        monthly.append((end / cap - 1) * 100)
        cap = end
        exits += r["exits"]
        x = r.get("exposure")
        if x is not None and len(x):
            xx = x[(x.index >= t) & (x.index < me)]
            if len(xx):
                dur = np.diff(np.r_[xx.index.values.astype("int64"), np.int64(me.value)]) / 6e10
                expo.append((float(np.sum(xx.values * dur)), float(np.sum(dur)), float(xx.max())))
    c = pd.concat(pieces)
    c = c[~c.index.duplicated(keep="last")].sort_index()
    return c, monthly, list(starts), exits, expo, {m: len(lists[m]) for m in starts}


def windows(c):
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
    r30 = np.array(r30)
    return {"n": len(r30), "median": (np.median(r30) - 1) * 100, "neg": float(np.mean(r30 < 1)) * 100, "worst": (r30.min() - 1) * 100,
            "best": (r30.max() - 1) * 100, "x2": float(np.mean(t2)) * 100, "half": float(np.mean(half)) * 100,
            "p50": float(np.mean(r30 >= 1.5)) * 100}


def regimes(month_keys):
    """Режим рынка по месяцу: медиана месячного изменения цен монет из списка этого месяца (дневные закрытия KuCoin)."""
    hl = json.load(open(T.HL, encoding="utf-8"))
    lists = T.month_lists(T.MONTHLY)
    out = {}
    for m in month_keys:
        a = pd.Timestamp(m + "-01")
        b = a + pd.offsets.MonthBegin(1)
        ra = []
        for c in lists[m]:
            d = hl.get(c) or {}
            ka = (a - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            kb = (b - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if ka in d and kb in d and d[ka][2] > 0:
                ra.append(d[kb][2] / d[ka][2] - 1)
        med = float(np.median(ra)) * 100 if ra else 0.0
        out[m] = ("рост" if med > 5 else ("падение" if med < -5 else "боковик"), med)
    return out


def years(month_keys, monthly):
    y = {}
    for m, x in zip(month_keys, monthly):
        k = m[:4] if m[:4] != "2026" else "2026 (9 мес.)"
        y[k] = y.get(k, 1.0) * (1 + x / 100)
    return {k: (v - 1) * 100 for k, v in y.items()}


def main():
    full = json.load(open(T.OUTF, encoding="utf-8"))["full"]
    data = {}
    for name in (SUPER, TURBO, V4, "Супер Турбо, сдвиг 0,229 (×0,8)", "Супер Турбо, сдвиг 0,286 (как до 03.10)",
                 "Супер Турбо, издержки «средне»", "Супер Турбо без снятых с торгов монет"):
        if name not in full or not full[name].get("total") and full[name].get("total") != 0:
            continue
        c, monthly, mk, exits, expo, ncoins = chain(name)
        data[name] = {"c": c, "monthly": monthly, "mk": mk, "exits": exits, "expo": expo, "ncoins": ncoins,
                      "w": windows(c), "res": full[name], "years": years(mk, monthly)}
        print("готово:", name, flush=True)
    s = data[SUPER]
    reg = regimes(s["mk"])
    L = []
    w = L.append
    r = s["res"]
    tot = r["total"]
    geo = ((1 + tot / 100) ** (1 / len(s["monthly"])) - 1) * 100
    w("# Супер Турбо — все цифры и наблюдения (03.10.2026)\n")
    w("Честная проверка на **33 месяцах: январь 2024 – сентябрь 2026**, $300, «как на реальных деньгах» (запас исполнения "
      "0,02%, проскальзывание 0,2%, выход при обвале BTC по худшей из 5 минут). Список монет собирается **каждый месяц по "
      "обороту KuCoin того времени**, вместе с монетами, которые потом **сняли с торгов** (164 контракта; их правила и даты "
      "подложены движку). Свечи — Binance (как у движка всегда). Настройка Супер Турбо подбиралась на 07.2025–09.2026, "
      "поэтому **01.2024–06.2025 (18 месяцев) — проверка на данных, которых подбор не видел**.\n")
    w("## Настройка\n")
    w("EMA Anchor (TB Core), только лонг; 5 монет, экспозиция 9 (плечо 10), на монету ≤ 1,8 депозита (предел доли); первая "
      "покупка 7,5% доли (≈ 40 $ при 300 $); сдвиг продажи от размера позиции 0,143; средние 332 / 476 мин; докупка ×2,94; "
      "выход при обвале BTC (−5% за 12 ч) с паузой до следующего дня; фильтр разогнанных монет ×3; стоп TB Core 35%; защита от "
      "делистинга; список монет раз в месяц.\n")
    w("## Главное\n")
    w(f"| | значение |\n|---|---|\n| Итог за 33 месяца | **{tot:+,.0f}%** (×{1 + tot / 100:,.1f}) |\n"
      f"| В среднем в месяц (геом.) | {geo:+.1f}% |")
    for k, v in s["years"].items():
        w(f"| {k} | {v:+,.0f}% |")
    for k, v in r["halves"].items():
        w(f"| {k} | {v:+,.0f}% |")
    mm = s["monthly"]
    w(f"| Месяцев в минусе | {sum(x < 0 for x in mm)} из {len(mm)} |\n| Худший месяц | {min(mm):+.1f}% ({s['mk'][int(np.argmin(mm))]}) |\n"
      f"| Лучший месяц | {max(mm):+.1f}% ({s['mk'][int(np.argmax(mm))]}) |\n| Медиана месяца | {np.median(mm):+.1f}% |\n"
      f"| Худшая минута (просадка от пика) | **−{r['dd_min']:.1f}%** ({r.get('dd_when', '—')} UTC) |\n"
      f"| Капитал / объём позиций в эту минуту | ≈ {(r.get('equity_to_notional') or 0) * 100:.1f}% (ликвидация ≈ 1–2%) |\n"
      f"| Выходов при обвале BTC | {s['exits']} |")
    ww = s["w"]
    w(f"| 30-дневные окна (старт каждый день, {ww['n']}) — медиана | {ww['median']:+.1f}% |\n| Окон в минусе | {ww['neg']:.0f}% |\n"
      f"| Худшие 30 дней / лучшие | {ww['worst']:+.0f}% / {ww['best']:+.0f}% |\n| Удвоение за 30 дней (касание ×2) | {ww['x2']:.1f}% окон |\n"
      f"| +50% за 30 дней | {ww['p50']:.0f}% окон |\n| Падение на половину внутри 30 дней | {ww['half']:.1f}% окон |")
    if s["expo"]:
        e = s["expo"]
        w(f"| Средняя загрузка (объём позиций / баланс) | {sum(x[0] for x in e) / sum(x[1] for x in e):.2f}× (максимум {max(x[2] for x in e):.1f}×) |")
    w("")
    w("## По месяцам\n")
    w("| месяц | монет в списке | рынок (медиана альтов) | Супер Турбо | Turbo 5.0 | основной v4 |\n|---|---|---|---|---|---|")
    tm = data.get(TURBO, {}).get("monthly", [None] * len(mm))
    vm = data.get(V4, {}).get("monthly", [None] * len(mm))
    for i, m in enumerate(s["mk"]):
        g, med = reg[m]
        w(f"| {m} | {s['ncoins'][m]} | {g} ({med:+.0f}%) | {mm[i]:+.1f}% | " + (f"{tm[i]:+.1f}%" if tm[i] is not None else "—") + " | "
          + (f"{vm[i]:+.1f}%" if vm[i] is not None else "—") + " |")
    w("")
    w("## Рынок: рост / боковик / падение\n")
    w("| режим | месяцев | Супер Турбо | Turbo 5.0 | основной v4 |\n|---|---|---|---|---|")
    for g in ("рост", "боковик", "падение"):
        idx = [i for i, m in enumerate(s["mk"]) if reg[m][0] == g]
        if not idx:
            continue
        cell = lambda arr: f"{np.mean([arr[i] for i in idx]):+.1f}% (в минусе {sum(arr[i] < 0 for i in idx)})" if arr and arr[0] is not None else "—"  # noqa: E731
        w(f"| {g} | {len(idx)} | {cell(mm)} | {cell(tm)} | {cell(vm)} |")
    w("")
    w("## Сравнение вариантов (тот же период и та же проверка)\n")
    w("| вариант | итог | по годам | худший месяц | худшая минута | касание ×2 за 30 дн. | 30 дн. в минусе |\n|---|---|---|---|---|---|---|")
    for name, d in data.items():
        y = " / ".join(f"{v:+,.0f}" for v in d["years"].values())
        w(f"| {name} | {d['res']['total']:+,.0f}% | {y}% | {min(d['monthly']):+.1f}% | −{d['res']['dd_min']:.0f}% | {d['w']['x2']:.1f}% | {d['w']['neg']:.0f}% |")
    w("")
    cp = r.get("coin_pnl") or {}
    if cp:
        w("## Монеты, давшие больше всего (вклад, доли капитала начала месяца, сумма по месяцам)\n")
        w(", ".join(f"{c} {v:+.1f}" for c, v in list(cp.items())[:12]) + "\n")
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))
    summary = {n: {"total": d["res"]["total"], "years": d["years"], "halves": d["res"]["halves"], "dd": d["res"]["dd_min"],
                   "dd_when": d["res"].get("dd_when"), "worst_month": min(d["monthly"]), "windows": d["w"], "exits": d["exits"],
                   "monthly": dict(zip(d["mk"], [round(x, 2) for x in d["monthly"]]))} for n, d in data.items()}
    json.dump({"summary": summary, "regimes": reg}, open(os.path.join(HERE, "results", "super_full_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
