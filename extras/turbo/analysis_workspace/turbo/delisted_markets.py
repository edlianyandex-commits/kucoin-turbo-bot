r"""Снятые с торгов фьючерсы KuCoin в исследовательских прогонах TB Core (03.10.2026): движок берёт правила контрактов из
caches/kucoin/markets.json (только живые контракты) и свечи — с Binance по caches/binance/markets.json; снятые монеты он
молча пропускает. ensure() дописывает в оба файла описания снятых контрактов (по API KuCoin /api/v1/contracts/{symbol},
который отдаёт и закрытые) и освежает время файла, чтобы движок не перезагрузил его с биржи (кэш живёт 24 ч).
Только для исследований; живой бот этих файлов не касается (на сервере свой кэш).

Список снятых — turbo/delisted_kucoin.py → results/delisted_kucoin.json; описания — results/delisted_contracts.json.
"""
import copy
import json
import os
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIST = os.path.join(HERE, "results", "delisted_kucoin.json")
SPECS = os.path.join(HERE, "results", "delisted_contracts.json")
KU = os.path.join(ROOT, "caches", "kucoin", "markets.json")
BI = os.path.join(ROOT, "caches", "binance", "markets.json")
LOCK = threading.Lock()
_last = [0.0]


def coins():
    d = json.load(open(LIST, encoding="utf-8")) if os.path.exists(LIST) else {}
    return sorted(c for c, r in d.items() if not r.get("still_active") and r.get("kucoin_days", 0) > 0)


def specs():
    have = json.load(open(SPECS, encoding="utf-8")) if os.path.exists(SPECS) else {}
    for c in coins():
        if c in have:
            continue
        req = urllib.request.Request(f"https://api-futures.kucoin.com/api/v1/contracts/{c}USDTM", headers={"User-Agent": "tb-research"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30)).get("data")
        except Exception:                      # noqa: BLE001
            d = None
        if d:
            have[c] = d
        time.sleep(0.2)
    json.dump(have, open(SPECS, "w", encoding="utf-8"), ensure_ascii=False)
    return have


def _ku_entry(tpl, c, sp):
    m = copy.deepcopy(tpl)
    m.update({"id": sp["symbol"], "symbol": f"{c}/USDT:USDT", "base": c, "baseId": c, "lowercaseId": None, "active": True,
              "contractSize": float(sp.get("multiplier") or 1.0), "created": sp.get("firstOpenDate")})
    m["precision"] = {"amount": float(sp.get("lotSize") or 1), "price": float(sp.get("tickSize") or 0.0001)}
    m["limits"] = {"leverage": {"min": 1.0, "max": float(sp.get("maxLeverage") or 10)},
                   "amount": {"min": float(sp.get("lotSize") or 1), "max": float(sp.get("maxOrderQty") or 1e6)},
                   "price": {"min": float(sp.get("tickSize") or 0.0001), "max": float(sp.get("maxPrice") or 1e6)},
                   "cost": {"min": None, "max": None}}
    m["info"] = dict(sp)
    return m


def _bi_entry(tpl, c, sp):
    m = copy.deepcopy(tpl)
    m.update({"id": f"{c}USDT", "symbol": f"{c}/USDT:USDT", "base": c, "baseId": c, "lowercaseId": f"{c.lower()}usdt", "active": True})
    tick = float(sp.get("tickSize") or 0.0001)
    m["precision"] = {**m.get("precision", {}), "amount": 1.0, "price": tick}
    m["limits"] = {**m.get("limits", {}), "amount": {"min": 1.0, "max": 1e7}, "price": {"min": tick, "max": 1e7},
                   "market": {"min": 1.0, "max": 1e7}}
    info = dict(m.get("info") or {})
    info.update({"symbol": f"{c}USDT", "pair": f"{c}USDT", "baseAsset": c, "status": "TRADING",
                 "onboardDate": str(sp.get("firstOpenDate") or 0)})
    m["info"] = info
    return m


def ensure(force=False):
    """Дописать снятые контракты в кэш рынков движка (раз в 10 минут или при force)."""
    with LOCK:
        if not force and time.time() - _last[0] < 600:
            return
        sp = specs()
        for path, tpl_key, make in ((KU, "SOL/USDT:USDT", _ku_entry), (BI, "SOL/USDT:USDT", _bi_entry)):
            if not os.path.exists(path):
                continue
            d = json.load(open(path, encoding="utf-8"))
            tpl = d.get(tpl_key)
            if not tpl:
                continue
            changed = False
            for c, s in sp.items():
                key = f"{c}/USDT:USDT"
                if key not in d:
                    d[key] = make(tpl, c, s)
                    changed = True
            if changed:
                tmp = path + ".tmp"
                json.dump(d, open(tmp, "w", encoding="utf-8"))
                os.replace(tmp, path)
            os.utime(path, None)               # свежий файл — движок не станет перезагружать рынки с биржи
        first_timestamps(sp)
        _last[0] = time.time()


FT = os.path.join(ROOT, "caches", "first_ohlcv_timestamps_unified.json")
FTX = os.path.join(ROOT, "caches", "first_ohlcv_timestamps_unified_exchange_specific.json")
FTS = os.path.join(ROOT, "caches", "first_ohlcv_timestamps_unified_exchange_specific_symbols.json")


def first_timestamps(sp):
    """Дата первой свечи по биржам: у снятой монеты движок не может спросить KuCoin и пишет 0 → монета для него «не на
    KuCoin» и не торгуется. Ставим дату открытия контракта KuCoin (firstOpenDate); для Binance — её, если своей нет."""
    if not os.path.exists(FTX):
        return
    u = json.load(open(FT, encoding="utf-8")) if os.path.exists(FT) else {}
    x = json.load(open(FTX, encoding="utf-8"))
    sy = json.load(open(FTS, encoding="utf-8")) if os.path.exists(FTS) else {}
    changed = False
    for c, s in sp.items():
        fo = float(s.get("firstOpenDate") or 0)
        if fo <= 0:
            continue
        e = x.setdefault(c, {})
        if not e.get("kucoin"):
            e["kucoin"] = fo
            changed = True
        if not e.get("binanceusdm"):
            e["binanceusdm"] = fo
            changed = True
        if not u.get(c):
            u[c] = min(v for v in e.values() if v)
            changed = True
        if c not in sy:
            sy[c] = {"binanceusdm": f"{c}/USDT:USDT", "kucoin": f"{c}/USDT:USDT"}
            changed = True
    if changed:
        for path, d in ((FT, u), (FTX, x), (FTS, sy)):
            tmp = path + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8"))
            os.replace(tmp, path)


if __name__ == "__main__":
    ensure(force=True)
    print("снятых монет:", len(coins()), "описаний:", len(json.load(open(SPECS, encoding="utf-8"))))
