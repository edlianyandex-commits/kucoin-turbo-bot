r"""Движок «разгона» для Супер Турбо (03.10.2026, пользователь: «Супер Турбо — настоящий бот для разгона депозита»,
цель — месяцы ×2, пусть и нестабильно). Только исследование: боты не меняются.

Идея — «положительная асимметрия» вместо усреднения EMA Anchor: почти всё время в рынке на 1–3 монетах с крупными
позициями (вместе ≤ 9 депозитов при плече 10), покупать то, что уже растёт, резать убыток скользящим стопом, давать
прибыли расти (докупка в плюс).

Данные — turbo/super_data.py (5-мин. свечи и финансирование Binance). Честно: список монет на каждый день — те, у кого
оборот KuCoin за вчерашние сутки ≥ 2,5 млн $ (в т.ч. потом рухнувшие SIREN, PIPPIN…), сигналы только по прошлым часам.

Модель исполнения: вход/выход по сигналу — рыночный ордер по закрытию часа ± проскальзывание 0,1% + комиссия 0,06%;
стоп — внутри 5-мин. свечи по уровню (если свеча открылась ниже — по открытию) − 0,2%; финансирование — по реальным
ставкам Binance с объёма; ликвидация — капитал по худшим ценам 5-мин. свечи ≤ 1% объёма → счёт обнулён; выход при
обвале BTC (закрытие часа на 5% ниже максимума за 12 ч) → всё закрыть, пауза до 00:00 UTC следующего дня.

    venv\Scripts\python turbo\super_engine.py prep            — матрицы (results/super/mat_*.npy)
    venv\Scripts\python turbo\super_engine.py scan [--workers 11]   — сетка вариантов → results/super_scan.json
"""
import argparse
import itertools
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DIR = os.path.join(HERE, "results", "super")
T0 = pd.Timestamp("2025-06-01")
T1 = pd.Timestamp("2026-10-01")
N5 = int((T1 - T0) / pd.Timedelta(minutes=5))
NH = N5 // 12
SIM_FROM = pd.Timestamp("2025-07-01")
FEE, SLIP, SLIP_STOP, MM = 0.0006, 0.001, 0.002, 0.01


# ---------------------------------------------------------------- подготовка матриц
def prep():
    import turnover_rule as T
    idx = json.load(open(os.path.join(DIR, "index.json"), encoding="utf-8"))
    coins = sorted(c for c in idx["need"] if os.path.exists(os.path.join(DIR, f"{c}.npz")))
    coins.remove("BTC")
    coins = ["BTC"] + coins
    nc = len(coins)
    t0ms = int(T0.timestamp() * 1000)
    mats = {k: np.lib.format.open_memmap(os.path.join(DIR, f"mat_{k}.npy"), mode="w+", dtype="float32", shape=(nc, N5))
            for k in "ohlc"}
    for m in mats.values():
        m[:] = np.nan
    fund = {}
    for i, c in enumerate(coins):
        z = np.load(os.path.join(DIR, f"{c}.npz"))
        j = (z["t"] - t0ms) // 300_000
        ok = (j >= 0) & (j < N5)
        for k in "ohlc":
            mats[k][i, j[ok]] = z[k][ok]
        fj = (z["fund_t"] - t0ms) // 300_000
        fok = (fj >= 0) & (fj < N5)
        fund[c] = [fj[fok].tolist(), z["fund_r"][fok].astype(float).tolist()]
    for m in mats.values():
        m.flush()
    c5 = mats["c"]
    h5, l5 = mats["h"], mats["l"]
    ch = np.full((nc, NH), np.nan, dtype="float32")
    hh = np.full((nc, NH), np.nan, dtype="float32")
    lh = np.full((nc, NH), np.nan, dtype="float32")
    for i in range(nc):
        r = np.asarray(c5[i]).reshape(NH, 12)
        ch[i] = r[:, -1]
        with np.errstate(all="ignore"):
            hh[i] = np.nanmax(np.asarray(h5[i]).reshape(NH, 12), axis=1)
            lh[i] = np.nanmin(np.asarray(l5[i]).reshape(NH, 12), axis=1)
    np.save(os.path.join(DIR, "h1_c.npy"), ch)
    np.save(os.path.join(DIR, "h1_h.npy"), hh)
    np.save(os.path.join(DIR, "h1_l.npy"), lh)
    # список на день: оборот KuCoin за вчерашние сутки ≥ порога
    df = T.table()
    days = pd.date_range(T0, T1 - pd.Timedelta(days=1), freq="D")
    el = (df.shift(1) >= T.MIN_T).reindex(days).fillna(False)
    elig = np.zeros((nc, len(days)), dtype=bool)
    for i, c in enumerate(coins):
        if c in el.columns:
            elig[i] = el[c].values
    np.save(os.path.join(DIR, "elig.npy"), elig)
    lev = {r["coin"]: r["maxLeverage"] for r in json.load(open(T.CONTRACTS, encoding="utf-8"))}
    json.dump({"coins": coins, "fund": fund, "maxlev": [lev.get(c, 10) for c in coins]},
              open(os.path.join(DIR, "meta.json"), "w", encoding="utf-8"))
    print(f"монет {nc}, 5-мин. свечей {N5}, часов {NH}")


