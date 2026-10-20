"""Проверка логики ставки турбо-панели на подставных данных: без биржи и без passivbot.

    venv\\Scripts\\python turbo\\test_bet.py
Вместо passivbot live запускается безобидный спящий процесс, вместо биржи — файл снимка во временной папке.
С 02.10.2026 в панели одна стратегия — Turbo 5.0 (правила 1.0–4.0 и их проверки убраны вместе с пресетами); Супер Турбо
(03.10.2026) убран после честной проверки; с 03.10.2026 у Turbo 5.0 быстрый стоп по монете.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import pandas as pd

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
P.BET = os.path.join(tmp, "bet.json")
P.MONITOR = os.path.join(tmp, "state.latest.json")
P.KEYS_PATH = os.path.join(tmp, "keys.json")
json.dump({P.USER: {"key": "k", "secret": "s", "passphrase": "p"}}, open(P.KEYS_PATH, "w"))
SETTINGS = os.path.join(tmp, "settings.json")
spawned = []
cfgs = []                   # конфиги, с которыми «запускался» passivbot


def fake_spawn(cfg, tag):   # вместо passivbot live — безобидный спящий процесс
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    spawned.append((tag, cfg["live"]["forced_mode_long"], p.pid))
    cfgs.append(cfg)
    return p.pid


P.spawn_live = fake_spawn
_stop = P.proc.stop_tree
P.proc.stop_tree = lambda pid, grace_s=20.0, ctime=None: _stop(pid, 0.5, ctime)   # в тесте не ждём 20 с
BTCM = {"drop": -1.0}      # BTC к максимуму закрытий за N часов, %


def set_btc(drop_pct):
    BTCM["drop"] = drop_pct
    P.BTC_MOVE_CACHE["t"] = 0      # не ждать минуту кэша


P.btc_move = lambda hours: {"btc": 100000.0, "high": 100000.0, "hours": hours, "drop_pct": BTCM["drop"], "checked": "тест"}
HOTC = {}           # подставные дневные закрытия KuCoin для фильтра разогнанных монет: {монета: {день UTC: цена}}
P.CG.fetch_closes = lambda sess, coins, end_ms, days=32, pause=0.12: {c: dict(HOTC.get(c) or {}) for c in coins}
ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("OK  " if cond else "FAIL"), name, extra)


def snap(equity, positions=1, ts=None, hsl=None):
    pos = {"APT/USDT:USDT": {"long": {"size": 10.0 if positions else 0.0, "price": 1.0}, "short": {"size": 0.0}}}
    json.dump({"meta": {"current_cycle_ts_ms": ts or P.now_ms()}, "account": {"equity": equity, "balance_raw": equity},
               "positions": pos, "open_orders": {}, "hsl": {"long": hsl} if hsl else {}}, open(P.MONITOR, "w"))


def wait_for(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


def bet():
    return P.load_json(P.BET, {})


def log_text():
    return open(P.LIVE_LOG, encoding="utf-8").read()


# наблюдатель ставки крутится в потоке; вместо 15 с между проверками — 0,05 с
P.time = type("T", (), {"sleep": staticmethod(lambda s: time.sleep(0.05)), "time": staticmethod(time.time)})()
threading.Thread(target=P.bet_watcher, daemon=True).start()

# 1. одна стратегия — Turbo 5.0; настройки прежних версий переходят на неё
check("пресеты — только Turbo 5.0 (Супер Турбо убран 03.10.2026)", list(P.presets()) == ["turbo_ema5"]
      and P.DEFAULT_SETTINGS["preset"] == "turbo_ema5")
P.save_json(SETTINGS, {"preset": "turbo_ema4", "balance": 200.0, "leverage": 25, "target_mult": 2.0, "bet_stop": 0.6,
                       "stop_rebase_days": 30, "btc_exit": 0, "btc_resume": False, "regime_filter": True,
                       "coin_filter_dd": 0.2, "auto_lock": True})                  # так сохраняла панель 4.0
st = P.settings()
check("сохранённые настройки 4.0 → Turbo 5.0: плечо 10, выход по BTC −5% за 12 ч, ставка 200 $ сохранена",
      st["preset"] == "turbo_ema5" and st["leverage"] == 10 and st["btc_exit"] == 0.05 and st["btc_exit_hours"] == 12
      and st["balance"] == 200.0, str(st))
check("правил прежних версий в настройках нет",
      not any(k in st for k in ("target_mult", "bet_stop", "stop_rebase_days", "btc_resume", "regime_filter",
                                "coin_filter_dd", "auto_lock", "profit_guard")), str(sorted(st)))
P.save_settings({"balance": 300})
saved = P.load_json(SETTINGS, {})
check("после сохранения в файле — только нынешние настройки 5.0",
      saved.get("preset") == "turbo_ema5" and saved.get("btc_exit") == 0.05 and "target_mult" not in saved, str(saved))
ep = P.effective_params(P.settings())
check("5.0: EMA Anchor, экспозиция 9, 7 монет, первая покупка 9,375% (×1,25 с 03.10.2026)", ep["strategy"] == "ema_anchor"
      and ep["exposure"] == 9 and ep["positions"] == 7 and ep["initial_pct"] == 0.09375, str(ep))
check("5.0: первая покупка ≈ 36 $ при ставке 300 $", abs(P.coins_status(P.settings())["first_entry"] - 36.16) < 0.6,
      str(P.coins_status(P.settings())["first_entry"]))
ea = P.build_cfg(P.settings(), "2026-09-01", "2026-10-01", "backtests/tb4/test")["bot"]["long"]["strategy"]["ema_anchor"]
check("своя настройка EMA Anchor (как у основного бота): средние 332/476 мин, докупка 2,94, сдвиг 0,286, волатильность 10,4/560",
      ea["ema_span_0"] == 332 and ea["ema_span_1"] == 476 and ea["entry_double_down_factor"] == 2.94
      and ea["offset_psize_weight"] == 0.28608 and ea["offset_volatility_1h_weight"] == 10.4
      and ea["offset_volatility_ema_span_1h"] == 560 and ea["offset"] == 0.0193, str(ea))
cfg5 = P.build_cfg(P.settings(), "2026-09-01", "2026-10-01", "backtests/tb4/test")
lr, sr, hl = cfg5["bot"]["long"]["risk"], cfg5["bot"]["short"]["risk"], cfg5["bot"]["long"]["hsl"]
check("5.0: конфиг — ema_anchor, лонг, плечо 10, быстрый стоп по монете (как у основного бота: 0,8 доли, 2 мин, рыночный)",
      cfg5["live"]["strategy_kind"] == "ema_anchor" and lr["total_wallet_exposure_limit"] == 9 and lr["n_positions"] == 7
      and sr["total_wallet_exposure_limit"] == 0 and cfg5["live"]["leverage"] == 10 and hl["enabled"]
      and cfg5["live"]["hsl_signal_mode"] == "coin" and hl["red_threshold"] == 0.8 and hl["ema_span_minutes"] == 2
      and hl["tier_ratios"] == {"yellow": 0.375, "orange": 0.625} and hl["panic_close_order_type"] == "market"
      and hl["orange_tier_mode"] == "tp_only_with_active_entry_cancellation" and hl["restart_after_red_policy"] == "always"
      and hl["cooldown_minutes_after_red"] == 4320, str(hl))
ep5 = P.effective_params(P.settings())
check("5.0: панель видит стоп по монете (порог 80% доли, без докупок с 62,5%)",
      ep5["hsl_mode"] == "coin" and ep5["hsl_threshold"] == 0.8 and ep5["hsl_orange"] == 0.625, str(ep5))
for v, want in ((1.0, True), (0.2, True), (0.1, False), (1.2, False)):
    try:
        P.save_settings({"hsl_threshold": v})
        res = True
    except P.UIError:
        res = False
    check(f"стоп по монете: порог {v:g} {'принят' if want else 'отклонён'} (от 20% до 100% доли)", res == want)
P.save_settings({"hsl_threshold": None})
h = P.hsl_summary({"hsl": {"long": {"enabled": True, "tier": "green", "coins": {
    "ZEC/USDT:USDT": {"tier": "orange", "last_metrics": {"drawdown_usd": 20.0, "slot_budget": 42.86, "red_threshold": 0.8,
                                                          "tier_ratio_orange": 0.625}},
    "SOL/USDT:USDT": {"tier": "green"}, "WIF/USDT:USDT": {"tier": "red", "halted": True, "cooldown_until_ms": 1}}}}})
check("сводка стопа по монете: режим coin, ZEC без докупок (47% доли, порог 50%), WIF закрыта; SOL не показана",
      h["mode"] == "coin" and [c["coin"] for c in h["coins"]] == ["WIF", "ZEC"] and h["coins"][1]["slot_pct"] == 47
      and h["coins"][1]["orange_pct"] == 50 and h["coins"][0]["halted"] is True, str(h))
check("5.0: предел доли монеты включён (правка TB Core для EMA Anchor: докупка не выше 9/7 баланса)",
      lr["position_exposure_enforcer_enabled"] is True and lr["position_exposure_enforcer_threshold"] == 1.0, str(lr))
check(f"5.0: все монеты списка в конфиге ({len(P.MC.default_coins())})", len(cfg5["live"]["approved_coins"]["long"]) == len(P.MC.default_coins()) > 30,
      str(len(cfg5["live"]["approved_coins"]["long"])))
for body, word in (({"exposure": 12}, "Экспозиция"), ({"leverage": 25}, "Плечо")):
    try:
        P.save_settings(body); check(f"{word} выше предела 10× — отказ", False)
    except P.UIError as e:
        check(f"{word} выше предела 10× — отказ", word in str(e), str(e)[:60])

# 2. без слова ЗАПУСК — отказ
try:
    P.live_start({"confirm": "да"}); check("без ЗАПУСК отказ", False)
except P.UIError:
    check("без ЗАПУСК отказ", True)

# 3. плечо меньше экспозиции — отказ
P.save_settings({"leverage": 5})
try:
    P.live_start({"confirm": "ЗАПУСК"}); check("плечо < экспозиции: отказ", False)
except P.UIError as e:
    check("плечо < экспозиции: отказ", "плеч" in str(e), str(e)[:80])
P.save_settings({"leverage": 10})

# 4. запуск: ставка без цели, с выходом по BTC; рост — ставка идёт
set_btc(-1.0)
snap(300.0, ts=1)
n_cfg = len(cfgs)
P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
check("живой бот запущен на EMA Anchor", len(cfgs) > n_cfg and cfgs[n_cfg]["live"]["strategy_kind"] == "ema_anchor")
b = bet()
check("в ставке — выход по BTC −5% за 12 ч, правил прежних версий нет",
      b.get("btc_exit") == {"drop": 0.05, "hours": 12} and not any(k in b for k in ("target_mult", "stop_mult", "rebase_days")), str(b))
snap(300.0)
check("капитал на старте записан", wait_for(lambda: bet().get("state") == "running") and bet().get("start_equity") == 300.0)
check("в журнале — «капитал на старте ставки … без цели»", "капитал на старте ставки: 300.00 USDT, без цели" in log_text())
snap(750.0)
check("×2,5 — без цели ставка идёт", wait_for(lambda: bet().get("last_equity") == 750.0) and bet().get("state") == "running")
snap(180.0); time.sleep(0.5)
check("−40% от старта — стопа панели нет, ставка идёт", bet().get("state") == "running")

# 5. стоп passivbot с перезапуском через 3 дня (passivbot сам закрыл всё и ждёт) — ставка продолжается
snap(170.0, positions=0, hsl={"enabled": True, "tier": "red", "halted": True, "no_restart_latched": False,
                              "cooldown_until_ms": P.now_ms() + 3 * 86_400_000})
time.sleep(0.5)
check("стоп passivbot с перезапуском — ставка идёт, бот работает", bet().get("state") == "running" and P.live_status()["running"])
check("панель видит паузу стопа passivbot", (P.live_status().get("snapshot") or {}).get("hsl", {}).get("halted") is True)

# 6. «Зафиксировать сейчас»: режим panic → позиции закрыты → ставка закрыта, бот остановлен
snap(171.0)
P.lock_profit("вручную из панели")
check("фиксация: перезапуск в режиме panic", wait_for(lambda: any(t == "live_panic" and m == "panic" for t, m, _ in spawned)))
check("ставка — «фиксирую»", bet().get("state") == "locking" and bet().get("lock_reason") == "вручную из панели")
snap(172.0, positions=1, ts=P.now_ms() + 60_000); time.sleep(0.5)
check("позиции ещё открыты — ждём", bet().get("state") == "locking")
snap(168.0, positions=0, ts=P.now_ms() + 60_000)
check("позиции закрыты — ставка закрыта с итогом", wait_for(lambda: bet().get("state") == "done") and bet().get("final_equity") == 168.0)
check("бот остановлен", wait_for(lambda: not P.live_status()["running"]))
check("в журнале — «ставка закрыта»", "ставка закрыта: 168.00 USDT" in log_text())

# 7. бот падает сразу после запуска — не больше 3 перезапусков в час, потом ждать человека
cfgp = os.path.join(tmp, "cfg.json")
open(cfgp, "w").write("{}")
resumed = []
real_spawn_path = P.spawn_path


def dead_spawn(path):   # «passivbot сразу упал»: процесс уже завершён
    p = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=subprocess.CREATE_NO_WINDOW)
    p.wait()
    resumed.append(path)
    return p.pid


P.spawn_path = dead_spawn
if os.path.exists(P.MONITOR):
    os.remove(P.MONITOR)   # упавший сразу бот не успел написать снимок монитора
P.save_json(P.LIVE_STATE, {"pid": None, "cfg_path": cfgp, "want_running": True})
for _ in range(6):
    P.live_resume()
check("падает сразу — не больше 3 перезапусков в час", len(resumed) == 3, f"перезапусков: {len(resumed)}")
check("автоперезапуск остановлен — видно в панели", bool(P.live_status()["gave_up"]))
check("в журнале — причина", "автоперезапуск остановлен" in log_text())
P.bet_reset()
P.live_start({"confirm": "ЗАПУСК"})
check("ручной запуск сбрасывает запрет", not P.live_status()["gave_up"] and wait_for(lambda: P.live_status()["running"]))
P.live_stop()
wait_for(lambda: not P.live_status()["running"])

# 8. обвал BTC → закрыть всё, пауза до 00:00 UTC следующего дня, затем та же ставка (или ещё сутки, если BTC всё падает)
paths = []


def path_spawn(path):          # возобновление после паузы запускает готовый конфиг по пути
    pp = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                          creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    paths.append(path)
    return pp.pid


P.spawn_path = path_spawn
CLOCK = {"off": 0}
_clock = P.bet_clock_ms
P.bet_clock_ms = lambda: _clock() + CLOCK["off"]
set_btc(-6.0)
P.bet_reset(); snap(300.0, ts=1)
try:
    P.live_start({"confirm": "ЗАПУСК"}); check("идёт обвал BTC — запуск запрещён", False)
except P.UIError as e:
    check("идёт обвал BTC — запуск запрещён", "обвал" in str(e), str(e)[:70])
set_btc(-1.0)
P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
snap(300.0); wait_for(lambda: bet().get("state") == "running")
n_panic = sum(t == "live_panic" for t, _, _ in spawned)
set_btc(-5.4)
check("обвал BTC — ставка на паузе, закрываю позиции",
      wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "closing") and bet().get("state") == "btc_pause")
check("перезапущен в режиме panic", wait_for(lambda: sum(t == "live_panic" for t, _, _ in spawned) > n_panic))
ls = P.load_json(P.LIVE_STATE, {})
check("LIVE_STATE — режим btc_panic, бот должен работать, обычный конфиг запомнен",
      ls.get("mode") == "btc_panic" and ls.get("want_running") and ls.get("cfg_normal", "").endswith("live.json")
      and (ls.get("btc_pause") or {}).get("state") == "closing", str(ls)[:200])
check("в журнале — «закрываю все позиции», ставка продолжится", "закрываю все позиции" in log_text()
      and "ставка продолжится после паузы" in log_text())
check("ставка не закончилась", bet().get("state") == "btc_pause" and bet().get("start_equity") == 300.0)
time.sleep(0.3)
check("позиции ещё открыты — ждём", (bet().get("btc_pause") or {}).get("state") == "closing")
snap(238.0, positions=0, ts=P.now_ms() + 60_000)
check("позиции закрыты — пауза, бот остановлен, «не должен работать»",
      wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "waiting")
      and wait_for(lambda: not P.live_status()["running"]) and not P.load_json(P.LIVE_STATE, {}).get("want_running"))
pz = bet()["btc_pause"]
check("пауза до 00:00 UTC следующего дня", pz["resume_ms"] == P.next_day_ms(_clock()) and pz.get("equity") == 238.0, str(pz))
check("панель видит паузу", (P.live_status().get("btc_pause") or {}).get("state") == "waiting")
P.live_resume()
check("автовосстановление не поднимает бота на паузе", not P.live_status()["running"])
n_paths = len(paths)
CLOCK["off"] = pz["resume_ms"] - _clock() + 60_000                    # 00:01 — рано (ждём свечу 00:00)
time.sleep(0.4)
check("в 00:01 ещё пауза", bet().get("state") == "btc_pause" and len(paths) == n_paths)
set_btc(-6.0)
CLOCK["off"] = pz["resume_ms"] - _clock() + 180_000                   # 00:03, BTC всё падает
check("в 00:03 BTC всё падает — пауза продлена на сутки",
      wait_for(lambda: bet().get("btc_pause", {}).get("resume_ms") == P.next_day_ms(pz["resume_ms"])) and len(paths) == n_paths)
check("в журнале — «пауза продлена»", "пауза продлена" in log_text())
check("продление видно и в LIVE_STATE (отчёт, Telegram)",
      P.load_json(P.LIVE_STATE, {}).get("btc_pause", {}).get("resume_ms") == P.next_day_ms(pz["resume_ms"]))
set_btc(-2.0)
CLOCK["off"] = P.next_day_ms(pz["resume_ms"]) - _clock() + 180_000   # следующие сутки, 00:03, BTC спокоен
check("BTC перестал падать — торговля возобновлена в той же ставке",
      wait_for(lambda: bet().get("state") == "running") and len(paths) == n_paths + 1 and paths[-1].endswith("live.json"),
      str(paths[-1:]))
ls, b = P.load_json(P.LIVE_STATE, {}), bet()
check("LIVE_STATE — обычный режим, должен работать, паузы нет",
      ls.get("mode") == "normal" and ls.get("want_running") and not ls.get("btc_pause") and ls["cfg_path"].endswith("live.json"))
check("старт ставки прежний, пауз 1", b.get("start_equity") == 300.0 and b.get("pauses") == 1 and b.get("resumed_ms"))
check("в журнале — «торговля возобновлена»", "торговля возобновлена" in log_text())
snap(250.0); time.sleep(0.5)
check("после паузы капитал снова отслеживается", bet().get("last_equity") == 250.0)
# второй обвал → пауза → ручная остановка: ставка закончена, в 00:00 бот сам не запустится
set_btc(-5.5)
wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "closing")
snap(249.0, positions=0, ts=P.now_ms() + 60_000)
wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "waiting")
check("второй обвал — снова пауза", bet().get("pauses") == 2)
P.live_stop_manual()
check("«Остановить» на паузе — ставка остановлена с итогом", bet().get("state") == "stopped" and bet().get("final_equity") == 249.0)
set_btc(-1.0)
n_paths = len(paths)
CLOCK["off"] += 3 * 86_400_000
time.sleep(0.4)
check("после остановки бот сам не запускается", len(paths) == n_paths and not P.live_status()["running"]
      and not P.load_json(P.LIVE_STATE, {}).get("btc_pause"))
# панель перезапустилась посреди закрытия: процесса panic нет — поднять снова
P.bet_reset(); snap(300.0, ts=1); P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
snap(300.0); wait_for(lambda: bet().get("state") == "running")
set_btc(-5.5)
wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "closing")
wait_for(lambda: P.load_json(P.LIVE_STATE, {}).get("cfg_path", "").endswith("live_panic.json") and P.live_status()["running"])
ls = P.load_json(P.LIVE_STATE, {})
P.proc.stop_tree(ls["pid"], ctime=ls.get("ctime"))
n_paths = len(paths)
check("процесс panic пропал посреди закрытия — поднят снова",
      wait_for(lambda: len(paths) > n_paths and paths[-1].endswith("live_panic.json")) and wait_for(lambda: P.live_status()["running"]))
snap(299.0, positions=0, ts=P.now_ms() + 60_000)
wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "waiting")
P.live_stop_manual()
set_btc(-1.0)
P.bet_clock_ms = _clock

# 9. бумага: правило панели на поминутном капитале (без passivbot) и отрезки подряд на подставном passivbot
M, H0 = 60_000, 1_790_000_000_000 - (1_790_000_000_000 % 3_600_000)
t = [H0 + i * M for i in range(600)]
R5 = {"btc_drop": 0.05, "btc_hours": 12}
flat_btc = [[H0 - 24 * 3_600_000 + h * 3_600_000, 100.0] for h in range(40)]
crash_btc = [[o, (94.0 if o >= H0 + 2 * 3_600_000 else 100.0)] for o, _ in flat_btc]   # свеча 2-го часа закрылась на −6%
check("бумага: BTC спокоен, ×2,5 и −40% — отрезок идёт (цели и стопа панели нет)",
      P.apply_bet_rules(t, [100.0] * 100 + [250.0] * 250 + [60.0] * 250, R5, flat_btc) == (None, None))
i, k = P.apply_bet_rules(t, [100.0] * 600, R5, crash_btc)
check("бумага: обвал BTC — с конца часа, когда свеча закрылась", k == "обвал BTC" and t[i] == H0 + 3 * 3_600_000, f"{k} {i}")
check("бумага: выход по BTC выключен — отрезок идёт", P.apply_bet_rules(t, [100.0] * 600, dict(R5, btc_drop=0), crash_btc) == (None, None))
eq = [100.0] * 600
eq[150:] = [8.0] * 450
check("бумага: капитал ≤ 10% — «сгорела» раньше обвала BTC", P.apply_bet_rules(t, eq, R5, crash_btc) == (150, "сгорела"))
T0 = int(pd.Timestamp("2026-07-01", tz="UTC").value // 1_000_000)
closes = []
for h in range(40 * 24):
    tt = T0 - 2 * 86_400_000 + h * 3_600_000
    c = 100.0 if tt < int(pd.Timestamp("2026-07-10 15:00", tz="UTC").value // 1_000_000) else 94.0   # −6% с 15:00 10.07
    closes.append([tt, c])
check("btc_crash_at: 10.07 15:30 ещё нет (свеча 15:00 не закрыта)",
      not P.btc_crash_at(closes, int(pd.Timestamp("2026-07-10 15:30", tz="UTC").value // 1_000_000), 0.05, 12))
check("btc_crash_at: 10.07 16:00 — обвал",
      P.btc_crash_at(closes, int(pd.Timestamp("2026-07-10 16:00", tz="UTC").value // 1_000_000), 0.05, 12))
check("btc_crash_at: 11.07 00:02 — обвал ещё идёт (в окне 12 ч есть 100)",
      P.btc_crash_at(closes, int(pd.Timestamp("2026-07-11 00:02", tz="UTC").value // 1_000_000), 0.05, 12))
check("btc_crash_at: 12.07 00:02 — нет", not P.btc_crash_at(closes, int(pd.Timestamp("2026-07-12 00:02", tz="UTC").value // 1_000_000), 0.05, 12))


def fake_paper_run(kind, cfg, stage_cb):
    bt = cfg["backtest"]
    t0 = pd.Timestamp(bt["start_date"], tz="UTC")
    tt = pd.date_range(t0, pd.Timestamp(bt["end_date"], tz="UTC"), freq="1min")
    days = (tt - t0).total_seconds().values / 86400
    d = os.path.join(tmp, "paper_run", bt["start_date"])
    os.makedirs(d, exist_ok=True)
    pd.DataFrame({"timestamp": tt.strftime("%Y-%m-%d %H:%M:%S"), "usd_total_equity": bt["starting_balance"] * (1 + days / 100)}
                 ).to_csv(os.path.join(d, "balance_and_equity.csv.gz"), index=False)
    pd.DataFrame([{"timestamp": (t0 + pd.Timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), "type": "close_panic_long",
                   "coin": "APT", "pnl": -1.0, "fee_paid": 0.0}]).to_csv(os.path.join(d, "fills.csv"), index=False)
    PAPER_COINS.append(list(cfg["live"]["approved_coins"]["long"]))
    return d


PAPER_COINS = []


_run, _btc = P.run_passivbot, P.kucoin_btc_closes
P.run_passivbot, P.kucoin_btc_closes = fake_paper_run, lambda a, b: closes
pr = P.run_paper_bets(P.settings(), "2026-07-01", "2026-07-20", lambda x: None)
P.run_passivbot, P.kucoin_btc_closes = _run, _btc
bb = pr["bets"]
check("бумага: выход по BTC 10.07 16:00, продолжение 12.07 (11.07 обвал ещё шёл), стоп passivbot с перезапуском не обрывает",
      len(bb) == 2 and bb[0]["kind"] == "обвал BTC" and bb[0]["end"] == "2026-07-10 16:00" and bb[1]["start"] == "2026-07-12"
      and bb[1]["kind"] == "идёт", json.dumps(bb, ensure_ascii=False)[:300])
check("бумага: второй отрезок — на остаток счёта после выхода", abs(bb[1]["start_equity"] - bb[0]["final_equity"]) < 0.01,
      f"{bb[0]['final_equity']} → {bb[1]['start_equity']}")
check("бумага: правила в итоге — выход по BTC и фильтр разогнанных ×3",
      pr["rules"] == {"btc_drop": 0.05, "btc_hours": 12, "hot_ratio": 3.0}, str(pr["rules"]))
P.save_json(os.path.join(tmp, "paper.json"), {"start": "2026-07-01", "settings": dict(P.settings(), preset="turbo_smart3",
                                                                                         target_mult=2.0)})
seen = {}
_start_job = P.start_job
P.start_job = lambda kind, fn: seen.update(kind=kind, fn=fn)
P.paper_refresh(force=True)
P.start_job = _start_job
P.run_passivbot, P.kucoin_btc_closes = fake_paper_run, lambda a, b: closes
pr2 = seen["fn"](lambda x: None) if seen.get("kind") == "paper" else None
P.run_passivbot, P.kucoin_btc_closes = _run, _btc
check("бумага, начатая на убранном пресете, считается на Turbo 5.0", pr2 and pr2["preset"] == "turbo_ema5", str((pr2 or {}).get("preset")))
# фильтр разогнанных в бумаге: APT за 30 дней до 01.07 — от 1 до 3,5 (×3,5) → первый отрезок без APT; ко второму (12.07)
# минимум 10.06 ушёл из окна — APT снова в работе
HC = "APT" if "APT" in P.MC.default_coins() else [c for c in P.MC.default_coins() if c != "BTC"][0]   # монета сценария из живого списка
HOTC[HC] = {"2026-06-10": 1.0, "2026-06-20": 3.5}
PAPER_COINS.clear()
P.run_passivbot, P.kucoin_btc_closes = fake_paper_run, lambda a, b: closes
pr3 = P.run_paper_bets(P.settings(), "2026-07-01", "2026-07-20", lambda x: None)
P.run_passivbot, P.kucoin_btc_closes = _run, _btc
HOTC.clear()
check("бумага: отрезок с 01.07 — без разогнанной APT (×3,5), с 12.07 — APT снова в работе",
      len(PAPER_COINS) == 2 and HC not in PAPER_COINS[0] and HC in PAPER_COINS[1] and len(PAPER_COINS[0]) == len(PAPER_COINS[1]) - 1
      and pr3["bets"][0]["hot"] == {HC: 3.5} and pr3["bets"][1]["hot"] == {}, json.dumps([b.get("hot") for b in pr3["bets"]]))

# 10. гонки: кнопка в панели или Telegram срабатывает, пока наблюдатель ставки ждёт цену BTC (сетевой запрос до 15 с)
real_step, real_cached = P.bet_step, P.btc_move_cached
P.bet_step = lambda: None                    # фоновый наблюдатель на время проверки ничего не делает
time.sleep(0.3)
set_btc(-1.0)
P.bet_reset(); snap(300.0, ts=1); P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
snap(300.0); real_step()
check("гонки: ставка идёт", bet().get("state") == "running")


def lock_during_fetch(hours, ttl=60):        # «Зафиксировать» нажали, пока наблюдатель ждал ответ биржи
    P.lock_profit("вручную из панели")
    return {"btc": 1.0, "high": 1.0, "hours": hours, "drop_pct": -1.0, "checked": "тест"}


P.btc_move_cached = lock_during_fetch
snap(310.0); real_step()
P.btc_move_cached = real_cached
check("гонки: фиксация во время запроса цены BTC не затёрта наблюдателем", bet().get("state") == "locking", bet().get("state"))
snap(309.0, positions=0, ts=P.now_ms() + 60_000); real_step()
check("гонки: после закрытия — ставка закрыта, бот остановлен",
      bet().get("state") == "done" and wait_for(lambda: not P.live_status()["running"]))
# пауза после обвала: запуск и «Новая ставка» — отказ; «Завершить ставку» в момент решения о возобновлении — бот не стартует
CLOCK = {"off": 0}
_clock = P.bet_clock_ms
P.bet_clock_ms = lambda: _clock() + CLOCK["off"]
P.bet_reset(); snap(300.0, ts=1); P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
snap(300.0); real_step()
set_btc(-5.5); real_step()
check("гонки: обвал — пауза, закрываю позиции", (bet().get("btc_pause") or {}).get("state") == "closing")
snap(298.0, positions=0, ts=P.now_ms() + 60_000); real_step()
check("гонки: позиции закрыты — ждём следующий день", (bet().get("btc_pause") or {}).get("state") == "waiting"
      and wait_for(lambda: not P.live_status()["running"]))
for name, fn in (("запуск", lambda: P.live_start({"confirm": "ЗАПУСК"})), ("новая ставка", P.bet_reset)):
    try:
        fn(); check(f"гонки: на паузе {name} — отказ", False)
    except P.UIError as e:
        check(f"гонки: на паузе {name} — отказ с объяснением", "Завершить ставку" in str(e), str(e)[:90])
check("гонки: ставка осталась на паузе", bet().get("state") == "btc_pause")


def stop_during_fetch(hours, ttl=60):        # «Завершить ставку» нажали ровно в 00:02, пока панель спрашивала цену BTC
    P.live_stop_manual()
    return {"btc": 1.0, "high": 1.0, "hours": hours, "drop_pct": -1.0, "checked": "тест"}


set_btc(-1.0)
n_paths = len(paths)
CLOCK["off"] = bet()["btc_pause"]["resume_ms"] - _clock() + 180_000
P.btc_move_cached = stop_during_fetch
real_step()
P.btc_move_cached = real_cached
time.sleep(0.3)
check("гонки: «Завершить ставку» в момент возобновления — ставка остановлена, бот не запущен",
      bet().get("state") == "stopped" and len(paths) == n_paths and not P.live_status()["running"]
      and not P.load_json(P.LIVE_STATE, {}).get("want_running"), f"{bet().get('state')}, запусков {len(paths) - n_paths}")
P.bet_clock_ms = _clock
P.bet_step = real_step

# 11. защита от делистинга (coin_guard.py): данные KuCoin подставные; ставка продолжается
P.GUARD, P.GUARD_IGNORED = os.path.join(tmp, "coin_guard.json"), os.path.join(tmp, "coin_guard_ignored.json")
P.HOT_CLOSES = os.path.join(tmp, "coin_guard_closes.json")
P.ROOT = tmp                                   # конфиги перезапуска — во временную папку
CMAP = P.guard_coins()
KU = {"expire": {}, "st": [], "gone": set(), "ann": []}


def fake_fetch(sess, spot=True):
    fut = {sym: {"expire": KU["expire"].get(c), "turnover": 5e7} for c, sym in CMAP.items() if c not in KU["gone"]}
    return {"futures": fut, "announcements": list(KU["ann"]), "st": list(KU["st"]) if spot else None}


def guard_now():
    P.coin_guard_fetch(force=True)
    return (P.load_json(P.GUARD, {}) or {}).get("coins") or {}


def snap_coins(*coins, eq=300.0, ahead_ms=1000):
    json.dump({"meta": {"current_cycle_ts_ms": P.now_ms() + ahead_ms}, "account": {"equity": eq, "balance_raw": eq},
               "positions": {f"{c}/USDT:USDT": {"long": {"size": 1.0, "price": 10.0}, "short": {"size": 0.0}} for c in coins},
               "open_orders": {}, "hsl": {}}, open(P.MONITOR, "w"))


P.CG.fetch = fake_fetch
P.spawn_path = path_spawn
set_btc(-1.0)
check("защита: монеты бота — все из списка", len(CMAP) == len(P.MC.default_coins()) > 30, str(len(CMAP)))
check("защита: всё в порядке — монет под защитой нет", guard_now() == {})
P.bet_reset(); snap_coins(eq=300.0); P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
snap_coins("ZEC", "SOL")
check("ставка идёт", wait_for(lambda: bet().get("state") == "running"))
check("боевой конфиг Turbo: список исключений — файл", cfgs[-1]["live"]["ignored_coins"] == P.GUARD_IGNORED)
pid0, n_paths = P.load_json(P.LIVE_STATE, {})["pid"], len(paths)
KU["expire"]["ZEC"] = P.now_ms() + 3 * 86_400_000
g = guard_now()
check("назначен делистинг ZEC — «закрыть», ZEC в файле исключений",
      (g.get("ZEC") or {}).get("level") == "close" and "ZEC" in P.load_json(P.GUARD_IGNORED, {})["long"])
check("в журнале — «защита от делистинга: ZEC — KuCoin закроет контракт …»", "защита от делистинга: ZEC — KuCoin закроет контракт" in log_text())
snap_coins("ZEC", "SOL")
P.coin_guard_close_step()
L = P.load_json(P.LIVE_STATE, {})
cfg_live = P.load_json(P.cfg_path_for("live"), {})
check("по ZEC открыта позиция — бот перезапущен с panic только для ZEC, ставка идёт",
      L["pid"] != pid0 and len(paths) == n_paths + 1 and L.get("guard_panic") == ["ZEC"] and L["want_running"]
      and cfg_live.get("coin_overrides") == {"ZEC": {"live": {"forced_mode_long": "panic"}}} and bet().get("state") == "running",
      str(cfg_live.get("coin_overrides")))
check("в журнале — «закрываю позицию по ZEC … ставка продолжается»", "закрываю позицию по ZEC" in log_text()
      and "ставка продолжается" in log_text())
snap_coins("ZEC", "SOL"); P.coin_guard_close_step()
check("повторно не перезапускает", len(paths) == n_paths + 1)
snap_coins("SOL"); P.coin_guard_close_step(); P.coin_guard_close_step()
check("позиция по ZEC закрылась — запись один раз", log_text().count("защита от делистинга: позиция по ZEC закрыта") == 1)
# обвал BTC: пауза — защита бота не трогает
CLOCK = {"off": 0}
_clock = P.bet_clock_ms
P.bet_clock_ms = lambda: _clock() + CLOCK["off"]
set_btc(-5.6)
wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "closing")
snap_coins(eq=290.0, ahead_ms=60_000)
wait_for(lambda: (bet().get("btc_pause") or {}).get("state") == "waiting")
KU["expire"]["DASH"] = P.now_ms() + 3 * 86_400_000
n_paths = len(paths)
guard_now(); P.coin_guard_close_step()
check("на паузе после обвала BTC: делистинг DASH записан, бот не запускается",
      "DASH" in P.load_json(P.GUARD, {})["coins"] and len(paths) == n_paths and not P.live_status()["running"])
P.live_stop_manual()
set_btc(-1.0)
P.bet_clock_ms = _clock
check("панель видит защиту (state.coin_guard)", "DASH" in ((P.state().get("coin_guard") or {}).get("coins") or {}))
P.spawn_path = real_spawn_path

# 12. фильтр разогнанных монет (coin_guard.hot, настройка hot_ratio, по умолчанию ×3): монета не покупается, без тревоги
day = lambda k: P.CG.utc_day(P.now_ms() - k * 86_400_000)
check("фильтр: ×22 за 30 дней — разогнана; ×1,5 — нет; сегодняшняя свеча и свечи старше 30 дней не в счёт",
      P.CG.hot({"A": {day(1): 1.0, day(10): 22.0}, "B": {day(2): 1.0, day(5): 1.5}, "C": {day(0): 1.0, day(3): 9.0},
                "D": {day(40): 1.0, day(3): 9.0}}, 3, P.now_ms()) == {"A": 22.0})
check("фильтр выключен (0) — разогнанных нет", P.CG.hot({"A": {day(1): 1.0, day(10): 22.0}}, 0, P.now_ms()) == {})
check("настройка по умолчанию — ×3 (пресет Turbo 5.0)", P.settings()["hot_ratio"] == 3.0)
KU["expire"].clear()
P.spawn_path = path_spawn
P.bet_reset(); snap_coins(eq=300.0); P.live_start({"confirm": "ЗАПУСК"}); time.sleep(0.2)
check("ставка идёт", wait_for(lambda: bet().get("state") == "running"))
n_paths = len(paths)
HOTC["ZEN"] = {day(2): 1.0, day(20): 4.0}
P.GUARD_CACHE["hot_t"] = 0
g = guard_now()
z = g.get("ZEN") or {}
check("ZEN ×4 за 30 дней — «не входить», в файле исключений, причина с фильтром ×3",
      z.get("level") == "ignore" and z.get("hot") and "ZEN" in P.load_json(P.GUARD_IGNORED, {})["long"] and "×3" in z.get("reason", ""),
      str(z))
check("в журнале — «фильтр разогнанных монет: ZEN …», не «защита от делистинга»",
      "фильтр разогнанных монет: ZEN — цена разогнана" in log_text() and "защита от делистинга: ZEN" not in log_text())
check("разогнанная монета бота не перезапускает, ставка идёт", len(paths) == n_paths and bet().get("state") == "running")
check("в итоге проверки — порог и время цен", (P.load_json(P.GUARD, {}) or {}).get("hot_ratio") == 3.0
      and (P.load_json(P.GUARD, {}) or {}).get("hot_checked"))
# перезапуск панели: цены — из файла, без нового запроса к KuCoin; ZEN по-прежнему «не входить», без «снова в работе»
calls_closes = []
_fc = P.CG.fetch_closes
P.CG.fetch_closes = lambda *a, **k: calls_closes.append(1) or _fc(*a, **k)
P.GUARD_CACHE.update(closes={}, hot_t=0, t=0)
log_before = log_text().count("ZEN снова в работе")
P.coin_guard_fetch()
P.CG.fetch_closes = _fc
check("после перезапуска панели цены — из файла, ZEN сразу «не входить», в журнале не «качается»",
      not calls_closes and (P.load_json(P.GUARD, {})["coins"].get("ZEN") or {}).get("hot")
      and log_text().count("ZEN снова в работе") == log_before, str(len(calls_closes)))
P.GUARD_CACHE["hot_busy"] = False
try:
    P.save_settings({"hot_ratio": 25})
    check("порог 25 — ошибка", False)
except P.UIError as e:
    check("порог 25 — ошибка", "разогнанных" in str(e), str(e))
P.save_settings({"hot_ratio": 0})
check("фильтр выключен — ZEN снова в работе, запись в журнале",
      "ZEN" not in guard_now() and "фильтр разогнанных монет: ZEN снова в работе" in log_text())
P.save_settings({"hot_ratio": 3})
HOTC.clear()
P.live_stop_manual()
check("«Остановить бота» во время ставки — ставка «остановлена», а не «идёт»; в журнале — позиции на бирже остались",
      bet().get("state") == "stopped" and "открытые позиции и ордера на бирже остались" in log_text(), str(bet().get("state")))
P.spawn_path = real_spawn_path
# 13. список монет раз в месяц (kucoin_refresh.py): первый запуск — нынешний список, 1-го числа (после 00:10 UTC) —
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
# бумага Turbo: отрезок с 20.10 — по октябрьскому списку (с ARK, без NEWC), даже когда справочник уже ноябрьский
PAPER_COINS.clear()
P.run_passivbot, P.kucoin_btc_closes = fake_paper_run, lambda a, b: closes
pr_h = P.run_paper_bets(P.settings(), "2026-10-20", "2026-10-25", lambda x: None)
P.run_passivbot, P.kucoin_btc_closes = _run, _btc
check("бумага: отрезок с 20.10 — октябрьский список монет (с ARK, без NEWC), а не нынешний",
      PAPER_COINS and "ARK" in PAPER_COINS[0] and "NEWC" not in PAPER_COINS[0], str(len(PAPER_COINS)))
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
