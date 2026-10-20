"""Funding основного бота по ставкам KuCoin (где он и торгует) против Binance — по сделкам цепочек turbo/main_variants.py
(turbo/results/main_variants_fills/<вариант>_<период>.csv.gz). История ставок KuCoin — api-futures /contract/funding-rates
(по 100 записей назад от «to»), кэш turbo/results/funding_kucoin.json.

    venv\\Scripts\\python turbo\\funding_check.py [вариант ...]
"""
import glob
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import main_variants as MV  # noqa: E402
import risk_rules as RR  # noqa: E402
import universe_chain as UC  # noqa: E402

KU = os.path.join(MV.RES, "funding_kucoin.json")


def kucoin_funding(coins):
    data = json.load(open(KU)) if os.path.exists(KU) else {}
    sym = {r["coin"]: r["symbol"] for r in json.load(open(os.path.join(MV.RES, "kucoin_contracts_2026-10-01.json")))}
    s = RR.http()
    t0 = int(pd.Timestamp("2025-06-25", tz="UTC").timestamp() * 1000)
    for c in coins:
        if c in data or c not in sym:
            continue
        rows, to = {}, int(pd.Timestamp("2026-10-01", tz="UTC").timestamp() * 1000)
        while to > t0:
            for attempt in range(5):
                try:
                    batch = s.get("https://api-futures.kucoin.com/api/v1/contract/funding-rates",
                                  params={"symbol": sym[c], "from": t0, "to": to}, timeout=20).json().get("data") or []
                    break
                except Exception:
                    time.sleep(2 * (attempt + 1))
            else:
                batch = []
            new = [(int(x["timepoint"]), float(x["fundingRate"])) for x in batch if int(x["timepoint"]) not in rows]
            if not new:
                break
            rows.update(new)
            to = min(t for t, _ in new) - 1
            time.sleep(0.15)
        data[c] = sorted(rows.items())
        print(f"  {c}: {len(rows)} ставок", flush=True)
    json.dump(data, open(KU, "w"))
    return data


def cost(fills, fund):
    """Плата за funding по отрезкам цепочки: доля капитала (Π по выплатам) и разбивка по монетам (% капитала отрезка)."""
    mult, by_coin = 1.0, {}
    for _, seg in fills.groupby("seg"):
        stake = MV.STAKE
        seg = seg.assign(t=pd.to_datetime(seg["timestamp"]))
        t_a, t_b = seg["t"].min(), seg["t"].max()
        events = []
        for c, g in seg.groupby("coin"):
            pos = pd.Series((g["psize"] * g["pprice"]).values, index=g["t"])
            pos = pos[~pos.index.duplicated(keep="last")]
            fr = fund.get(c) or []
            ft = pd.to_datetime([x[0] for x in fr], unit="ms")
            rate = np.array([x[1] for x in fr])
            m = (ft >= t_a) & (ft <= t_b)
            if not m.any():
                continue
            held = pos.reindex(pos.index.union(ft[m])).ffill().reindex(ft[m]).fillna(0.0).values
            pay = held * rate[m]
            by_coin[c] = by_coin.get(c, 0.0) + float(pay.sum()) / stake * 100
            events.append(pd.Series(pay, index=ft[m]))
        if events:
            for p in pd.concat(events).groupby(level=0).sum().values:
                mult *= 1 - p / stake            # приближённо: на стартовый капитал отрезка
    return (mult - 1) * 100, by_coin


def main():
    names = sys.argv[1:] or ["base"]
    coins = sorted({c for p in glob.glob(os.path.join(MV.FILLS, "*.csv.gz")) for c in pd.read_csv(p, usecols=["coin"])["coin"].unique()})
    ku = kucoin_funding(coins)
    bn = json.load(open(MV.FUND))
    for n in names:
        for i, period in enumerate(UC.PERIODS):
            path = os.path.join(MV.FILLS, f"{n}_{i}.csv.gz")
            if not os.path.exists(path):
                continue
            f = pd.read_csv(path)
            (cb, bb), (ck, bk) = cost(f, bn), cost(f, ku)
            print(f"{n:<9} {period:<28} плата Binance {cb:+6.1f}% · KuCoin {ck:+6.1f}% капитала (− = бот платит)")
            top = sorted(bk.items(), key=lambda x: x[1])[:6]
            print("    больше всего получено (KuCoin / Binance, % капитала): " +
                  ", ".join(f"{c} {-v:.1f}/{-bb.get(c, 0):.1f}" for c, v in top))
            print("    больше всего уплачено: " + ", ".join(f"{c} {v:.1f}/{bb.get(c, 0):.1f}"
                                                         for c, v in sorted(bk.items(), key=lambda x: -x[1])[:4]))


if __name__ == "__main__":
    main()