# ---------------------------------------------------------------- данные в процессе
D = {}


def load():
    if D:
        return D
    meta = json.load(open(os.path.join(DIR, "meta.json"), encoding="utf-8"))
    D["coins"] = meta["coins"]
    for k in "ohlc":
        D[k] = np.load(os.path.join(DIR, f"mat_{k}.npy"), mmap_mode="r")
    for k in ("c", "h", "l"):
        D["h" + k] = np.load(os.path.join(DIR, f"h1_{k}.npy"))
    D["elig"] = np.load(os.path.join(DIR, "elig.npy"))
    D["maxlev"] = np.array(meta["maxlev"], dtype=float)
    D["fund"] = {i: dict(zip(*meta["fund"][c])) for i, c in enumerate(meta["coins"])}
    btc = D["hc"][0].astype(float)
    peak12 = pd.Series(btc).rolling(12, min_periods=12).max().values
    D["btc_crash"] = btc <= peak12 * 0.95
    D["btc_sma"] = pd.Series(btc).rolling(168, min_periods=168).mean().values
    # дневная волатильность (ст. откл. дневных лог-доходностей за 30 дней по закрытиям 00:00 UTC) — для «волатильных»
    dc = D["hc"][:, 23::24].astype(float)
    with np.errstate(all="ignore"):
        lr = np.diff(np.log(dc), axis=1)
    vol = np.full(dc.shape, np.nan)
    for d in range(31, dc.shape[1]):
        w = lr[:, d - 31:d - 1]
        ok = np.isfinite(w).sum(1) >= 20
        with np.errstate(all="ignore"):
            vol[ok, d] = np.nanstd(w[ok], axis=1)
    D["vol"] = vol
    return D


# ---------------------------------------------------------------- симуляция
DEFAULT = {"mode": "leaders", "side": "long", "n": 3, "E": 3.0, "L": 72, "R": 12, "phase": 0, "keep": 1.0,
           "trail": 0.1, "hard": 0.1, "adds": 0, "add_step": 0.05, "add_E": 1.0, "min_ret": 0.0,
           "btc_filter": False, "btc_exit": True, "vol_top": 0, "exclude": (), "max_hold_h": 0}


