"""Панель для репетиции: та же панель (tb4/panel/server.py), но на своём порту и с ускоренными наблюдателями.
  python sim_panel.py <корень копии проекта> <порт> <ускорение ожиданий>
Турбо: если в replay.json есть часовые цены BTC (make_replays.py --btc), выход при обвале BTC смотрит на BTC
во время сценария (по clock.json), а не на сегодняшний BTC с биржи. Пауза после обвала BTC (до 00:00 UTC следующего
дня) — тоже по времени сценария.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

root, port, factor = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
sys.path.insert(0, os.path.join(root, "tb4", "panel"))
os.chdir(root)
sys.argv = [sys.argv[0], "--no-browser"]
import server as P  # noqa: E402

P.PORT = port
_sleep = time.sleep
# наблюдатель ставки (15 с) и автоперезапуск (60 с) — в factor раз чаще; остальное время — настоящее
P.time = type("T", (), {"sleep": staticmethod(lambda s: _sleep(max(0.05, s / factor))),
                        "time": staticmethod(time.time)})()

SIM_DIR = os.environ.get("SIM_DIR", "")
_BTC = {"mtime": None, "rows": None}


def _load(name):
    with open(os.path.join(SIM_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def sim_btc_move(hours):
    """Как P.btc_move, но по часовым закрытиям BTC из replay.json на момент времени сценария."""
    path = os.path.join(SIM_DIR, "replay.json")
    mtime = os.path.getmtime(path)
    if _BTC["mtime"] != mtime:
        _BTC.update(mtime=mtime, rows=_load("replay.json").get("btc"))
    if not _BTC["rows"]:
        raise RuntimeError("в ходе торговли сценария нет цен BTC (make_replays.py --btc)")
    ck = _load("clock.json")
    now = ck["sim_start_ms"] + (time.time() - ck["t0_real"]) * ck["speed"] * 1000
    closes = [c for ts, c in _BTC["rows"] if ts + 3_600_000 <= now][-hours:]
    if len(closes) < hours:
        raise RuntimeError("мало часовых свечей BTC на это время сценария")
    return {"btc": closes[-1], "high": max(closes), "hours": hours,
            "drop_pct": round((closes[-1] / max(closes) - 1) * 100, 2),
            "checked": datetime.fromtimestamp(now / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") + " (сценарий)"}


def sim_now_ms():
    ck = _load("clock.json")
    return int(ck["sim_start_ms"] + (time.time() - ck["t0_real"]) * ck["speed"] * 1000)


if hasattr(P, "bet_clock_ms") and SIM_DIR:
    P.bet_clock_ms = sim_now_ms           # перенос стопа раз в 30 дней — по времени сценария, а не по часам сервера
if hasattr(P, "rules_clock_ms") and SIM_DIR:
    P.rules_clock_ms = sim_now_ms         # основной бот: пауза после обвала BTC до следующего дня — по времени сценария
if hasattr(P, "btc_move") and SIM_DIR:
    P.btc_move = sim_btc_move
    _cached = P.btc_move_cached
    # кэш на минуту — в масштабе ускоренных наблюдателей, иначе при ускорении проверка BTC отстаёт на часы сценария
    P.btc_move_cached = lambda hours, ttl=60: _cached(hours, ttl=ttl / factor)
if hasattr(P, "CG") and SIM_DIR:
    def sim_guard_fetch(sess, spot=True):
        """Защита от делистинга: «данные KuCoin» — из guard_data.json сценария ({"expire": {монета: мс}, "st": [...],
        "gone": [...], "ann": [...]}), без него — всё в порядке (сценарии не зависят от сегодняшней биржи)."""
        try:
            d = _load("guard_data.json")
        except (OSError, ValueError):
            d = {}
        gone = set(d.get("gone") or [])
        fut = {sym: {"expire": (d.get("expire") or {}).get(c), "turnover": 5e7}
               for c, sym in P.guard_coins().items() if c not in gone}
        return {"futures": fut, "announcements": list(d.get("ann") or []), "st": list(d.get("st") or []) if spot else None}
    P.CG.fetch = sim_guard_fetch
    P.GUARD_EVERY_S = 2                   # проверка «KuCoin» — раз в 2 с настоящего времени, а не 15 мин

    def sim_closes(sess, coins, end_ms, days=32, pause=0.12):
        """Фильтр разогнанных монет (Turbo): «дневные закрытия KuCoin» — у монет из guard_data.json {"hot": {монета:
        во сколько раз}} цена в окне 30 дней менялась в столько раз, у остальных — ровная (дни — от настоящего времени:
        фильтр считает по часам сервера)."""
        try:
            hot = _load("guard_data.json").get("hot") or {}
        except (OSError, ValueError):
            hot = {}
        day = lambda k: P.CG.utc_day(int(time.time() * 1000) - k * 86_400_000)
        return {c: {day(2): 1.0, day(10): float(hot.get(c) or 1.0)} for c in coins}
    P.CG.fetch_closes = sim_closes
    if hasattr(P, "coin_list_step"):
        P.coin_list_step = lambda now=None: None     # список монет на стенде не обновляется
    if hasattr(P, "HOT_EVERY_S"):
        P.HOT_EVERY_S = 2                 # цены «KuCoin» для фильтра — раз в 2 с, а не раз в час
P.main()
