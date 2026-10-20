"""Проверка логики реальной торговли панели основного бота на подставных данных: без биржи и без passivbot.

    venv\\Scripts\\python tb4\\test_live.py
Вместо passivbot live запускается безобидный спящий процесс, вместо биржи — файл снимка во временной папке.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tb4", "panel"))
sys.path.insert(0, os.path.join(ROOT, "tb4"))
import server as P  # noqa: E402

tmp = tempfile.mkdtemp()
P.DATA = tmp
P.COINLIST, P.APPROVED = os.path.join(tmp, "coin_list.json"), os.path.join(tmp, "approved_coins.json")
P.COINLIST_HISTORY = os.path.join(tmp, "coin_list_history.json")
P.LIVE_STATE = os.path.join(tmp, "live.json")
P.LIVE_LOG = os.path.join(tmp, "live.log")
P.MONITOR = os.path.join(tmp, "state.latest.json")
P.KEYS_PATH = os.path.join(tmp, "keys.json")
P.ROOT = tmp                     # конфиг запуска пишется во временную папку, а не в configs/ проекта
json.dump({P.USER: {"key": "k", "secret": "s", "passphrase": "p"}}, open(P.KEYS_PATH, "w"))
_stop = P.proc.stop_tree
P.proc.stop_tree = lambda pid, grace_s=20.0, ctime=None: _stop(pid, 0.5, ctime)   # в тесте не ждём 20 с
BTCM = {"drop": -1.0}         # BTC к максимуму закрытий за N часов, % (вместо часовых свечей KuCoin)


def set_btc(drop_pct):
    BTCM["drop"] = drop_pct
    P.BTC_MOVE_CACHE["t"] = 0


P.btc_move = lambda hours: {"btc": 100000.0, "high": 100000.0, "hours": hours, "drop_pct": BTCM["drop"], "checked": "тест"}
ok = True
spawned = []


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("OK  " if cond else "FAIL"), name, extra)


def sleeper(cfg_path, note):     # вместо passivbot live — спящий процесс
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    spawned.append(note)
    return p.pid


def crasher(cfg_path, note):     # «passivbot сразу упал»: процесс уже завершён
    p = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW)
    p.wait()
    spawned.append(note)
    return p.pid


# 1. запуск и снимок со стопом passivbot на паузе
P.spawn_live = sleeper
P.live_start({"confirm": "ЗАПУСК"})
check("бот запущен", P.live_status()["running"])
until = int(time.time() * 1000) + 3 * 86400_000
# +1 с: часы Windows идут шагами по 10–16 мс, снимок «в ту же миллисекунду» мог оказаться раньше запуска процесса
json.dump({"meta": {"current_cycle_ts_ms": int(time.time() * 1000) + 1000}, "account": {"equity": 250.0, "balance_raw": 250.0},
           "positions": {}, "open_orders": {},
           "hsl": {"long": {"enabled": True, "tier": "red", "halted": True, "no_restart_latched": False,
                            "cooldown_until_ms": until}}}, open(P.MONITOR, "w"))
h = (P.live_status().get("snapshot") or {}).get("hsl") or {}
check("панель видит паузу после стопа", h.get("halted") is True and h.get("cooldown_until_ms") == until)
P.live_stop()
check("остановлен кнопкой", not P.live_status()["running"])

# 2. после остановки кнопкой автоперезапуска нет
P.spawn_live = crasher
P.live_resume()
check("остановлен кнопкой — не поднимается сам", not spawned[1:], str(spawned))

# 3. бот падает сразу после запуска — не больше 3 перезапусков в час
import os as _os
if _os.path.exists(P.MONITOR):
    _os.remove(P.MONITOR)   # упавший сразу бот не успел написать снимок монитора
st = P.load_json(P.LIVE_STATE, {})
st.update(want_running=True, pid=None)
P.save_json(P.LIVE_STATE, st)
for _ in range(6):
    P.live_resume()
check("падает сразу — не больше 3 перезапусков в час", spawned.count("автовосстановление после перезапуска") == 3,
      str(spawned))
check("автоперезапуск остановлен — видно в панели", bool(P.live_status()["gave_up"]))
check("в журнале — причина", "автоперезапуск остановлен" in open(P.LIVE_LOG, encoding="utf-8").read())

# 4. ручной запуск сбрасывает запрет
P.spawn_live = sleeper
P.live_start({"confirm": "ЗАПУСК"})
check("ручной запуск сбрасывает запрет", not P.live_status()["gave_up"] and P.live_status()["running"])
P.live_stop()

# 5. основная стратегия: EMA Anchor 10 монет × 8 + выход при обвале BTC + стоп по монете (02.10.2026 — единственный пресет)
st = P.settings()
check("единственный пресет — «EMA Anchor, 10 монет + выход по BTC + стоп по монете», выход −5% за 12 ч",
      P.DEFAULT_SETTINGS["preset"] == "ema_wide" and list(P.presets()) == ["ema_wide"]
      and st["btc_exit"] == 0.05 and st["btc_exit_hours"] == 12, str({k: st[k] for k in ("preset", "btc_exit")}))
ep = P.effective_params(st)
check("EMA Anchor: экспозиция 8, 10 монет, первый вход 6% (настройки автора движка, с 03.10.2026)",
      ep["strategy"] == "ema_anchor" and ep["exposure"] == 8 and ep["positions"] == 10 and ep["initial_pct"] == 0.06, str(ep))
cfg = P.build_cfg(st, "2026-09-01", "2026-10-01", "backtests/tb4/test")
ea = cfg["bot"]["long"]["strategy"]["ema_anchor"]
check("EMA Anchor автора движка: средние 415/793 мин, докупка 1,96, сдвиг от позиции 0,358, волатильность 1 ч 13 / 448",
      ea["ema_span_0"] == 415 and ea["ema_span_1"] == 793 and ea["entry_double_down_factor"] == 1.96
      and ea["offset_psize_weight"] == 0.3576 and ea["offset_volatility_1h_weight"] == 13
      and ea["offset_volatility_ema_span_1h"] == 448 and ea["base_qty_pct"] == 0.06
      and ea["offset"] == 0.0193 and ea["offset_volatility_1m_weight"] == 0.4, str(ea))
lr, hl = cfg["bot"]["long"]["risk"], cfg["bot"]["long"]["hsl"]
check("конфиг TB Core: ema_anchor, только лонг, быстрый стоп по монете: без докупок с ½ доли, закрытие при 0,8 доли (сглаживание 2 мин, рыночным), пауза 3 дня",
      cfg["live"]["strategy_kind"] == "ema_anchor" and lr["total_wallet_exposure_limit"] == 8 and lr["n_positions"] == 10
      and cfg["bot"]["short"]["risk"]["total_wallet_exposure_limit"] == 0 and hl["enabled"]
      and cfg["live"]["hsl_signal_mode"] == "coin" and hl["red_threshold"] == 0.8 and hl["tier_ratios"]["orange"] == 0.625
      and hl["orange_tier_mode"] == "tp_only_with_active_entry_cancellation" and hl["ema_span_minutes"] == 2
      and hl["panic_close_order_type"] == "market" and not lr["position_exposure_enforcer_enabled"]
      and hl["restart_after_red_policy"] == "always" and hl["cooldown_minutes_after_red"] == 4320, str(hl))
P.save_json(os.path.join(tmp, "settings.json"), dict(st, preset="careful_stop", btc_exit=0))   # сохранено до 02.10.2026
s_old = P.settings()
check("сохранён убранный пресет «Осторожный + стоп» (без выхода по BTC) — EMA Anchor с выходом по BTC −5% за 12 ч",
      s_old["preset"] == "ema_wide" and s_old["btc_exit"] == 0.05 and s_old["btc_exit_hours"] == 12, str(s_old))
P.save_settings({"preset": "careful_stop", "balance": 300})              # прежний пресет панель не примет
check("сохранение с убранным пресетом — остаётся EMA Anchor с выходом по BTC",
      P.load_json(os.path.join(tmp, "settings.json"), {}).get("preset") == "ema_wide" and P.settings()["btc_exit"] == 0.05)
set_btc(-6.0)
try:
    P.live_start({"confirm": "ЗАПУСК"}); check("идёт обвал BTC — запуск запрещён", False)
except P.UIError as e:
    check("идёт обвал BTC — запуск запрещён", "обвал" in str(e), str(e)[:70])
set_btc(-1.0)
spawned.clear()
P.live_start({"confirm": "ЗАПУСК"})
L = P.load_json(P.LIVE_STATE, {})
check("запуск: выход по BTC записан, режим обычный", L.get("btc_exit") == {"drop": 0.05, "hours": 12} and L.get("mode") == "normal")
P.btc_guard_step()
check("BTC −1% — бот работает", P.load_json(P.LIVE_STATE, {}).get("mode") == "normal" and P.live_status()["running"])
set_btc(-5.4)
P.btc_guard_step()
L = P.load_json(P.LIVE_STATE, {})
check("BTC −5,4% за 12 ч — закрываю всё: passivbot в режиме panic",
      L.get("mode") == "btc_panic" and spawned[-1] == "live_panic" and (L.get("btc_pause") or {}).get("state") == "closing"
      and L["cfg_path"].endswith("live_panic.json") and P.load_json(L["cfg_path"], {})["live"]["forced_mode_long"] == "panic")
check("в журнале — «обвал BTC … закрываю все позиции»", "закрываю все позиции" in open(P.LIVE_LOG, encoding="utf-8").read())
since = L["btc_pause"]["since_ms"]
json.dump({"meta": {"current_cycle_ts_ms": since + 60_000}, "account": {"equity": 287.0, "balance_raw": 287.0},
           "positions": {"SOL/USDT:USDT": {"long": {"size": 3.0}}}, "open_orders": {}}, open(P.MONITOR, "w"))
P.btc_guard_step()
check("позиции ещё открыты — ждём", P.load_json(P.LIVE_STATE, {})["btc_pause"]["state"] == "closing")
json.dump({"meta": {"current_cycle_ts_ms": since + 90_000}, "account": {"equity": 286.0, "balance_raw": 286.0},
           "positions": {}, "open_orders": {}}, open(P.MONITOR, "w"))
P.btc_guard_step()
L = P.load_json(P.LIVE_STATE, {})
check("позиции закрыты — пауза по плану (не «должен работать»), бот остановлен",
      L["btc_pause"]["state"] == "waiting" and L["want_running"] is False and L["btc_pause"]["equity"] == 286.0
      and not P.live_status()["running"] and P.live_status()["gave_up"] is None)
check("в журнале — «пауза, торговля продолжится сама после …»", "продолжится сама после" in open(P.LIVE_LOG, encoding="utf-8").read())
P.live_resume()
check("автовосстановление не поднимает бота во время паузы", not P.live_status()["running"])
n = len(spawned)
P.btc_guard_step()
check("до следующего дня — ждём", len(spawned) == n)
_now0 = P.now_ms
P.now_ms = lambda: L["btc_pause"]["resume_ms"] + 60_000
P.btc_guard_step()
check("00:01 — ещё ждём свечу BTC, закрывшуюся в 00:00", len(spawned) == n)
P.now_ms = _now0
_now = P.now_ms
P.now_ms = lambda: L["btc_pause"]["resume_ms"] + 180_000
set_btc(-5.2)
P.btc_guard_step()
L2 = P.load_json(P.LIVE_STATE, {})
check("в 00:00 BTC ещё падает — этот день пропускаем, пауза до следующих суток",
      len(spawned) == n and not P.live_status()["running"] and L2["btc_pause"]["resume_ms"] == L["btc_pause"]["resume_ms"] + 86_400_000
      and "пауза продлена" in open(P.LIVE_LOG, encoding="utf-8").read())
set_btc(-2.0)
P.btc_guard_step()
check("в тот же день BTC перестал падать — всё равно ждём следующих суток", len(spawned) == n)
P.now_ms = lambda: L2["btc_pause"]["resume_ms"] + 180_000
P.btc_guard_step()
L = P.load_json(P.LIVE_STATE, {})
check("BTC перестал падать — торговля возобновлена сама",
      spawned[-1].startswith("пауза после обвала BTC закончилась") and L["mode"] == "normal" and L["want_running"] is True
      and L.get("btc_pause") is None and L["cfg_path"] == L["cfg_normal"] and P.live_status()["running"])
P.now_ms = _now
set_btc(-6.0); P.btc_guard_step(); set_btc(-1.0)
P.live_stop()
L = P.load_json(P.LIVE_STATE, {})
check("«Остановить бота» во время обвала — пауза отменена, сам не запустится",
      L.get("btc_pause") is None and L["want_running"] is False and not P.live_status()["running"])
L.update(want_running=False, mode="btc_panic", pid=None, cfg_path=os.path.join(P.ROOT, "configs", "tb4", "live_panic.json"),
         btc_pause={"state": "closing", "since_ms": P.now_ms(), "resume_ms": P.now_ms() + 86_400_000, "resume_after": "—"})
P.save_json(P.LIVE_STATE, L)                    # панель перезапустилась между остановкой бота и запуском закрытия позиций
P.btc_guard_step()
check("перезапуск панели посреди закрытия — бот закрытия позиций поднят снова",
      spawned[-1] == "live_panic" and P.live_status()["running"] and P.load_json(P.LIVE_STATE, {})["want_running"] is True)
P.live_stop()

# 6. бумага с выходом по BTC: прогон до обвала, пауза до следующего дня, новый прогон на остаток (подставной passivbot)
import pandas as pd  # noqa: E402
H = 3_600_000
t0 = int(pd.Timestamp("2026-07-01", tz="UTC").value // 1_000_000)
btc_rows = [[t0 - 48 * H + h * H, 94.0 if t0 + 50 * H <= t0 - 48 * H + h * H < t0 + 60 * H else 100.0] for h in range(48 + 24 * 12)]


def fake_run(kind, cfg, stage_cb):
    bt = cfg["backtest"]
    tt = pd.date_range(pd.Timestamp(bt["start_date"], tz="UTC"), pd.Timestamp(bt["end_date"], tz="UTC"), freq="1min")
    d = os.path.join(tmp, "paper_run", bt["start_date"])
    os.makedirs(d, exist_ok=True)
    k = 1.0 + (tt - tt[0]).total_seconds().values / 86400 / 100        # +1% в день от старта прогона
    pd.DataFrame({"timestamp": tt.strftime("%Y-%m-%d %H:%M:%S"), "usd_total_equity": bt["starting_balance"] * k}
                 ).to_csv(os.path.join(d, "balance_and_equity.csv.gz"), index=False)
    pd.DataFrame(columns=["timestamp", "type", "coin", "pnl", "fee_paid"]).to_csv(os.path.join(d, "fills.csv"), index=False)
    return d


_run, _closes = P.run_passivbot, P.kucoin_btc_closes
P.run_passivbot, P.kucoin_btc_closes = fake_run, lambda a, b: btc_rows
pr = P.run_paper_guarded(P.settings(), "2026-07-01", "2026-07-10", lambda x: None)
P.run_passivbot, P.kucoin_btc_closes = _run, _closes
ex = pr["btc_exits"]
check("бумага: выход по BTC в конце часа, когда свеча закрылась на −6%",
      len(ex) == 1 and ex[0]["time"] == "2026-07-03 03:00", str(ex))
check("бумага: после выхода — пауза до следующего дня, затем новый прогон на остаток",
      pr["mode"] == "btc_guard" and abs(pr["end_equity"] - ex[0]["equity"] * 1.06) < 0.5, str(pr["end_equity"]))

# 7. стоп по монете (с 02.10.2026): сводка снимка passivbot и порог в долях монеты
m = {"slot_budget": 30.0, "red_threshold": 1.0, "tier_ratio_orange": 0.5}
hs = P.hsl_summary({"hsl": {"long": {"enabled": True, "tier": "red", "halted": True, "no_restart_latched": False, "coins": {
    "ZEC/USDT:USDT": {"tier": "red", "halted": True, "no_restart_latched": False, "cooldown_until_ms": until,
                      "last_metrics": dict(m, drawdown_usd=0.0)},
    "DASH/USDT:USDT": {"tier": "orange", "halted": False, "last_metrics": dict(m, drawdown_usd=16.5)},
    "SOL/USDT:USDT": {"tier": "yellow", "halted": False, "last_metrics": dict(m, drawdown_usd=10.0)},
    "APT/USDT:USDT": {"tier": "green", "halted": False, "last_metrics": {}}}}}})
cs = {c["coin"]: c for c in hs.get("coins") or []}
check("стоп по монете: в сводке только закрытая ZEC (пауза) и DASH без докупок",
      hs.get("mode") == "coin" and sorted(cs) == ["DASH", "ZEC"] and cs["ZEC"]["halted"] and cs["ZEC"]["cooldown_until_ms"] == until,
      str(hs)[:200])
check("DASH: минус 16,5 $ = 55% доли монеты, без докупок с 50%",
      cs["DASH"]["loss"] == 16.5 and cs["DASH"]["slot_pct"] == 55 and cs["DASH"]["orange_pct"] == 50, str(cs.get("DASH")))
P.save_settings({"preset": "ema_wide", "hsl_threshold": 0.6})
check("порог стопа по монете 60% доли сохраняется",
      P.settings()["hsl_threshold"] == 0.6 and P.effective_params(P.settings())["hsl_mode"] == "coin"
      and P.build_cfg(P.settings(), "2026-09-01", "2026-10-01", "backtests/tb4/test")["bot"]["long"]["hsl"]["red_threshold"] == 0.6)
try:
    P.save_settings({"hsl_threshold": 1.2}); check("порог больше 100% доли — ошибка", False)
except P.UIError as e:
    check("порог больше 100% доли — ошибка (passivbot не примет)", "доли монеты" in str(e), str(e))
P.save_settings({"hsl_threshold": None})
check("порог стопа по монете — снова как в пресете (80% доли)", P.effective_params(P.settings())["hsl_threshold"] == 0.8)
try:
    P.save_settings({"leverage": 5}); check("плечо 5 при экспозиции пресета 8 — ошибка", False)
except P.UIError as e:
    check("плечо 5 при экспозиции пресета 8 — ошибка (бирже не хватит маржи)", "не меньше экспозиции" in str(e), str(e))
check("после отказа плечо осталось 10", P.settings()["leverage"] == 10, str(P.settings().get("leverage")))
try:
    P.save_settings({"exposure": 9, "leverage": 8}); check("экспозиция 9 при плече 8 — ошибка", False)
except P.UIError as e:
    check("экспозиция 9 при плече 8 — ошибка", "не меньше экспозиции" in str(e), str(e))

# 8. защита от делистинга (coin_guard.py): данные KuCoin подставные
P.GUARD, P.GUARD_IGNORED = os.path.join(tmp, "coin_guard.json"), os.path.join(tmp, "coin_guard_ignored.json")
CMAP = P.guard_coins()
KU = {"expire": {}, "st": [], "gone": set(), "ann": [], "fail": False}


def fake_fetch(sess, spot=True):
    if KU["fail"]:
        raise RuntimeError("нет связи с KuCoin")
    fut = {sym: {"expire": KU["expire"].get(c), "turnover": 5e7} for c, sym in CMAP.items() if c not in KU["gone"]}
    return {"futures": fut, "announcements": list(KU["ann"]), "st": list(KU["st"]) if spot else None}


def guard_now():
    P.coin_guard_fetch(force=True)
    return (P.load_json(P.GUARD, {}) or {}).get("coins") or {}


def snap_pos(*coins, eq=310.0):
    json.dump({"meta": {"current_cycle_ts_ms": int(time.time() * 1000) + 1000}, "account": {"equity": eq, "balance_raw": eq},
               "positions": {f"{c}/USDT:USDT": {"long": {"size": 1.0, "price": 10.0}, "short": {"size": 0.0}} for c in coins},
               "open_orders": {}, "hsl": {}}, open(P.MONITOR, "w"))


def log_text():
    return open(P.LIVE_LOG, encoding="utf-8").read()


P.CG.fetch = fake_fetch
set_btc(-1.0)
check(f"защита: монеты бота — все из списка ({len(P.MC.default_coins())}), с символами KuCoin",
      len(CMAP) == len(P.MC.default_coins()) > 30 and CMAP.get("BTC") == "XBTUSDTM", str(len(CMAP)))
check("защита: всё в порядке — монет под защитой нет, файл исключений пуст",
      guard_now() == {} and P.load_json(P.GUARD_IGNORED, {}).get("long") == [])
spawned.clear()
P.live_start({"confirm": "ЗАПУСК"})
cfg_live = P.load_json(os.path.join(P.ROOT, "configs", "tb4", "live.json"), {})
check("боевой конфиг: список исключений — файл (passivbot перечитывает на ходу), закрываемых монет нет",
      cfg_live["live"]["ignored_coins"] == P.GUARD_IGNORED and not cfg_live.get("coin_overrides"))
snap_pos("ZEC", "SOL")
pid0 = P.load_json(P.LIVE_STATE, {})["pid"]
n_log = len(log_text())
KU["expire"]["ZEC"] = P.now_ms() + 3 * 86_400_000
g = guard_now()
check("назначен делистинг ZEC (дата закрытия контракта) — «закрыть»", (g.get("ZEC") or {}).get("level") == "close", str(g))
check("ZEC в файле исключений — бот в неё больше не входит", "ZEC" in P.load_json(P.GUARD_IGNORED, {}).get("long", []))
check("в журнале — «защита от делистинга: ZEC — KuCoin закроет контракт …»",
      "защита от делистинга: ZEC — KuCoin закроет контракт" in log_text()[n_log:])
P.coin_guard_close_step()
L = P.load_json(P.LIVE_STATE, {})
cfg_live = P.load_json(L["cfg_path"], {})
check("по ZEC открыта позиция — бот перезапущен с режимом panic только для ZEC",
      L["pid"] != pid0 and P.live_status()["running"] and L.get("guard_panic") == ["ZEC"] and L["want_running"]
      and cfg_live.get("coin_overrides") == {"ZEC": {"live": {"forced_mode_long": "panic"}}}
      and (cfg_live["live"].get("forced_mode_long") or "") == "", str(cfg_live.get("coin_overrides")))
check("запись запуска (в журнал и Telegram) — «закрываю позицию по ZEC … остальные монеты торгуются»",
      spawned[-1].startswith("защита от делистинга: закрываю позицию по ZEC") and "остальные монеты торгуются" in spawned[-1])
n_sp = len(spawned)
snap_pos("ZEC", "SOL")
P.coin_guard_close_step()
check("повторно не перезапускает", len(spawned) == n_sp)
snap_pos("SOL")
P.coin_guard_close_step(); P.coin_guard_close_step()
check("позиция по ZEC закрылась — запись в журнал один раз", log_text().count("защита от делистинга: позиция по ZEC закрыта") == 1)
KU["st"] = ["SOL"]
g = guard_now()
P.coin_guard_close_step()
check("метка ST у SOL — «не входить», без перезапуска (позицию ведёт как обычно)",
      (g.get("SOL") or {}).get("level") == "ignore" and len(spawned) == n_sp and "SOL" in P.load_json(P.GUARD_IGNORED, {})["long"])
KU["st"], KU["gone"] = [], {"ZEC"}
KU["expire"].pop("ZEC")
g = guard_now()
cfg_saved = P.load_json(P.load_json(P.LIVE_STATE, {})["cfg_normal"], {})
check("контракт ZEC убран с биржи — «не входить», из сохранённого конфига режим panic по ZEC убран",
      (g.get("ZEC") or {}).get("level") == "ignore" and not cfg_saved.get("coin_overrides"), str(cfg_saved.get("coin_overrides")))
check("SOL снова в работе — в журнале «снова в работе»", "SOL" not in g and "защита от делистинга: SOL снова в работе" in log_text())
KU["fail"] = True
n_log = len(log_text())
guard_now(); guard_now()
check("нет связи с KuCoin — ошибка в журнале один раз, прежний список не теряется",
      log_text()[n_log:].count("не удалось получить данные KuCoin") == 1 and "ZEC" in P.load_json(P.GUARD, {})["coins"]
      and P.load_json(P.GUARD, {}).get("error"))
KU["fail"] = False
P.live_stop()
n_log = len(log_text())
KU["ann"] = [{"t": P.now_ms(), "title": "KuCoin Futures Will Delist the DASHUSDT Perpetual Contract and Related Services"}]
g = guard_now()
check("объявление о делистинге DASH — «закрыть»; бот остановлен — в журнал не пишет",
      (g.get("DASH") or {}).get("level") == "close" and "DASH" not in log_text()[n_log:])
st = P.state()
check("панель видит защиту (state.coin_guard)", "DASH" in ((st.get("coin_guard") or {}).get("coins") or {}))
# 9. список монет раз в месяц (kucoin_refresh.py): первый запуск — нынешний список, 1-го числа (после 00:10 UTC) —
# новый по обороту KuCoin; сбой или подозрительно мало монет — прежний список, повтор через час
import shutil  # noqa: E402
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
tb4tmp = os.path.join(tmp, "tb4")
os.makedirs(tb4tmp, exist_ok=True)
for _f in ("kucoin_coins.json", "kucoin_min_cost.json"):
    shutil.copy(os.path.join(P.TB4, _f), tb4tmp)
# ARK — постоянная монета сценария (в живом списке её может уже не быть): добавляем в копию справочника
_ku = P.load_json(os.path.join(tb4tmp, "kucoin_coins.json"), {})
if "ARK" not in _ku["coins"]:
    _ku["coins"].append("ARK")
_ku["kucoin"]["ARK"] = {"symbol": "ARKUSDTM", "multiplier": 1.0, "maxLeverage": 20, "turnover24h": 9e6}
P.save_json(os.path.join(tb4tmp, "kucoin_coins.json"), _ku)
_mc = P.load_json(os.path.join(tb4tmp, "kucoin_min_cost.json"), {})
_mc.setdefault("ARK", 0.3)
P.save_json(os.path.join(tb4tmp, "kucoin_min_cost.json"), _mc)
_tb4, _mch = P.TB4, P.MC.HERE
P.TB4 = P.MC.HERE = tb4tmp
old_coins = P.MC.default_coins()
ROWS = {"rows": None, "fail": False}


def fake_rows(sess, min_turnover=1e6):
    if ROWS["fail"]:
        raise RuntimeError("нет связи с KuCoin")
    return ROWS["rows"]


P.KR.fetch_rows = fake_rows
if os.path.exists(P.COINLIST):
    os.remove(P.COINLIST)
P.coin_list_step(_dt(2026, 10, 2, 12, tzinfo=_tz.utc))
cl = P.load_json(P.COINLIST, {})
check("список монет: первый запуск — нынешний справочник считается списком октября",
      cl.get("month") == "2026-10" and cl.get("count") == len(old_coins), str(cl))
ku = P.load_json(os.path.join(tb4tmp, "kucoin_coins.json"), {})
rows = [(c, dict(ku["kucoin"][c], turnover24h=(1e6 if c == "ARK" else ku["kucoin"][c]["turnover24h"])), 1.0) for c in ku["coins"]]
rows.append(("NEWC", {"symbol": "NEWCUSDTM", "multiplier": 1.0, "maxLeverage": 20, "turnover24h": 9e6}, 1.0))
ROWS["rows"] = rows
P.coin_list_step(_dt(2026, 11, 1, 0, 5, tzinfo=_tz.utc))
check("1-го числа до 00:10 UTC — ещё не обновляет", P.load_json(P.COINLIST, {}).get("month") == "2026-10")
n_log = log_text().count("список монет")
_fetch = P.CG.fetch
P.CG.fetch = lambda sess, spot=True: {"futures": {sym: {"expire": None, "turnover": 5e7} for c, sym in P.guard_coins().items()},
                                      "announcements": [], "st": []}
P.save_json(P.GUARD, {"coins": {"ARK": {"level": "ignore", "reason": "оборот за сутки упал до 0.8 млн $",
                                        "since_ms": P.now_ms(), "delist_ms": None}}})
P.save_json(P.LIVE_STATE, dict(P.load_json(P.LIVE_STATE, {}) or {}, want_running=True))     # журнал пишется, пока бот должен работать
t_guard = time.time()
P.coin_list_step(_dt(2026, 11, 1, 0, 12, tzinfo=_tz.utc))
P.CG.fetch = _fetch
cl = P.load_json(P.COINLIST, {})
new_coins = P.MC.default_coins()
check("1 ноября 00:12 UTC: список обновлён — ARK (оборот 1 млн $) убрана, NEWC добавлена; файл для TB Core тот же",
      cl.get("month") == "2026-11" and "ARK" not in new_coins and "NEWC" in new_coins and cl.get("removed") == ["ARK"]
      and cl.get("added") == ["NEWC"] and P.load_json(P.APPROVED, {}).get("long") == new_coins, str(cl))
check("в журнале — «список монет обновлён … убраны: ARK — открытые позиции по ним бот ведёт как обычно»",
      "список монет обновлён (раз в месяц)" in log_text() and "убраны: ARK — открытые позиции" in log_text())
check("убранная из списка монета (ARK) не получает ложного «снова в работе — причина снята»", "ARK снова в работе" not in log_text())
check("при обновлении проверка защиты пошла по новому списку — до записи файла монет для TB Core",
      P.GUARD_CACHE["t"] >= t_guard and "ARK" not in ((P.load_json(P.GUARD, {}) or {}).get("coins") or {}))
P.coin_list_step(_dt(2026, 11, 20, 12, tzinfo=_tz.utc))
check("в тот же месяц повторно не обновляет", log_text().count("список монет обновлён") == 1)
hist = P.load_json(P.COINLIST_HISTORY, {})
check("история списков: октябрь — прежний (с ARK), ноябрь — новый (с NEWC, без ARK)",
      "ARK" in hist.get("2026-10", []) and "NEWC" not in hist.get("2026-10", []) and "NEWC" in hist.get("2026-11", [])
      and "ARK" not in hist.get("2026-11", []), str({k: len(v) for k, v in hist.items()}))
check("список на дату: 15.10 — октябрьский, 02.11 — ноябрьский, сентябрь (раньше истории) — самый ранний",
      "ARK" in P.coins_on("2026-10-15") and "NEWC" in P.coins_on("2026-11-02") and P.coins_on("2026-09-01") == hist["2026-10"])
# повтор бэктеста без монеты, по которой у биржи нет данных
calls_once = []
_once = P.run_passivbot_once


def fake_once(kind, cfg, cb):
    calls_once.append(list(cfg["live"]["approved_coins"]["long"]))
    if "ZEC" in cfg["live"]["approved_coins"]["long"]:
        raise P.MissingCoin("ZEC", "No exchange data found for coin ZEC")
    return "run_dir"


P.run_passivbot_once = fake_once
cfg_m = P.build_cfg(P.settings(), "2026-10-01", "2026-10-03", "backtests/tb4/panel_paper")
r_m = P.run_passivbot("paper", cfg_m, lambda x: None)
P.run_passivbot_once = _once
check("бэктест: у биржи нет данных по ZEC — повтор без неё, а не ошибка",
      r_m == "run_dir" and len(calls_once) == 2 and "ZEC" not in calls_once[1] and len(calls_once[1]) == len(calls_once[0]) - 1
      and "ZEC" not in cfg_m["backtest"]["coin_sources"])
ROWS["fail"] = True
P.coin_list_step(_dt(2026, 12, 1, 1, 0, tzinfo=_tz.utc))
cl = P.load_json(P.COINLIST, {})
check("1 декабря KuCoin не ответил — прежний список (ноябрь), ошибка записана, в журнале одна строка",
      cl.get("month") == "2026-11" and cl.get("error") and log_text().count("список монет: обновить не удалось") == 1
      and P.MC.default_coins() == new_coins, str(cl))
ROWS["fail"] = False
P.coin_list_step(_dt(2026, 12, 1, 1, 20, tzinfo=_tz.utc))
check("повтор — не раньше чем через час", P.load_json(P.COINLIST, {}).get("month") == "2026-11")
ROWS["rows"] = rows[:10]
st_cl = P.load_json(P.COINLIST, {})
st_cl["tried"] = 0
P.save_json(P.COINLIST, st_cl)
P.coin_list_step(_dt(2026, 12, 1, 3, 0, tzinfo=_tz.utc))
check("монет стало подозрительно мало (10) — прежний список", P.load_json(P.COINLIST, {}).get("month") == "2026-11"
      and P.MC.default_coins() == new_coins)
# неожиданный сбой посреди обновления (запись файла) — не повторяется раз в минуту, а только через час
ROWS["rows"] = rows
writes = []
_kw = P.KR.write


def boom(rows_, here=None):
    writes.append(1)
    raise OSError("диск переполнен")


P.KR.write = boom
st_cl = P.load_json(P.COINLIST, {})
st_cl["tried"], st_cl["tried_month"] = 0, ""
P.save_json(P.COINLIST, st_cl)
try:
    P.coin_list_step(_dt(2027, 1, 1, 1, 0, tzinfo=_tz.utc))
except OSError:
    pass
for minute in (1, 2, 30):
    try:
        P.coin_list_step(_dt(2027, 1, 1, 1, minute, tzinfo=_tz.utc))
    except OSError:
        pass
P.KR.write = _kw
check("сбой записи файла: одна попытка за час, а не раз в минуту", len(writes) == 1, str(len(writes)))
cfg_cl = P.build_cfg(P.settings(), "2025-01-01", "2026-12-01", "backtests/tb4/live")
check("боевой конфиг: монеты бота — файл (TB Core перечитывает его на ходу)", cfg_cl["live"]["approved_coins"] == P.APPROVED)
check("панель видит список монет (state.coin_list)", (P.state().get("coin_list") or {}).get("month") == "2026-11")
P.TB4, P.MC.HERE = _tb4, _mch

print("\nИТОГ:", "всё прошло" if ok else "ЕСТЬ ОШИБКИ")
