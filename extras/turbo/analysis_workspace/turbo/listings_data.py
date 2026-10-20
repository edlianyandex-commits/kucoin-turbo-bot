"""Данные для идеи «шорт новых листингов» (Turbo, 01.10.2026): все бессрочные USDT-фьючерсы Binance, вышедшие на торги
с 01.12.2024 по 15.09.2026 (по onboardDate из exchangeInfo — там есть и снятые с торгов со статусом SETTLING/CLOSE),
часовые свечи первых 90 дней и история ставок финансирования. Публичные данные Binance.

    venv\\Scripts\\python turbo\\listings_data.py
Итог: turbo/results/listings_binance.npz (+ listings_meta.json).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import risk_rules as RR  # noqa: E402

OUT = os.path.join(HERE, "results", "listings_binance.npz")
META = os.path.join(HERE, "results", "listings_meta.json")
H = 3_600_000
DAYS = 90
FROM = int(datetime(2024, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
TO = int(datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp() * 1000)
NOW = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)


def main():
    s = RR.http()
    info = s.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=30).json()
    syms = [x for x in info["symbols"] if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT"
            and FROM <= int(x.get("onboardDate", 0)) <= TO]
    print(f"новых бессрочных USDT-фьючерсов {len(syms)} (статусы: "
          f"{ {st: sum(1 for x in syms if x['status'] == st) for st in set(x['status'] for x in syms)} })", flush=True)
    meta, arrays = [], {}
    for k, x in enumerate(syms):
        sym, onboard = x["symbol"], int(x["onboardDate"])
        end = min(onboard + DAYS * 86_400_000, NOW)
        rows, t = [], onboard - H
        while t < end:
            r = s.get("https://fapi.binance.com/fapi/v1/klines",
                      params={"symbol": sym, "interval": "1h", "startTime": t, "endTime": end, "limit": 1500}, timeout=20).json()
            if not isinstance(r, list) or not r:
                break
            rows += [[int(z[0]), float(z[1]), float(z[2]), float(z[3]), float(z[4]), float(z[7])] for z in r]
            t = r[-1][0] + H
            time.sleep(0.05)
        fr, t = [], onboard - H
        while t < end:
            r = s.get("https://fapi.binance.com/fapi/v1/fundingRate",
                      params={"symbol": sym, "startTime": t, "endTime": end, "limit": 1000}, timeout=20).json()
            if not isinstance(r, list) or not r:
                break
            fr += [[int(z["fundingTime"]), float(z["fundingRate"])] for z in r]
            t = r[-1]["fundingTime"] + 1
            time.sleep(0.05)
        if len(rows) < 48:
            continue
        arrays[f"k_{sym}"] = np.array(rows, dtype=np.float64)
        arrays[f"f_{sym}"] = np.array(fr if fr else np.zeros((0, 2)), dtype=np.float64)
        meta.append({"symbol": sym, "onboard": onboard, "status": x["status"], "candles": len(rows), "funding": len(fr)})
        if k % 25 == 0:
            print(f"  {k + 1}/{len(syms)} {sym}", flush=True)
    np.savez_compressed(OUT, **arrays)
    json.dump(meta, open(META, "w"), indent=1)
    print(f"сохранено {len(meta)} листингов", flush=True)


if __name__ == "__main__":
    main()
