r"""Торговля по ночам (пользователь, 02.10.2026: «проанализируй важность торговли по ночам, все за и против»).

Оба бота с нынешней (своей) настройкой EMA Anchor, «реалистично», 07.2025–09.2026 (кривые — кэш turbo/v4_research.py):
  1) прибыль по часам суток (МСК = UTC+3): средний час, вклад в общий рост, доля ночи 00–08 МСК;
  2) риск по часам: падения ≥ 3% за час, худшие часы, начала больших просадок, выходы по обвалу BTC;
  3) ликвидность: оборот и размах цены наших 84 монет по часам (Binance, минутные свечи 07.2025–09.2026);
  4) грубая оценка «ночью без позиций» (закрывать всё в 00:00 МСК, снова торговать с 08:00): рост только в дневные часы
     минус закрытие каждый вечер (экспозиция в 00:00 МСК × 0,36%). Приближение: утром бот начинал бы без накопленных
     за ночь позиций, и дневные часы шли бы иначе.

    venv\Scripts\python turbo\night_study.py
Итог — turbo/results/night_study.json и таблицы.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reversal_signals as RS  # noqa: E402
import v4_research as VR  # noqa: E402

NIGHT = range(0, 8)                 # 00:00–08:00 МСК
MSK = pd.Timedelta(hours=3)


def curves():
    combo = json.load(open(VR.RESULT, encoding="utf-8"))["params"]["combo"]
    out = {}
    for bot, over in VR.bots_over().items():
        hr, expo = [], []
        for p in VR.PERIODS:
            r = VR.chain({**over, **combo}, p, VR.coins84(), "day", want_curve=True)
            if isinstance(r, str):
                raise SystemExit(f"{bot} {p}: {r}")
            h = r["curve"].resample("1h").last().ffill()
            hr.append(np.log(h).diff().dropna())
            expo.append(r["exposure"])
        out[bot] = (pd.concat(hr), pd.concat(expo))
    return out


def by_hour(lr):
    """Почасовые лог-доходности (индекс — начало часа UTC, resample label=left) → таблица по часу МСК."""
    start_msk = (lr.index + MSK).hour
    df = pd.DataFrame({"lr": lr.values, "h": start_msk})
    g = df.groupby("h")["lr"]
    tot = df["lr"].sum()
    return pd.DataFrame({"mean_pct": (np.exp(g.mean()) - 1) * 100, "share_pct": g.sum() / tot * 100,
                         "drop3": g.apply(lambda x: int((x < np.log(0.97)).sum())),
                         "worst_pct": (np.exp(g.min()) - 1) * 100})


def drawdowns(lr, min_dd=0.08):
    eq = np.exp(lr.cumsum())
    peak = eq.cummax()
    dd = 1 - eq / peak
    out, in_dd, start = [], False, None
    for t, v in dd.items():
        if not in_dd and v > 0:
            in_dd, start = True, t
        if in_dd and v == 0:
            seg = dd[start:t]
            if seg.max() >= min_dd:
                tr = seg.idxmax()
                h = lr[start:tr]
                worst = h.idxmin()
                out.append({"from": f"{start + MSK:%d.%m.%Y %H:%M}", "bottom": f"{tr + MSK:%d.%m %H:%M}",
                            "dd_pct": round(seg.max() * 100, 1), "worst_hour_msk": int((worst + MSK).hour),
                            "worst_hour_pct": round((np.exp(h.min()) - 1) * 100, 1)})
            in_dd = False
    return out


def flat_at_night(lr, expo):
    """Рост только в дневные часы; каждый вечер в 00:00 МСК (21:00 UTC) закрыть позиции: экспозиция × 0,36%."""
    start_msk = (lr.index + MSK).hour
    day = lr[~np.isin(start_msk, list(NIGHT))]
    closes = pd.date_range(lr.index[0].normalize() + pd.Timedelta(hours=21), lr.index[-1], freq="1D")
    e = expo.reindex(expo.index.union(closes)).ffill().reindex(closes).fillna(0.0)
    cost = np.log(1 - e.values * VR.ER.EXIT_COST).sum()
    return {"all_pct": round((np.exp(lr.sum()) - 1) * 100), "day_only_pct": round((np.exp(day.sum()) - 1) * 100),
            "flat_night_pct": round((np.exp(day.sum() + cost) - 1) * 100),
            "avg_exposure_at_midnight": round(float(e.mean()), 2), "closing_cost_pct": round((1 - np.exp(cost)) * 100, 1)}


def liquidity():
    """Оборот ($) и средний размах минутной свечи наших монет по часам МСК, к среднему за сутки."""
    vol, rng, n = np.zeros(24), np.zeros(24), np.zeros(24)
    for coin in VR.coins84():
        t0, d = RS.minutes(coin)
        if np.isnan(d[:, 2]).all():
            continue
        h = ((np.arange(len(d)) // 60) + int(t0.hour) + 3) % 24
        q = d[:, 3] * d[:, 2]
        r = (d[:, 0] - d[:, 1]) / d[:, 2]
        ok = ~np.isnan(q) & ~np.isnan(r)
        tot_q = np.nanmean(q[ok])
        tot_r = np.nanmean(r[ok])
        for k in range(24):
            m = ok & (h == k)
            if m.any():
                vol[k] += np.mean(q[m]) / tot_q
                rng[k] += np.mean(r[m]) / tot_r
                n[k] += 1
    return {"volume_rel": [round(x, 2) for x in vol / n], "range_rel": [round(x, 2) for x in rng / n]}


def main():
    C = curves()
    res = {}
    for bot, (lr, expo) in C.items():
        t = by_hour(lr)
        night = t.loc[list(NIGHT)]
        print(f"\n=== {bot}: по часам суток (МСК), «реалистично», 07.2025–09.2026, своя настройка")
        print("  час  средний час  доля роста  часов с падением ≥3%  худший час")
        for h, r in t.iterrows():
            print(f"  {h:02d}   {r.mean_pct:+.3f}%     {r.share_pct:5.1f}%      {int(r.drop3):3d}          {r.worst_pct:+.1f}%")
        fl = flat_at_night(lr, expo)
        halves = {}
        for name, (a, b) in (("H2 2025", ("2025-07-01", "2026-01-01")), ("H1 2026", ("2026-01-01", "2026-07-01")),
                             ("H2 2026", ("2026-07-01", "2026-10-01"))):
            x = lr[(lr.index >= a) & (lr.index < b)]
            n = x[np.isin((x.index + MSK).hour, list(NIGHT))]
            halves[name] = {"total_pct": round((np.exp(x.sum()) - 1) * 100, 1), "night_pct": round((np.exp(n.sum()) - 1) * 100, 1),
                            "day_pct": round((np.exp(x.sum() - n.sum()) - 1) * 100, 1), "night_share_pct": round(n.sum() / x.sum() * 100, 1)}
        print("  по периодам: " + "; ".join(f"{k}: всего {v['total_pct']:+.0f}%, ночные часы {v['night_pct']:+.0f}%, дневные {v['day_pct']:+.0f}% "
                                            f"(доля ночи {v['night_share_pct']:.0f}%)" for k, v in halves.items()))
        dds = drawdowns(lr)
        res[bot] = {"by_hour": {int(h): {k: round(float(v), 3) for k, v in r.items()} for h, r in t.iterrows()},
                    "night_share_pct": round(float(night.share_pct.sum()), 1),
                    "night_drop3": int(night.drop3.sum()), "all_drop3": int(t.drop3.sum()),
                    "flat_at_night": fl, "drawdowns_8pct": dds, "halves": halves}
        print(f"  ночь 00–08 МСК: доля роста {night.share_pct.sum():.1f}% (часов — 33%), падений ≥3% за час "
              f"{int(night.drop3.sum())} из {int(t.drop3.sum())}")
        print(f"  «ночью без позиций» (грубо): всё время {fl['all_pct']:+d}%, только дневные часы {fl['day_only_pct']:+d}%, "
              f"с закрытием каждый вечер {fl['flat_night_pct']:+d}% (экспозиция в полночь в среднем {fl['avg_exposure_at_midnight']}, "
              f"закрытия съели бы {fl['closing_cost_pct']}%)")
        print("  просадки ≥ 8%: " + "; ".join(f"{d['from']} → {d['bottom']} −{d['dd_pct']}% (худший час {d['worst_hour_msk']:02d}:00 МСК "
                                               f"{d['worst_hour_pct']:+.1f}%)" for d in dds))
    ex = []
    prev = False
    for t, v in VR.FLAG.items():
        if v and not prev and pd.Timestamp("2025-07-01") <= t < pd.Timestamp("2026-09-30"):
            ex.append(t)
        prev = bool(v)
    hrs = [int(((t + MSK).hour)) for t in ex]
    res["btc_exits_msk_hours"] = hrs
    print("\nВыходы по обвалу BTC (сигнал, МСК): " +", ".join(f"{(t + MSK):%d.%m %H:%M}" for t in ex)
          + f" — ночью (00–08) {sum(h in NIGHT for h in hrs)} из {len(hrs)}")
    lq = liquidity()
    res["liquidity"] = lq
    print("\nОборот наших монет по часам МСК (к среднему за сутки): " + "  ".join(f"{h:02d} {v:.2f}" for h, v in enumerate(lq["volume_rel"])))
    print("Размах минутной свечи по часам МСК (к среднему):       " + "  ".join(f"{h:02d} {v:.2f}" for h, v in enumerate(lq["range_rel"])))
    json.dump(res, open(os.path.join(VR.MV.RES, "night_study.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
