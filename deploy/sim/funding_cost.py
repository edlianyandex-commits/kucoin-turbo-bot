#!/usr/bin/env python3
"""Сколько стоило бы финансирование (funding) — прогоны passivbot его не учитывают.

По каждому replay (make_replays.py): объём позиций по времени (из сделок) × реальные ставки финансирования
KuCoin Futures в моменты расчётов. Лонг платит при положительной ставке, получает при отрицательной.
Только публичные данные KuCoin, без ключей. Только стандартная библиотека.
  python3 funding_cost.py replays/turbo5_pause.json ...
"""
import json
import sys
import time
import urllib.request
from collections import defaultdict

API = "https://api-futures.kucoin.com/api/v1/contract/funding-rates"


def kucoin_symbol(coin):
    return ("XBT" if coin == "BTC" else coin) + "USDTM"


def funding_history(coin, start_ms, end_ms):
    """[(время, ставка)] за период; запрашиваем кусками по 5 дней."""
    out, t = {}, start_ms
    while t < end_ms:
        t2 = min(end_ms, t + 5 * 86_400_000)
        url = f"{API}?symbol={kucoin_symbol(coin)}&from={t}&to={t2}"
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    data = json.load(r).get("data") or []
                break
            except Exception:
                time.sleep(1 + attempt)
                data = []
        for row in data if isinstance(data, list) else []:
            out[int(row["timepoint"])] = float(row["fundingRate"])
        t = t2
        time.sleep(0.15)
    return sorted(out.items())


def main():
    for path in sys.argv[1:]:
        rp = json.load(open(path, encoding="utf-8"))
        fills = rp["fills"]                            # [ts, coin, type, qty, price, psize, pprice, pnl, fee, we]
        start, end = rp["equity"][0][0], rp["equity"][-1][0]
        # размер позиции у KuCoin — в контрактах (множитель у каждой монеты свой), поэтому объём считаем
        # через загрузку позиции (wallet_exposure = объём / баланс), которую passivbot пишет в каждую сделку
        bal_by_hour = {e[0] // 3_600_000: e[1] for e in rp["equity"]}

        def balance_at(ts):
            h = ts // 3_600_000
            while h not in bal_by_hour and h > start // 3_600_000:
                h -= 1
            return bal_by_hour.get(h, rp["equity"][0][1])
        by_coin = defaultdict(list)                    # coin → [(ts, объём после сделки, USDT)]
        for ts, coin, _ty, _q, _p, ps, _pp, _pnl, _fee, we in fills:
            by_coin[coin].append((ts, (we if ps > 1e-12 else 0.0) * balance_at(ts)))
        paid, received, per_coin = 0.0, 0.0, {}
        exposure_hours = 0.0
        for coin, steps in by_coin.items():
            hist = funding_history(coin, start, end)
            cost, i, notional = 0.0, 0, 0.0
            for tp, rate in hist:
                while i < len(steps) and steps[i][0] <= tp:
                    notional = steps[i][1]
                    i += 1
                c = notional * rate
                cost += c
                if c > 0:
                    paid += c
                else:
                    received += -c
            per_coin[coin] = (round(cost, 3), len(hist))
        # средняя загрузка (объём позиций к капиталу) — по часам
        eq_by_hour = {e[0] // 3_600_000: e[2] for e in rp["equity"]}
        notional_now, hours = defaultdict(float), 0
        idx = sorted((ts, coin, n) for coin, steps in by_coin.items() for ts, n in steps)
        j = 0
        for h in range(start // 3_600_000, end // 3_600_000 + 1):
            while j < len(idx) and idx[j][0] <= h * 3_600_000:
                notional_now[idx[j][1]] = idx[j][2]
                j += 1
            eq = eq_by_hour.get(h)
            if eq:
                exposure_hours += sum(notional_now.values()) / eq
                hours += 1
        st, m = rp["stats"], rp["meta"]
        net = paid - received
        days = st["days"]
        pnl = st["end_equity"] - st["start_equity"]
        print(f"=== {m['label']}: {m['start']} — {m['end']} ({days:.0f} дн.), экспозиция до {m['twel']}, "
              f"средняя загрузка {exposure_hours / max(1, hours):.2f}× капитала")
        print(f"  финансирование: заплачено {paid:.2f}, получено {received:.2f}, итого {net:+.2f} USDT "
              f"= {net / st['start_equity'] * 100:+.1f}% стартового капитала "
              f"({net / st['start_equity'] * 100 / days * 30:+.1f}% в пересчёте на 30 дней)")
        print(f"  результат прогона без финансирования: {pnl:+.2f} USDT ({pnl / st['start_equity'] * 100:+.1f}%) → "
              f"с финансированием ≈ {pnl - net:+.2f} USDT ({(pnl - net) / st['start_equity'] * 100:+.1f}%)")
        top = sorted(per_coin.items(), key=lambda x: -abs(x[1][0]))[:5]
        print("  больше всего:", ", ".join(f"{c} {v[0]:+.2f} ({v[1]} расчётов)" for c, v in top))


if __name__ == "__main__":
    main()
