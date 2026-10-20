#!/usr/bin/env python3
"""Подставной passivbot для репетиции: панель запускает его вместо `passivbot live`.

Ускоренно проигрывает ход торговли, посчитанный настоящим ядром passivbot (make_replays.py), и ведёт себя для
панели как настоящий бот: пишет снимок monitor/kucoin/<user>/state.latest.json (капитал, позиции, ордера,
состояние стопа HSL) и журнал в консоль (в т.ч. строки «[risk] HSL[long] …» в формате passivbot),
выполняет режим panic (закрыть всё), мягко останавливается по Ctrl+C/SIGINT.
На биржу ничего не отправляет. Только стандартная библиотека.

Окружение: SIM_DIR — папка с replay.json, clock.json {t0_real, sim_start_ms, speed}, control.json, exchange.json.
Стоп по монете (meta.hsl.signal_mode = "coin", основной бот с 02.10.2026): прогон passivbot по нему почти не даёт событий,
поэтому стенд проигрывает учебную тревогу из control.json: "coin_drill": [[время сценария, мс, монета, событие,
минус по монете $], …], события orange / green / red / final / resumed — строки «[risk] HSL[long:МОНЕТА/USDT:USDT] …»
и снимок с hsl.long.coins, как у настоящего passivbot (src/passivbot_hsl.py, passivbot_monitor.py).
"""
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

SIM_DIR = os.environ.get("SIM_DIR", "")
STOP = False


