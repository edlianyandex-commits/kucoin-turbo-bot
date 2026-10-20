#!/usr/bin/env python3
"""Свечи KuCoin (на них бот торгует) против Binance (на них считались тесты) за последние сутки.

Пропуски минут у KuCoin, минуты без сделок и расхождение цен закрытия. Только публичные данные, без ключей.
  python3 candle_quality.py APT FIL ALGO ...
"""
import json
import sys
import time
import urllib.request

H = 3_600_000


def get(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except Exception as exc:
            err = exc
            time.sleep(1 + attempt)
    raise err


def kucoin(coin, start, end):
    sym = ("XBT" if coin == "BTC" else coin) + "USDTM"
    out, t = {}, start
    while t < end:
        t2 = min(end, t + 3 * H)                                  # KuCoin отдаёт не больше 200 свечей за запрос
        rows = get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1&from={t}&to={t2}")
        for r in rows.get("data") or []:
            out[int(r[0])] = (float(r[4]), float(r[5]))        # закрытие, объём
        t = t2
        time.sleep(0.12)
    return out


def binance(coin, start, end):
    out, t = {}, start
    while t < end:
        rows = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT&interval=1m&startTime={t}&endTime={end}&limit=1500")
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = float(r[4])
        t = int(rows[-1][0]) + 60_000
        time.sleep(0.12)
    return out


def main():
    end = int(time.time() // 60 * 60 * 1000) - 60_000
    start = end - 24 * H
    print("монета | минут у KuCoin из 1440 | без сделок | расхождение цены с Binance: среднее / максимум")
    for coin in sys.argv[1:]:
        try:
            k = kucoin(coin, start, end)
        except Exception as exc:
            print(f"{coin}: KuCoin недоступен ({exc})")
            continue
        try:
            b = binance(coin, start, end)
        except Exception as exc:
            b = {}
            note = f"Binance недоступен ({type(exc).__name__})"
        else:
            note = ""
        have = sum(1 for t in range(start, end, 60_000) if t in k)
        zero = sum(1 for t in range(start, end, 60_000) if t in k and k[t][1] == 0)
        diffs = [abs(k[t][0] / b[t] - 1) * 100 for t in k if t in b and b[t] > 0]
        d = f"{sum(diffs) / len(diffs):.3f}% / {max(diffs):.3f}%" if diffs else (note or "нет общих минут")
        print(f"{coin:6} | {have:4} ({1440 - have} пропусков) | {zero:4} | {d}")


if __name__ == "__main__":
    main()
