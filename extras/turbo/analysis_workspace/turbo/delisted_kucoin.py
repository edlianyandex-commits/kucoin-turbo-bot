r"""Снятые с торгов фьючерсы KuCoin (для честной проверки 2024–2025, 03.10.2026): объявления KuCoin о делистинге
(api/v3/announcements, annType=delistings, все страницы) → названия контрактов «XXXUSDT» в заголовках про фьючерсы →
есть ли по ним дневные свечи в API фьючерсов KuCoin (kline/query) и были ли они на Binance (архив data.binance.vision).

    venv\Scripts\python turbo\delisted_kucoin.py
Итог: turbo/results/delisted_kucoin.json — {монета: {"symbol", "announced", "kucoin_days", "binance": bool}}.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results", "delisted_kucoin.json")


def get(url, raw=False, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tb-research"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status if raw else json.load(r)
        except urllib.error.HTTPError as e:
            if raw:
                return e.code
            if k == tries - 1:
                raise
        except Exception:
            if k == tries - 1:
                raise
        time.sleep(2 + 2 * k)


def announcements(since="2023-10-01"):
    """Все объявления о делистинге с since: API без времени отдаёт только последний месяц — идём окнами по 30 дней."""
    out, seen = [], set()
    t_end = int(time.time() * 1000)
    t_min = int(time.mktime(time.strptime(since, "%Y-%m-%d")) * 1000)
    while t_end > t_min:
        t_start = t_end - 30 * 86_400_000
        page = 1
        while True:
            d = get("https://api.kucoin.com/api/v3/announcements?annType=delistings&lang=en_US&pageSize=50"
                    f"&currentPage={page}&startTime={t_start}&endTime={t_end}")
            items = (d.get("data") or {}).get("items") or []
            for a in items:
                key = (a.get("annId"), a.get("annTitle"))
                if key not in seen:
                    seen.add(key)
                    out.append({"t": int(a.get("cTime") or 0), "title": str(a.get("annTitle") or "")})
            if not items or page >= int((d.get("data") or {}).get("totalPage") or 1):
                break
            page += 1
            time.sleep(0.3)
        t_end = t_start
        time.sleep(0.3)
    return out


def main():
    ann = announcements()
    print(f"объявлений о делистинге: {len(ann)}, самое раннее: {time.strftime('%Y-%m-%d', time.gmtime(min(a['t'] for a in ann) / 1000))}")
    coins = {}
    for a in ann:
        title = a["title"]
        if not re.search(r"futures|perpetual", title, re.I):
            continue
        for sym in re.findall(r"\b([0-9A-Z]{2,20})USDTM?\b", title):
            c = sym
            coins.setdefault(c, {"announced": a["t"], "title": title[:160]})
    print(f"монет в объявлениях о фьючерсах: {len(coins)}")
    active = {r["coin"] for r in json.load(open(os.path.join(HERE, "results", "kucoin_contracts_2026-10-01.json"), encoding="utf-8"))}
    res = {}
    for i, (c, info) in enumerate(sorted(coins.items())):
        sym = ("XBT" if c == "BTC" else c) + "USDTM"
        t1 = info["announced"] + 40 * 86_400_000
        t0 = t1 - 190 * 86_400_000
        try:
            d = get(f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity=1440&from={t0}&to={t1}")
            days = len(d.get("data") or [])
            turn = sorted(float(r[6]) for r in (d.get("data") or []))
            med = turn[len(turn) // 2] if turn else 0.0
            mx = turn[-1] if turn else 0.0
        except Exception as e:                       # noqa: BLE001
            days, med, mx = -1, 0.0, 0.0
            info["error"] = repr(e)[:80]
        m = time.strftime("%Y-%m", time.gmtime(info["announced"] / 1000))
        st = get(f"https://data.binance.vision/data/futures/um/monthly/klines/{c}USDT/1d/{c}USDT-1d-{m}.zip", raw=True)
        res[c] = {**info, "symbol": sym, "kucoin_days": days, "turnover_median": med, "turnover_max": mx,
                  "binance": st == 200, "still_active": c in active}
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(coins)}", flush=True)
        time.sleep(0.2)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    gone = {c: r for c, r in res.items() if not r["still_active"]}
    print(f"снято с KuCoin (нет среди живых 01.10.2026): {len(gone)}; со свечами KuCoin: {sum(r['kucoin_days'] > 0 for r in gone.values())}; "
          f"с оборотом ≥ 2,5 млн $ хотя бы день: {sum(r['turnover_max'] >= 2.5e6 for r in gone.values())}; "
          f"из них были на Binance: {sum(r['turnover_max'] >= 2.5e6 and r['binance'] for r in gone.values())}")
    for c, r in sorted(gone.items(), key=lambda kv: -kv[1]["turnover_max"])[:25]:
        print(f"  {c:12s} объявлено {time.strftime('%Y-%m-%d', time.gmtime(r['announced'] / 1000))} | свечей {r['kucoin_days']:4d} | "
              f"оборот медиана {r['turnover_median'] / 1e6:6.2f} млн, макс {r['turnover_max'] / 1e6:7.2f} млн | Binance {'да' if r['binance'] else 'нет'}")


if __name__ == "__main__":
    main()