def load(name, default):
    try:
        with open(os.path.join(SIM_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, data):
    path = os.path.join(SIM_DIR, name)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def log(sim_ms, level, text):
    print(f"{fmt(sim_ms)} {level:<8} {text}", flush=True)


def on_signal(*_):
    global STOP
    STOP = True


class Replay:
    """Капитал, позиции и события стопа из прогона passivbot на момент времени."""

    def __init__(self, data):
        self.meta = data["meta"]
        self.equity = data["equity"]              # [ts, balance, equity, strategy_equity] по часам
        self.fills = data["fills"]                # [ts, coin, type, qty, price, psize, pprice, pnl, fee, we]
        self.hsl = self.meta["hsl"]
        # прогон кончился ликвидацией (капитал < 10% старта): после последней сделки позиций нет, деньги — остаток
        self.liquidated = self.equity[-1][2] < self.meta["balance"] * 0.1
        self.liq_ts = (self.fills[-1][0] if self.fills else self.equity[-1][0]) + 60_000
        self.coin_mode = self.hsl.get("signal_mode") == "coin"      # стоп по монете — учебная тревога из control.json
        self.events = [] if self.coin_mode else sorted(self._hsl_events(), key=lambda e: e[0])

    def at(self, t):
        lo, hi = 0, len(self.equity) - 1
        if t <= self.equity[0][0]:
            return self.equity[0]
        while lo < hi:                            # последняя точка не позже t
            mid = (lo + hi + 1) // 2
            if self.equity[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        return self.equity[lo]

    def positions(self, t):
        if self.liquidated and t >= self.liq_ts:
            return {}
        pos = {}
        for ts, coin, _ty, _q, _p, ps, pp, _pnl, _fee, we in self.fills:
            if ts > t:
                break
            if ps > 1e-12:
                pos[coin] = (ps, pp, we)
            else:
                pos.pop(coin, None)
        return pos

    def _hsl_events(self):
        """Стоп по просадке: по аварийным закрытиям (close_panic) или по ликвидации в конце прогона."""
        if not self.hsl["enabled"]:
            return []
        red = self.hsl["red_threshold"]
        ratios = self.hsl.get("tier_ratios") or {"yellow": 0.5, "orange": 0.75}
        policy = self.hsl["restart_after_red_policy"]
        cooldown_ms = int(self.hsl["cooldown_minutes_after_red"] * 60_000)
        panic = [f[0] for f in self.fills if "panic" in f[2]]
        clusters = []
        for ts in panic:
            if clusters and ts - clusters[-1][1] < 6 * 3_600_000:
                clusters[-1][1] = ts
            else:
                clusters.append([ts, ts])
        liquidated = self.liquidated
        if liquidated and not clusters:
            clusters.append([self.liq_ts, self.liq_ts + 5 * 60_000])
        ev, peak, tier, reset_at = [], None, "green", self.equity[0][0]
        stops = iter(clusters)
        nxt = next(stops, None)
        for ts, _b, _e, se in self.equity:
            if nxt and ts >= nxt[0]:
                ev.append((nxt[0] - 60_000, "tier", tier, "red", self._dd(peak, se)))
                ev.append((nxt[0] - 60_000, "red", None, None, max(red, self._dd(peak, se))))
                if policy == "never" or liquidated:
                    ev.append((nxt[1], "final_terminal", None, None, None))
                    return ev
                ev.append((nxt[1], "final_restart", None, None, None))
                ev.append((nxt[1] + cooldown_ms, "resumed", None, None, None))
                tier, peak, reset_at = "green", None, nxt[1] + cooldown_ms
                nxt = next(stops, None)
                continue
            if ts < reset_at:
                continue
            peak = se if peak is None else max(peak, se)
            dd = self._dd(peak, se)
            new = ("orange" if dd >= red * ratios["orange"] else "yellow" if dd >= red * ratios["yellow"]
                   else "green" if dd < red * ratios["yellow"] * 0.8 else tier)   # запас, чтобы не мигало
            if new != tier:
                ev.append((ts, "tier", tier, new, dd))
                tier = new
        if nxt:                                   # стоп/ликвидация после последней часовой точки
            dd = max(red, self._dd(peak, self.equity[-1][3]))
            ev += [(nxt[0] - 60_000, "tier", tier, "red", dd), (nxt[0] - 60_000, "red", None, None, dd),
                   (nxt[1], "final_terminal" if (policy == "never" or liquidated) else "final_restart", None, None, None)]
        return ev

    @staticmethod
    def _dd(peak, se):
        return 0.0 if not peak else max(0.0, 1 - se / peak)


class CoinDrill:
    """Учебная тревога стопа по монете: [[время, мс, монета, событие, минус $], …] → строки журнала и состояние монет."""
    STATUS_EVERY_MS = 15 * 60_000            # passivbot пишет status по монете с позицией раз в 15 минут

    def __init__(self, drill, hsl, balance, n_positions, start_t):
        self.drill = sorted(drill or [], key=lambda e: e[0])
        self.red = float(hsl["red_threshold"])
        self.orange_ratio = float((hsl.get("tier_ratios") or {}).get("orange", 0.5))
        self.cooldown_ms = int(hsl["cooldown_minutes_after_red"] * 60_000)
        self.slot = balance / max(1.0, float(n_positions))
        self.coins = {}
        self.done = sum(1 for e in self.drill if e[0] <= start_t)    # события до старта бота — в прошлом
        for e in self.drill[:self.done]:
            self._apply(e, log_it=False)

    def _metrics(self, loss):
        raw = min(1.0, max(0.0, loss) / self.slot)
        return raw, (f"drawdown_raw={raw:.6f} drawdown_ema={raw:.6f} drawdown_score={raw:.6f} red_threshold={self.red:.6f} "
                     f"slot_budget={self.slot:.6f} realized_pnl=0.000000 peak_realized_pnl=0.000000 upnl={-loss:.6f}")

    def _status(self, t, sym, c):
        raw, m = self._metrics(c["loss"])
        log(t, "INFO", f"[risk] HSL[long:{sym}] status | tier={c['tier']} dist_to_red={max(0.0, self.red - raw):.6f} "
                       f"{m.replace(' slot_budget', ' cooldown_remaining=none last_red_ts=none pending_red_since_ms=none slot_budget')}")
        c["status_ms"] = t

    def _apply(self, e, log_it=True):
        ets, coin, kind, loss = e[0], e[1], e[2], float(e[3] if len(e) > 3 else 0.0)
        sym = f"{coin}/USDT:USDT"
        c = self.coins.setdefault(sym, {"tier": "green", "halted": False, "cooldown_until_ms": None, "loss": 0.0,
                                        "status_ms": 0})
        if kind in ("orange", "green"):
            c.update(tier=kind, loss=loss)
            if log_it:
                self._status(ets, sym, c)
        elif kind == "red":
            c.update(tier="red", loss=loss)
            if log_it:
                log(ets, "CRITICAL", f"[risk] HSL[long:{sym}] RED triggered | {self._metrics(loss)[1]}")
        elif kind == "final":
            c.update(tier="red", halted=True, cooldown_until_ms=ets + self.cooldown_ms)
            if log_it:
                raw = self._metrics(c["loss"])[0]
                log(ets, "CRITICAL", f"[risk] HSL[long:{sym}] RED stop finalized | stop_ts={ets} drawdown_raw={raw:.6f} "
                                     f"cooldown_until_ms={c['cooldown_until_ms']} no_restart_latched=False latch=simulation")
        elif kind == "resumed":
            c.update(tier="green", halted=False, cooldown_until_ms=None, loss=0.0)
            if log_it:
                log(ets, "INFO", f"[risk] HSL[long:{sym}] RED cooldown elapsed; trading resumed")

    def step(self, t):
        while self.done < len(self.drill) and self.drill[self.done][0] <= t:
            self._apply(self.drill[self.done])
            self.done += 1
        for sym, c in self.coins.items():         # монеты не на зелёном уровне — status раз в 15 минут, как passivbot
            if c["tier"] == "orange" and not c["halted"] and t - c["status_ms"] >= self.STATUS_EVERY_MS:
                self._status(t, sym, c)

    def halted(self):
        return {sym.split("/")[0] for sym, c in self.coins.items() if c["halted"]}

    def payload(self):
        tiers = {"green": 1, "yellow": 2, "orange": 3, "red": 5}
        coins = {sym: {"tier": c["tier"], "halted": c["halted"], "no_restart_latched": False,
                       "cooldown_until_ms": c["cooldown_until_ms"],
                       "last_metrics": {"tier": c["tier"], "drawdown_usd": round(c["loss"], 4), "slot_budget": self.slot,
                                        "red_threshold": self.red, "tier_ratio_orange": self.orange_ratio}}
                 for sym, c in self.coins.items()}
        return {"enabled": True, "tier": max((c["tier"] for c in coins.values()), key=lambda x: tiers.get(x, 4), default="green"),
                "halted": any(c["halted"] for c in coins.values()), "no_restart_latched": False, "coins": coins}


def main():
    if len(sys.argv) < 3 or sys.argv[1] != "live":
        sys.exit("использование: sim_passivbot.py live <config.json>")
    with open(sys.argv[2], encoding="utf-8") as f:
        cfg = json.load(f)
    user = cfg["live"]["user"]
    panic_mode = (cfg["live"].get("forced_mode_long") or "") == "panic"
    # защита от делистинга: режим panic только для отдельных монет (coin_overrides) — закрыть их, остальные торгуют
    coin_panic = sorted(c for c, v in (cfg.get("coin_overrides") or {}).items()
                        if ((v or {}).get("live") or {}).get("forced_mode_long") == "panic")
    snap_path = os.path.join(os.getcwd(), "monitor", "kucoin", user, "state.latest.json")
    os.makedirs(os.path.dirname(snap_path), exist_ok=True)
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), on_signal)

    rp = Replay(load("replay.json", None))
    clock = load("clock.json", {})
    control = load("control.json", {})
    sim_now = lambda: int(clock["sim_start_ms"] + (time.time() - clock["t0_real"]) * clock["speed"] * 1000)  # noqa: E731
    t = sim_now()
    log(t, "INFO", f"passivbot live (РЕПЕТИЦИЯ, без биржи) | user={user} exchange=kucoin "
                   f"mode_long={'panic' if panic_mode else 'normal'} replay={rp.meta['label']}")
    if control.get("crash_on_start"):
        log(t, "ERROR", "passivbot startup error | KuCoin rejected API key (симуляция) | action=stop")
        sys.exit(1)
    crash_at = time.time() + control["crash_after_real_s"] if control.get("crash_after_real_s") else None
    if crash_at:
        control["crash_after_real_s"] = None          # один раз: перезапущенный бот больше не падает
        save("control.json", control)

    done = set(json.dumps(e[:2]) for e in rp.events if e[0] <= t)   # события до старта уже в прошлом
    drill = CoinDrill(control.get("coin_drill"), rp.hsl, rp.meta["balance"], rp.meta["n_positions"], t) if rp.coin_mode else None
    panic_started = None
    coin_panic_since, coin_panic_done = time.time(), False
    last_health = t
    while not STOP:
        clock = load("clock.json", clock)         # репетиция может замедлить время (например, во время фиксации)
        t = sim_now()
        if crash_at and time.time() >= crash_at:
            print("Traceback (most recent call last):\n  File \"passivbot.py\", line 1, in <module>\n"
                  "RuntimeError: simulated crash (репетиция: внезапное падение процесса)", flush=True)
            os._exit(1)
        ex = load("exchange.json", {})
        ts_, bal, eq, se = rp.at(t)
        pos = rp.positions(t)
        if rp.liquidated and t >= rp.liq_ts:     # после ликвидации на счёте остаток, а не часовая точка до неё
            bal = eq = se = rp.equity[-1][2]
        if ex.get("frozen_from_ms") is not None and t >= ex["frozen_from_ms"]:
            bal = eq = ex["frozen_equity"]           # после аварийного закрытия счёт стоит в деньгах
            pos = {}
        if panic_mode:
            if panic_started is None:
                panic_started = time.time()
                log(t, "INFO", f"[mode] forced_mode_long=panic: закрываю все позиции ({len(pos)})")
            if time.time() - panic_started >= 3 and ex.get("frozen_from_ms") is None:
                fees = sum(we for _, _, we in pos.values()) * bal * 0.0006   # объём = загрузка × баланс (размер у KuCoin — в контрактах)
                save("exchange.json", {"frozen_from_ms": t, "frozen_equity": round(eq - fees, 4)})
                log(t, "INFO", f"[panic] все позиции закрыты | equity={eq - fees:.2f}")
        # события стопа по просадке — строками как у passivbot
        hsl = {"enabled": rp.hsl["enabled"], "tier": "green", "halted": False, "no_restart_latched": False,
               "cooldown_until_ms": None}
        if drill is not None:                    # стоп по монете: монета закрыта на паузу — её позиции в снимке нет
            drill.step(t)
            hsl = drill.payload()
            gone = drill.halted()
            pos = {c: v for c, v in pos.items() if c not in gone}
        if coin_panic:
            if time.time() - coin_panic_since >= 3:              # как и общий panic: закрытие за ~3 с
                if not coin_panic_done:
                    coin_panic_done = True
                    log(t, "INFO", f"[panic] {', '.join(coin_panic)}: позиции закрыты (режим panic для монет)")
                pos = {c: v for c, v in pos.items() if c not in coin_panic}
        for ev in rp.events:
            ets, kind, a, b, dd = ev
            if ets > t:
                break
            key = json.dumps(ev[:2])
            if kind == "tier":
                hsl["tier"] = b
            elif kind == "red":
                hsl["tier"] = "red"
            elif kind == "final_restart":
                hsl.update(halted=True, tier="red", cooldown_until_ms=ets + int(rp.hsl["cooldown_minutes_after_red"] * 60_000))
            elif kind == "final_terminal":
                hsl.update(halted=True, tier="red", no_restart_latched=True)
            elif kind == "resumed":
                hsl.update(halted=False, tier="green", cooldown_until_ms=None)
            if key in done:
                continue
            done.add(key)
            red = rp.hsl["red_threshold"]
            if kind == "tier":
                log(ets, "INFO", f"[risk] HSL[long] tier transition {a} -> {b} | balance={bal:.6f} strategy_equity={se:.6f} "
                                 f"drawdown_score={dd:.6f} red_threshold={red:.6f} yellow=0.500 orange=0.750")
            elif kind == "red":
                log(ets, "CRITICAL", f"[risk] HSL[long] RED triggered | strategy_equity={se:.6f} drawdown_score={dd:.6f} "
                                     f"red_threshold={red:.6f}")
            elif kind == "final_restart":
                log(ets, "CRITICAL", f"[risk] HSL[long] RED stop finalized (auto-restart eligible) | stop_ts={ets} "
                                     f"cooldown_until_ms={hsl['cooldown_until_ms']}")
            elif kind == "final_terminal":
                log(ets, "CRITICAL", f"[risk] HSL[long] RED stop finalized (terminal) | stop_ts={ets} strategy_equity={se:.6f}")
            elif kind == "resumed":
                log(ets, "INFO", "[risk] HSL[long] RED cooldown elapsed; trading resumed")
        if t - last_health >= 6 * 3_600_000:
            last_health = t
            log(t, "INFO", f"[health] equity={eq:.2f} balance={bal:.2f} positions={len(pos)}")
        notional = sum(ps * pp for ps, pp, _ in pos.values()) or 1.0
        snap = {
            "meta": {"current_cycle_ts_ms": int(time.time() * 1000), "sim_time_ms": t, "simulation": True},
            "account": {"balance_raw": round(bal, 4), "equity": round(eq, 4)},
            "positions": {f"{c}/USDT:USDT": {"long": {"size": ps, "price": pp,
                                                       "upnl": round((eq - bal) * ps * pp / notional, 4),
                                                       "wallet_exposure": we},
                                              "short": {"size": 0.0}} for c, (ps, pp, we) in pos.items()},
            "open_orders": {f"{c}/USDT:USDT": [{}, {}] for c in pos},
            "hsl": {"long": hsl},
        }
        with open(snap_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(snap_path + ".tmp", snap_path)
        time.sleep(0.2)
    log(sim_now(), "INFO", "[shutdown] signal received; requesting graceful stop")


if __name__ == "__main__":
    main()