def run(p, start=None, end=None, want=False):
    """Капитал с 1,0 от start до end (часы от T0). Возвращает почасовой капитал (закрытие часа) и худший капитал внутри часа."""
    d = load()
    p = {**DEFAULT, **p}
    coins = d["coins"]
    nc = len(coins)
    hc, hh = d["hc"], d["hh"]
    O, H, L_, C = d["o"], d["h"], d["l"], d["c"]
    h0 = int((SIM_FROM - T0) / pd.Timedelta(hours=1)) if start is None else start
    h1 = NH if end is None else min(end, NH)
    excl = np.array([c in p["exclude"] for c in coins])
    cash = 1.0
    pos = {}            # coin → [side, qty, entry, peak, adds, next_add, opened_h]
    eq_close = np.empty(h1 - h0)
    eq_low = np.empty(h1 - h0)
    pause_until = -1
    busted = False
    n_trades = 0
    pnl_coin = {}
    E_tot_cap = 9.5

    def equity(prices):
        return cash + sum(s * q * (prices[i] - e) for i, (s, q, e, *_r) in pos.items())

    def close(i, price, h, slip):
        nonlocal cash, n_trades
        s, q, e = pos[i][0], pos[i][1], pos[i][2]
        px = price * (1 - s * slip)
        pnl = s * q * (px - e) - q * px * FEE
        cash += pnl
        pnl_coin[coins[i]] = pnl_coin.get(coins[i], 0.0) + pnl
        n_trades += 1
        del pos[i]

    def px_at(j, b):
        v = C[j, b]
        return float(v) if np.isfinite(v) else pos[j][2]

    def margin_used(prices):
        """Маржа при плече 10 (или меньшем пределе биржи для монеты), кросс-маржа."""
        return sum(q * prices[i] / min(10.0, d["maxlev"][i]) for i, (s, q, *_r) in pos.items())

    for k, h in enumerate(range(h0, h1)):
        if busted:
            eq_close[k:] = 0.0
            eq_low[k:] = 0.0
            break
        lo_eq = None
        b0 = h * 12
        for b in range(b0, b0 + 12):
            if not pos:
                continue
            last = {}
            worst = {}
            for i in list(pos):
                s, q, e, peak, adds, nxt, oh = pos[i]
                o, hi, lo, c = O[i, b], H[i, b], L_[i, b], C[i, b]
                if not np.isfinite(c):               # нет данных (снята с торгов) — закрыть по последней цене с потерей 5%
                    lp = hc[i, h - 1] if np.isfinite(hc[i, h - 1]) else e
                    close(i, float(lp), h, 0.05)
                    continue
                o, hi, lo, c = float(o), float(hi), float(lo), float(c)
                if s > 0:
                    lvl = max(e * (1 - p["hard"]), peak * (1 - p["trail"]))
                    if o <= lvl:
                        close(i, o, h, SLIP_STOP)
                        continue
                    if lo <= lvl:
                        close(i, lvl, h, SLIP_STOP)
                        continue
                    if adds < p["adds"] and hi >= nxt:
                        pr = {j: (nxt if j == i else px_at(j, b)) for j in pos}
                        eqn = equity(pr)
                        addq = p["add_E"] * eqn / nxt
                        tot = sum(v[1] * pr[j] for j, v in pos.items())
                        if tot + addq * nxt <= E_tot_cap * eqn and margin_used(pr) + addq * nxt / min(10.0, d["maxlev"][i]) <= 0.95 * eqn:
                            px = nxt * (1 + SLIP)
                            cash -= addq * px * FEE
                            e = (e * q + px * addq) / (q + addq)
                            q += addq
                            adds += 1
                            nxt = nxt * (1 + p["add_step"])
                    peak = max(peak, hi)
                    worst[i] = lo
                else:
                    lvl = min(e * (1 + p["hard"]), peak * (1 + p["trail"]))
                    if o >= lvl:
                        close(i, o, h, SLIP_STOP)
                        continue
                    if hi >= lvl:
                        close(i, lvl, h, SLIP_STOP)
                        continue
                    if adds < p["adds"] and lo <= nxt:
                        pr = {j: (nxt if j == i else px_at(j, b)) for j in pos}
                        eqn = equity(pr)
                        addq = p["add_E"] * eqn / nxt
                        tot = sum(v[1] * pr[j] for j, v in pos.items())
                        if tot + addq * nxt <= E_tot_cap * eqn and margin_used(pr) + addq * nxt / min(10.0, d["maxlev"][i]) <= 0.95 * eqn:
                            px = nxt * (1 - SLIP)
                            cash -= addq * px * FEE
                            e = (e * q + px * addq) / (q + addq)
                            q += addq
                            adds += 1
                            nxt = nxt * (1 - p["add_step"])
                    peak = min(peak, lo)
                    worst[i] = hi
                pos[i] = [s, q, e, peak, adds, nxt, oh]
                last[i] = c
                fr = d["fund"][i].get(b)
                if fr is not None:
                    cash -= s * q * c * fr
            if pos:
                wl = cash + sum(s * q * (worst.get(i, last.get(i, e)) - e) for i, (s, q, e, *_r) in pos.items())
                nt = sum(q * worst.get(i, last.get(i, e)) for i, (s, q, *_r) in pos.items())
                lo_eq = wl if lo_eq is None else min(lo_eq, wl)
                if wl <= MM * nt:                    # ликвидация
                    busted = True
                    cash = 0.0
                    pos.clear()
                    break
        if busted:
            eq_close[k] = 0.0
            eq_low[k] = 0.0
            continue
        prices = {i: float(hc[i, h]) if np.isfinite(hc[i, h]) else pos[i][2] for i in pos}
        eqc = equity(prices)
        # выход при обвале BTC
        if p["btc_exit"] and d["btc_crash"][h] and h >= pause_until:
            for i in list(pos):
                close(i, prices[i], h, SLIP + 0.003)
            eqc = cash
            nd = (h // 24 + 1) * 24
            while nd < NH and d["btc_crash"][nd]:
                nd += 24
            pause_until = nd
        # пересмотр
        if h >= pause_until and (h - p["phase"]) % p["R"] == 0 and h >= p["L"]:
            day = h // 24
            cur, past = hc[:, h].astype(float), hc[:, h - p["L"]].astype(float)
            with np.errstate(all="ignore"):
                score = cur / past - 1
            ok = d["elig"][:, day] & np.isfinite(score) & ~excl
            ok[0] = False                                   # BTC — только сигнал
            if p["vol_top"]:
                v = d["vol"][:, day]
                vv = np.where(ok & np.isfinite(v), v, -1)
                top = np.argsort(-vv)[:p["vol_top"]]
                m = np.zeros(nc, dtype=bool)
                m[top[vv[top] > 0]] = True
                ok &= m
            if p["mode"] == "breakout":
                with np.errstate(all="ignore"):
                    prev_hi = np.nanmax(hh[:, h - p["L"]:h], axis=1)
                    prev_lo = np.nanmin(d["hl"][:, h - p["L"]:h], axis=1)
                long_ok = ok & (cur > prev_hi)
                short_ok = ok & (cur < prev_lo)
            else:
                long_ok = ok & (score > p["min_ret"])
                short_ok = ok & (score < -p["min_ret"])
            if p["btc_filter"] and not (hc[0, h] > d["btc_sma"][h]):
                long_ok = long_ok & False
            nl = p["n"] if p["side"] == "long" else (0 if p["side"] == "short" else (p["n"] + 1) // 2)
            ns = p["n"] - nl
            keep_n = max(1, int(round(p["n"] * p["keep"])))
            rank_l = [i for i in np.argsort(-np.where(long_ok, score, -np.inf)) if long_ok[i]]
            rank_s = [i for i in np.argsort(np.where(short_ok, score, np.inf)) if short_ok[i]]
            keep = set(rank_l[:max(keep_n, nl)] if nl else []) | set(rank_s[:max(keep_n, ns)] if ns else [])
            for i in list(pos):
                old = p["max_hold_h"] and h - pos[i][6] >= p["max_hold_h"]
                if i not in keep or old:
                    close(i, prices[i], h, SLIP)
            eqc = equity({i: prices[i] for i in pos})
            targets = [(i, 1) for i in rank_l[:nl]] + [(i, -1) for i in rank_s[:ns]]
            for i, s in targets:
                if len(pos) >= p["n"] or i in pos:
                    continue
                if (s > 0 and sum(1 for v in pos.values() if v[0] > 0) >= nl) or (s < 0 and sum(1 for v in pos.values() if v[0] < 0) >= ns):
                    continue
                px = float(cur[i]) * (1 + s * SLIP)
                tot = sum(v[1] * prices.get(j, v[2]) for j, v in pos.items())
                E = p["E"]
                if eqc <= 0 or tot + E * eqc > E_tot_cap * eqc \
                        or margin_used({j: prices.get(j, v[2]) for j, v in pos.items()}) + E * eqc / min(10.0, d["maxlev"][i]) > 0.95 * eqc:
                    continue
                q = E * eqc / px
                cash -= q * px * FEE
                nxt = px * (1 + s * p["add_step"])
                pos[i] = [s, q, px, px, 0, nxt, h]
                prices[i] = float(cur[i])
            eqc = equity({i: prices.get(i, v[2]) for i, v in pos.items()})
        eq_close[k] = eqc
        eq_low[k] = min(eqc, lo_eq) if lo_eq is not None else eqc
        if eqc <= 0:
            busted = True
    out = {"close": eq_close, "low": eq_low, "trades": n_trades}
    if want:
        out["pnl_coin"] = pnl_coin
    return out


# ---------------------------------------------------------------- оценка
HALVES = (("II 2025", "2025-07-01", "2026-01-01"), ("I 2026", "2026-01-01", "2026-07-01"), ("II 2026", "2026-07-01", "2026-10-01"))


def metrics(r):
    idx = pd.date_range(SIM_FROM, periods=len(r["close"]), freq="h")
    c = pd.Series(r["close"], index=idx)
    lo = pd.Series(r["low"], index=idx)
    out = {"total": float(c.iloc[-1] - 1) * 100}
    for name, a, b in HALVES:
        s = c[(c.index >= a) & (c.index < b)]
        prev = c[c.index < a]
        base = float(prev.iloc[-1]) if len(prev) else 1.0
        out[name] = (float(s.iloc[-1]) / base - 1) * 100 if base > 0 else -100.0
    peak = np.maximum.accumulate(c.values)
    with np.errstate(all="ignore"):
        out["dd"] = float(np.nanmax(1 - lo.values / peak)) * 100
    ms = c.resample("MS").last()
    prev = np.r_[1.0, ms.values[:-1]]
    with np.errstate(all="ignore"):
        mret = np.where(prev > 0, ms.values / prev - 1, -1.0)
    out["months"] = [round(float(x) * 100, 1) for x in mret]
    # 30-дневные окна со старта каждого дня (00:00 UTC)
    v, lv = c.values, lo.values
    t2, h2, l50, ends, st = [], [], [], [], []
    for s in range(0, len(v) - 720, 24):
        st.append(idx[s])
        b = v[s - 1] if s > 0 else 1.0
        if b <= 0:
            t2.append(False); h2.append(False); l50.append(True); ends.append(0.0)
            continue
        w, wl = v[s:s + 720] / b, lv[s:s + 720] / b
        t2.append(bool((w >= 2).any()))
        l50.append(bool((wl <= 0.5).any()))
        ends.append(float(w[-1]))
        h2.append(bool(w[-1] >= 2))
    ends = np.array(ends)
    st = pd.DatetimeIndex(st)
    for name, a, b in HALVES:                      # доля ×2 по окнам, начатым в этом полугодии
        m = (st >= a) & (st < b)
        out["p2 " + name] = float(np.mean(np.array(t2)[m])) * 100 if m.any() else None
    out.update({"p_touch2": float(np.mean(t2)) * 100, "p_end2": float(np.mean(h2)) * 100, "p_half": float(np.mean(l50)) * 100,
                "w_median": float(np.median(ends) - 1) * 100, "w_mean": float(np.mean(ends) - 1) * 100,
                "geo_month": float(((max(c.iloc[-1], 1e-9)) ** (1 / (len(c) / 720)) - 1) * 100), "trades": r["trades"]})
    return out


def grid():
    """Только лонг (шорт отдельно был в минусе); экспозиция всех позиций вместе 1 / 2 / 3 депозита; каждая точка —
    в трёх фазах пересмотра (0, R/3, 2R/3 часов от 00:00 UTC), чтобы не выбрать удачный час."""
    G = []
    for mode, n, Etot, L, R, trail, adds, flt, vt in itertools.product(
            ("leaders", "breakout"), (1, 2, 3), (1.0, 2.0, 3.0), (24, 48, 72, 120, 168), (12, 24, 48),
            (0.1, 0.2, 0.3, 0.4), (0, 1), (False, True), (0, 20)):
        E = round(Etot / n, 3)
        for ph in (0, R // 3, 2 * R // 3):
            G.append({"mode": mode, "side": "long", "n": n, "E": E, "L": L, "R": R, "phase": ph, "trail": trail,
                      "hard": trail, "adds": adds, "add_E": round(E / 2, 3), "btc_filter": flt, "vol_top": vt})
    return G


def _job(p):
    t = time.time()
    try:
        r = run(p)
        m = metrics(r)
    except Exception as e:                     # noqa: BLE001
        return p, {"error": repr(e)}
    m["sec"] = round(time.time() - t, 1)
    return p, m


def scan(a):
    G = grid()
    if a.limit:
        G = G[:a.limit]
    path = os.path.join(HERE, "results", "super_scan.json")
    done = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []
    seen = {json.dumps(x["p"], sort_keys=True) for x in done}
    todo = [p for p in G if json.dumps(p, sort_keys=True) not in seen]
    print(f"вариантов {len(G)}, уже посчитано {len(G) - len(todo)}", flush=True)
    with ProcessPoolExecutor(a.workers) as ex:
        for i, (p, m) in enumerate(ex.map(_job, todo, chunksize=4), 1):
            done.append({"p": p, "m": m})
            if i % 1000 == 0 or i == len(todo):
                json.dump(done, open(path, "w", encoding="utf-8"))
                print(f"  {i}/{len(todo)}", flush=True)
    json.dump(done, open(path, "w", encoding="utf-8"))


def label(p):
    q = {**DEFAULT, **p}
    side = {"long": "лонг", "short": "шорт", "both": "лонг+шорт"}[q["side"]]
    t = (f"{'лидеры' if q['mode'] == 'leaders' else 'пробой'} {side} {q['n']}×{q['E']:g} L{q['L']} R{q['R']} "
         f"стоп {q['trail'] * 100:g}%")
    if q["adds"]:
        t += f" +{q['adds']} докупки"
    if q["btc_filter"]:
        t += " BTC>ср."
    if q["vol_top"]:
        t += f" вол{q['vol_top']}"
    if q.get("phase"):
        t += f" фаза {q['phase']}"
    return t


def row(m):
    return (f"итог {m['total']:+9.0f}% | {m['II 2025']:+7.0f} / {m['I 2026']:+6.0f} / {m['II 2026']:+6.0f} | "
            f"в мес. {m['geo_month']:+5.1f}% | ×2 за 30 дн. {m['p_touch2']:4.1f}% ({m['p2 II 2025']:.0f}/{m['p2 I 2026']:.0f}/{m['p2 II 2026']:.0f}) | "
            f"−50%: {m['p_half']:4.1f}% | медиана окна {m['w_median']:+5.1f}% | просадка {m['dd']:5.1f}% | сделок {m['trades']}")


def train_ok(m):
    return m.get("II 2025", -100) > 0 and m.get("I 2026", -100) > 0 and m.get("dd", 100) < 99


def by_phase(res):
    """Точки сетки без фазы → метрики по фазам."""
    g = {}
    for x in res:
        if "error" in x["m"]:
            continue
        k = json.dumps({kk: v for kk, v in x["p"].items() if kk != "phase"}, sort_keys=True)
        g.setdefault(k, []).append(x["m"])
    return g


def best_phases(a):
    res = json.load(open(os.path.join(HERE, "results", "super_scan.json"), encoding="utf-8"))
    g = {k: v for k, v in by_phase(res).items() if len(v) >= 3}
    rows = []
    for k, ms in g.items():
        f = lambda key: [m[key] for m in ms]  # noqa: E731
        rows.append({"p": json.loads(k), "train_min": min(min(f("II 2025")), min(f("I 2026"))), "test_min": min(f("II 2026")),
                     "test_med": float(np.median(f("II 2026"))), "tot_min": min(f("total")), "tot_med": float(np.median(f("total"))),
                     "p2_train": float(np.mean(f("p2 II 2025") + f("p2 I 2026"))), "p2_test": float(np.mean(f("p2 II 2026"))),
                     "p2": float(np.mean(f("p_touch2"))), "half": float(np.mean(f("p_half"))), "dd": max(f("dd")),
                     "geo_min": min(f("geo_month")), "geo_med": float(np.median(f("geo_month"))), "trades": int(np.mean(f("trades")))})
    print(f"точек сетки {len(rows)} (по 3 фазы); все фазы в плюсе в обоих обучающих полугодиях: "
          f"{sum(r['train_min'] > 0 for r in rows)}; из них и в проверке (II 2026): {sum(r['train_min'] > 0 and r['test_min'] > 0 for r in rows)}")

    def line(r):
        return (f"  {label(r['p']):56s} итог мин/мед {r['tot_min']:+8.0f} / {r['tot_med']:+8.0f}% | проверка II 2026 мин/мед "
                f"{r['test_min']:+5.0f} / {r['test_med']:+5.0f}% | ×2 за 30 дн. обуч. {r['p2_train']:4.1f}% проверка {r['p2_test']:4.1f}% | "
                f"−50%: {r['half']:4.1f}% | в мес. мин {r['geo_min']:+5.1f}% | просадка до {r['dd']:4.0f}% | сделок {r['trades']}")
    ok = [r for r in rows if r["train_min"] > 0]
    print("\n— отбор по обучению (все фазы в плюсе в II 2025 и I 2026), сортировка по доле ×2 на обучении:")
    for r in sorted(ok, key=lambda r: -r["p2_train"])[:a.top]:
        print(line(r))
    print("\n— отбор по обучению, сортировка по худшей фазе среднего месяца:")
    for r in sorted(ok, key=lambda r: -r["geo_min"])[:a.top]:
        print(line(r))
    print("\n— все точки, по худшей фазе итога (как есть):")
    for r in sorted(rows, key=lambda r: -r["tot_min"])[:a.top]:
        print(line(r))
    json.dump(rows, open(os.path.join(HERE, "results", "super_best.json"), "w", encoding="utf-8"), ensure_ascii=False)


def best(a):
    res = json.load(open(os.path.join(HERE, "results", "super_scan.json"), encoding="utf-8"))
    res = [x for x in res if "error" not in x["m"]]
    print(f"вариантов {len(res)}; разорились (просадка ≥ 99%): {sum(x['m']['dd'] >= 99 for x in res)}; "
          f"в плюсе все 3 полугодия: {sum(x['m']['II 2025'] > 0 and x['m']['I 2026'] > 0 and x['m']['II 2026'] > 0 for x in res)}")
    print("\n— по доле ×2 на обучающих полугодиях (II 2025 + I 2026), только если оба в плюсе; II 2026 — проверка:")
    tr = [x for x in res if train_ok(x["m"])]
    tr.sort(key=lambda x: -(x["m"]["p2 II 2025"] + x["m"]["p2 I 2026"]))
    for x in tr[:a.top]:
        print(f"  {label(x['p']):58s} {row(x['m'])}")
    print("\n— по среднему месяцу (геом.) на всём периоде, без разорения:")
    ok = [x for x in res if x["m"]["dd"] < 99]
    ok.sort(key=lambda x: -x["m"]["geo_month"])
    for x in ok[:a.top]:
        print(f"  {label(x['p']):58s} {row(x['m'])}")
    print("\n— по доле ×2 за 30 дней на всём периоде (как есть, с подгонкой):")
    res.sort(key=lambda x: -x["m"]["p_touch2"])
    for x in res[:a.top]:
        print(f"  {label(x['p']):58s} {row(x['m'])}")


def robust(a):
    """Финалисты: все фазы пересмотра, без 5 самых прибыльных монет, соседние параметры."""
    cands = json.loads(open(a.cands, encoding="utf-8").read()) if a.cands else []
    jobs = []
    for p in cands:
        R = p.get("R", DEFAULT["R"])
        for ph in range(0, R, max(1, R // 6)):
            jobs.append(("фаза", p, {**p, "phase": ph}))
        for k, mult in (("L", 0.67), ("L", 1.5), ("trail", 0.7), ("trail", 1.4), ("E", 0.75)):
            q = dict(p)
            q[k] = type(DEFAULT[k])(round(q.get(k, DEFAULT[k]) * mult, 3)) if k != "L" else int(round(q.get(k, DEFAULT[k]) * mult))
            if k == "trail":
                q["hard"] = q["trail"]
            if k == "E":
                q["add_E"] = round(q["E"] / 2, 2)
            jobs.append((f"{k}×{mult:g}", p, q))
    with ProcessPoolExecutor(a.workers) as ex:
        res = list(ex.map(_job, [q for _, _, q in jobs], chunksize=1))
    by = {}
    for (kind, p, q), (_, m) in zip(jobs, res):
        by.setdefault(json.dumps(p, sort_keys=True), []).append((kind, q, m))
    out = []
    for p in cands:
        rows = by[json.dumps(p, sort_keys=True)]
        print(f"\n=== {label(p)}")
        for kind, q, m in rows:
            print(f"  {kind:8s} {label(q)[:60]:60s} {row(m) if 'error' not in m else m['error']}")
        # без 5 самых прибыльных монет (по базовому прогону)
        r = run(p, want=True)
        top5 = [c for c, _ in sorted(r["pnl_coin"].items(), key=lambda kv: -kv[1])[:5]]
        m = metrics(run({**p, "exclude": tuple(top5)}))
        print(f"  без {', '.join(top5)}: {row(m)}")
        out.append({"p": p, "rows": [(k, q, m) for k, q, m in rows], "no_top5": {"coins": top5, "m": m}})
    json.dump(out, open(os.path.join(HERE, "results", "super_robust.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "scan", "one", "best", "robust"])
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--cands", help="robust: JSON-файл со списком параметров финалистов")
    ap.add_argument("--workers", type=int, default=11)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--p", default="{}")
    a = ap.parse_args()
    if a.cmd == "prep":
        prep()
    elif a.cmd == "scan":
        scan(a)
    elif a.cmd == "best":
        best_phases(a)
    elif a.cmd == "robust":
        robust(a)
    else:
        t = time.time()
        r = run(json.loads(a.p), want=True)
        m = metrics(r)
        print(json.dumps({k: v for k, v in m.items() if k != "months"}, ensure_ascii=False), f"{time.time() - t:.1f} с")
        print("месяцы:", m["months"])
        print("монеты:", sorted(((round(v, 3), c) for c, v in r["pnl_coin"].items()))[:5], "…",
              sorted(((round(v, 3), c) for c, v in r["pnl_coin"].items()))[-5:])
