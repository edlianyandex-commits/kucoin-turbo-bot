"""Проверка движка «ставки на лидеров» (turbo/leaders.py; из панели убран 02.10.2026 — не подключался) — без биржи.

    venv\\Scripts\\python turbo\\test_leaders.py
1–5 — на искусственных ценах; 6 — сверка с исследованием (turbo/lottery_scan.py) на часовых свечах Binance 2026.
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "turbo"))
import leaders as LD  # noqa: E402

H = LD.H
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("OK  " if cond else "FAIL"), name, extra)


T0 = 1_790_000_000_000 - (1_790_000_000_000 % (12 * H))      # кратно 12 ч — начало суток или полдень UTC
hours = [T0 + k * H for k in range(400)]


def series(f):
    return {t: f(k) for k, t in enumerate(hours)}


# 1. рейтинг и фильтр BTC
closes = {"BTC": series(lambda k: 100 + 0.05 * k), "A": series(lambda k: 10 * 1.004 ** k), "B": series(lambda k: 10 * 1.003 ** k),
          "C": series(lambda k: 10 * 1.002 ** k), "D": series(lambda k: 10 * 1.001 ** k), "E": series(lambda k: 10 * 0.999 ** k)}
p = dict(LD.DEFAULTS)
d = LD.decide(closes, hours[300], p)
check("BTC растёт — выше средней, берём 3 лидера A, B, C", d["btc_ok"] and set(d["targets"]) == {"A", "B", "C"}, str(d["targets"]))
check("рейтинг по росту за 72 ч", [c for c, _ in d["ranking"][:4]] == ["A", "B", "C", "D"])
closes_dn = dict(closes, BTC=series(lambda k: 200 - 0.05 * k))
check("BTC ниже средней — без позиций", LD.decide(closes_dn, hours[300], p)["targets"] == {})
d_early = LD.decide(closes, hours[100], p)
check("меньше 168 ч истории BTC — без позиций", d_early["targets"] == {} and d_early["btc_ma"] is None)

# 2. перестановка: объём 3× поровну, вниз до целого контракта, комиссия с оборота
st = LD.new_state(200.0, hours[300])
prices = {c: closes[c][hours[300]] for c in closes}
mult = {"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0, "BTC": 0.001}
LD.rebalance(st, {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}, prices, hours[300], mult)
qty = {c: st["positions"][c]["qty"] for c in st["positions"]}
want = {c: int(200 * 3 / 3 / prices[c]) for c in ("A", "B", "C")}
check("количество — целые контракты, ~200 $ на монету", qty == {c: float(v) for c, v in want.items()}, str(qty))
fee = sum(want[c] * prices[c] for c in want) * 0.0008
check("комиссия 0,08% с оборота", abs(st["fees"] - fee) < 1e-9 and abs(st["balance"] - (200 - fee)) < 1e-9)
up = dict(prices, A=prices["A"] * 1.10)
check("A +10% → капитал растёт на 10% её позиции", abs(LD.equity_of(st, up) - (200 - fee + qty["A"] * prices["A"] * 0.10)) < 1e-6)
LD.rebalance(st, {"B": 0.5, "D": 0.5}, up, hours[301], mult)
check("выход из A и C фиксирует прибыль, вход в D", set(st["positions"]) == {"B", "D"} and st["balance"] > 200 - fee)

# 3. ход по часам: перестановка только в 00:00 и 12:00 UTC, финансирование каждый час
st = LD.new_state(200.0, hours[290])
LD.advance(st, closes, hours[330], mult)
reb_hours = sorted({tr["t"] for tr in st["trades"]})
check("перестановки только на закрытии 00:00/12:00 UTC", reb_hours and all((t // H) % 12 == 0 for t in reb_hours), str([(t // H) % 24 for t in reb_hours]))
check("каждый закрытый час — точка капитала", len(st["equity"]) == 1 + 40 and st["last_ms"] == hours[330])
check("финансирование списывается", st["funding"] > 0)

# 4. tick: догоняет пропущенные часы и не считает дважды
calls = []


def fake_fetch(symbols, a, b):
    calls.append((a, b))
    return {c: {t: v for t, v in cl.items() if t <= b} for c, cl in closes.items()}


st = LD.new_state(200.0, hours[250])
st, moved = LD.tick(st, {c: c for c in closes}, mult, hours[290] + 25 * 60_000, fetch=fake_fetch)
n1 = len(st["equity"])
st, moved2 = LD.tick(st, {c: c for c in closes}, mult, hours[290] + 50 * 60_000, fetch=fake_fetch)
check("панель была выключена — пропущенные 40 ч досчитаны", moved and st["last_ms"] == hours[290] and n1 == 41)
check("в том же часе повторно не считает", not moved2 and len(st["equity"]) == n1 and len(calls) == 1)
s = LD.summary(st)
check("сводка: капитал, позиции, следующая перестановка", s["equity"] > 0 and s["next_rebalance_ms"] % (12 * H) == 0
      and s["next_rebalance_ms"] > s["last_ms"])

# 5. сохранение и загрузка
import tempfile  # noqa: E402
path = os.path.join(tempfile.mkdtemp(), "leaders.json")
LD.save(path, st)
check("состояние сохраняется и читается", LD.load(path)["last_ms"] == st["last_ms"])

# 6. сверка с исследованием на истории 2026 (часовые свечи Binance, turbo/results/ohlc_1h_2025-08.npz)
try:
    import pandas as pd
    import range_scan as RS
    import lottery_scan as LS
    t, o, h, l, c, coins = RS.ohlc()
    a = int(np.searchsorted(t, int(pd.Timestamp("2026-01-01", tz="UTC").value // 1_000_000)))
    b = int(np.searchsorted(t, int(pd.Timestamp("2026-10-01", tz="UTC").value // 1_000_000)))
    cl = {coin: {int(t[i]) + H: float(c[i, j]) for i in range(a - 300, b) if not np.isnan(c[i, j])} for j, coin in enumerate(coins)}
    st = LD.new_state(200.0, int(t[a]) + H)
    st["params"]["cost"] = 0.0008
    LD.advance(st, cl, int(t[b - 1]) + H, {coin: 1e-9 for coin in coins})          # без округления — как в исследовании
    eng = st["equity"][-1][1] / 200
    btc = c[:, coins.index("BTC")]
    regime = pd.Series(btc).rolling(168, min_periods=168).mean().values < btc
    a12 = a + (-(int(t[a]) + H) // H) % 12                                          # то же выравнивание по 00:00/12:00 UTC
    res = LS.run_mom(c, a12, b, 72, 12, 3, 3.0, True, regime)
    ref = res[-1]
    check("движок = исследование на 2026 (3 монеты, 72 ч, раз в 12 ч, 3×, фильтр BTC)",
          abs(eng / ref - 1) < 0.03, f"движок ×{eng:.3f}, исследование ×{ref:.3f}")
    print(f"     январь–сентябрь 2026 по правилам движка: ×{eng:.2f}; комиссии {st['fees']:.0f} $, финансирование {st['funding']:.0f} $")
except FileNotFoundError:
    print("     (нет свечей turbo/results/ohlc_1h_2025-08.npz — сверка пропущена)")

print("\nИТОГ:", "всё прошло" if ok else "ЕСТЬ ОШИБКИ")
