r"""Данные для движка «разгона» (Супер Турбо, 03.10.2026): 5-минутные свечи и ставки финансирования Binance USDⓈ-M
из публичного архива data.binance.vision (помесячные zip; есть и по снятым с торгов монетам) для всех монет, которые
хоть один день 07.2025–09.2026 проходили порог ликвидности KuCoin (оборот вчерашних суток по свечам ≥ 2,5 млн $,
results/kucoin_turnover_daily.json) — плюс месяц до этого (история для сигналов) и BTC.

Остаточная ошибка выжившего: список кандидатов — контракты KuCoin, живые на 01.10.2026 (снятые раньше не видны).

    venv\Scripts\python turbo\super_data.py [--workers 8]
Итог: turbo/results/super/<COIN>.npz (t — начало свечи, мс; o h l c — float32; fund_t, fund_r) и index.json.
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "results", "super")
VISION = "https://data.binance.vision/data/futures/um/monthly"
FIRST, LAST = "2025-07-01", "2026-10-01"


def coin_months():
    import turnover_rule as T
    df = T.table()
    el = df.shift(1) >= T.MIN_T                       # проходит порог по обороту вчерашних суток
    el = el[(el.index >= FIRST) & (el.index < LAST)]
    need = {}
    for c in el.columns:
        days = el.index[el[c]]
        if not len(days):
            continue
        ms = set()
        for d in days:
            ms.add(d.strftime("%Y-%m"))
            ms.add((d.replace(day=1) - pd.Timedelta(days=1)).strftime("%Y-%m"))
        need[c] = sorted(ms)
    need["BTC"] = sorted({m.strftime("%Y-%m") for m in pd.date_range("2025-06-01", "2026-09-01", freq="MS")})
    return need


def get(url, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tb-research"})
            return urllib.request.urlopen(req, timeout=60).read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if k == tries - 1:
                raise
        except Exception:
            if k == tries - 1:
                raise
        time.sleep(2 + 3 * k)


def read_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        txt = z.read(z.namelist()[0]).decode()
    first = txt.split("\n", 1)[0]
    return pd.read_csv(io.StringIO(txt), header=0 if not first[0].isdigit() else None)


def fetch(item):
    coin, months = item
    path = os.path.join(OUT, f"{coin}.npz")
    if os.path.exists(path):
        return coin, "есть"
    sym = f"{coin}USDT"
    parts, fund, missing = [], [], []
    for m in months:
        raw = get(f"{VISION}/klines/{sym}/5m/{sym}-5m-{m}.zip")
        if raw is None:
            missing.append(m)
            continue
        k = read_zip(raw)
        parts.append(k.iloc[:, :5].to_numpy(dtype="float64"))
        raw = get(f"{VISION}/fundingRate/{sym}/{sym}-fundingRate-{m}.zip")
        if raw is not None:
            f = read_zip(raw)
            fund.append(f.iloc[:, [0, 2]].to_numpy(dtype="float64"))
    if not parts:
        return coin, f"нет данных ({', '.join(missing)})"
    a = np.concatenate(parts)
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.r_[True, np.diff(a[:, 0]) > 0]]
    f = np.concatenate(fund) if fund else np.zeros((0, 2))
    np.savez_compressed(path, t=a[:, 0].astype("int64"), o=a[:, 1].astype("float32"), h=a[:, 2].astype("float32"),
                        l=a[:, 3].astype("float32"), c=a[:, 4].astype("float32"),
                        fund_t=f[:, 0].astype("int64"), fund_r=f[:, 1].astype("float32"))
    return coin, f"{len(a)} свечей" + (f", нет месяцев: {', '.join(missing)}" if missing else "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--reverse", action="store_true", help="с конца списка (второй процесс параллельно первому)")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    need = coin_months()
    print(f"монет {len(need)}, монето-месяцев {sum(map(len, need.values()))}", flush=True)
    res = {}
    with ThreadPoolExecutor(a.workers) as ex:
        items = list(need.items())[::-1] if a.reverse else list(need.items())
        for i, (coin, msg) in enumerate(ex.map(fetch, items), 1):
            res[coin] = msg
            if "нет" in msg or i % 50 == 0:
                print(f"  {i}/{len(need)} {coin}: {msg}", flush=True)
    if not a.reverse:
        json.dump({"need": need, "result": res}, open(os.path.join(OUT, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("готово")
