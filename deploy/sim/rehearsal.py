#!/usr/bin/env python3
"""Репетиция реальной торговли на сервере — без биржи и без денег.

Копии обеих панелей (тот же код) на портах 18770/18771 в /tmp/tb_sim, вместо passivbot — sim_passivbot.py,
который ускоренно проигрывает ход торговли, посчитанный настоящим ядром passivbot на реальных ценах
(replays/*.json из make_replays.py). Telegram-бот работает по-настоящему, но «в холостую»: сообщения
записываются в стенограмму, а не отправляются. Рабочие боты, их данные и настоящий Telegram не затрагиваются.

  /opt/tradebot/turbo/venv/bin/python rehearsal.py     (из папки с sim_passivbot.py, sim_panel.py и replays/)
  … rehearsal.py --src /tmp/tb_src --only 2,4      (код панелей — из --src/<app>, а не из /opt/tradebot:
                                                     репетиция новой версии до выкладки; venv — рабочий)
Итог: /tmp/tb_sim/results.json и /tmp/tb_sim/transcript.md
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime

KIT = os.path.dirname(os.path.abspath(__file__))
SIM = "/tmp/tb_sim"
PROD = "/opt/tradebot"
SRC = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else PROD     # откуда копировать код панелей
# по одной стратегии на бота (02.10.2026); выход при обвале BTC включён (по умолчанию в пресетах) — в ходе торговли
# сценария есть часовые цены BTC (make_replays.py --btc), и стенд смотрит на BTC во время сценария (sim_panel.py)
# 15.10.2026: основной бот удалён, остался turbo. Стратегия ema_wide и её сценарии ушли вместе с ним;
# общий код панели живёт в panel/, у бота — в turbo/tb4/panel/.
APPS = {"turbo": dict(name="Турбо", port=18771, user="kucoin_turbo",
                      settings={"preset": "turbo_ema5", "balance": 300.0, "leverage": 10})}
PANEL_FACTOR = 60
PANELS = {}
CLOCK = {}
START_REAL = {}
MSGS = []
RESULTS = []
CUR = {"name": ""}


# ------------------------------------------------------------------ стенд
def setup():
    shutil.rmtree(SIM, ignore_errors=True)
    for app, a in APPS.items():
        dst, src = f"{SIM}/{app}", f"{SRC}/{app}"
        shutil.copytree(f"{src}/tb4", f"{dst}/tb4", ignore=shutil.ignore_patterns("data", "results", "__pycache__"))
        shutil.copytree(f"{src}/configs/examples", f"{dst}/configs/examples")
        if app == "turbo":
            shutil.copytree(f"{src}/deploy", f"{dst}/deploy", ignore=shutil.ignore_patterns("sim", "out", "__pycache__"))
            # телеграм-бот вынесен из deploy в отдельную папку SRC/telegram, но репетиция его импортирует
            tg_dir = f"{SRC}/telegram"
            if os.path.isdir(tg_dir):
                shutil.copytree(tg_dir, f"{dst}/deploy", ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
        os.makedirs(f"{dst}/venv/bin")
        with open(f"{dst}/venv/bin/passivbot", "w") as f:
            f.write(f'#!/bin/sh\nexec /usr/bin/python3 {KIT}/sim_passivbot.py "$@"\n')
        os.chmod(f"{dst}/venv/bin/passivbot", 0o755)
        placeholder = "SIMULATION-NOT-A-REAL-KEY"
        with open(f"{dst}/api-keys.json", "w") as f:
            json.dump({a["user"]: {"exchange": "kucoin", "key": placeholder, "secret": placeholder,
                                   "passphrase": placeholder}}, f)
        os.makedirs(f"{dst}/tb4/panel/data")
        with open(f"{dst}/tb4/panel/data/settings.json", "w") as f:
            json.dump(a["settings"], f)
        open(f"{dst}/tb4/panel/data/live_console.log", "a").close()
        os.makedirs(f"{SIM}/{app}_sim")
    os.makedirs(f"{SIM}/reports")


def start_panel(app):
    a = APPS[app]
    env = dict(os.environ, TB_LITE="1", SIM_DIR=f"{SIM}/{app}_sim", PYTHONUTF8="1", MALLOC_ARENA_MAX="2")
    log = open(f"{SIM}/panel_{app}.log", "a")
    p = subprocess.Popen([f"{PROD}/{app}/venv/bin/python", f"{KIT}/sim_panel.py", f"{SIM}/{app}", str(a["port"]),
                          str(PANEL_FACTOR)], cwd=f"{SIM}/{app}", env=env, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
    PANELS[app] = p
    for _ in range(100):
        if "error" not in api(app, "/api/state", timeout=2):
            return p
        time.sleep(0.2)
    raise RuntimeError(f"панель {app} не поднялась")


def kill_panel(app):
    p = PANELS.get(app)
    if p and p.poll() is None:
        os.kill(p.pid, signal.SIGKILL)
        p.wait()


def api(app, path, body=None, timeout=90):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{APPS[app]['port']}{path}", data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.load(e).get("error")}
        except ValueError:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def sim_pids(app=None):
    out = subprocess.run(["pgrep", "-f", "sim_passivbot.py live"], capture_output=True, text=True).stdout.split()
    pids = []
    for pid in out:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        if app is None or cwd == f"{SIM}/{app}":
            pids.append(int(pid))
    return pids


# ------------------------------------------------------------------ время репетиции
def sim_ms(real=None):
    real = time.time() if real is None else real
    return int(CLOCK["sim_start_ms"] + (real - CLOCK["t0_real"]) * CLOCK["speed"] * 1000)


def sim_dt(real=None):
    return datetime.fromtimestamp(sim_ms(real) / 1000, R.TZ)


def write_sim(app, name, data):
    with open(f"{SIM}/{app}_sim/{name}.tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(f"{SIM}/{app}_sim/{name}.tmp", f"{SIM}/{app}_sim/{name}")


def set_speed(app, speed):
    """Сменить скорость без скачка времени (подставной бот перечитывает часы на каждом шаге)."""
    now = time.time()
    CLOCK.update(sim_start_ms=sim_ms(now), t0_real=now, speed=speed)
    write_sim(app, "clock.json", CLOCK)
    T.TIER_EVERY_S, T.ERR_EVERY_S, T.COIN_ORANGE_EVERY_S = 1800 / speed, 3600 / speed, 6 * 3600 / speed


# ------------------------------------------------------------------ Telegram «в холостую»
def telegram_setup():
    global R, T
    sys.path.insert(0, f"{SIM}/turbo/deploy")
    import report as R
    import telegram_bot as T
    R.BASE, R.REP = SIM, f"{SIM}/reports"
    R.EQ_CSV, R.ALERTS, R.STATE = f"{SIM}/reports/equity.csv", f"{SIM}/reports/alerts.log", f"{SIM}/reports/.state.json"
    R.APPS = tuple((app, a["name"], a["port"], a["user"]) for app, a in APPS.items())
    T.NAMES = {app: a["name"] for app, a in APPS.items()}
    T.PORTS = {app: a["port"] for app, a in APPS.items()}
    real_sh = R.sh
    R.sh = lambda *a: "active" if "is-active" in a else ("0" if "NRestarts" in a else real_sh(*a))
    R.process_memory = lambda: []
    R.now = lambda: sim_dt()                  # отчёты, «за сутки», ночной режим — по времени репетиции
    T.now_ts = lambda: sim_ms() / 1000        # и счётчики сделок за сутки
    real_collect = R.collect

    def collect():
        rows = real_collect()
        for r in rows:
            if r["running"] and r["app"] in START_REAL:
                r["started"] = sim_dt(START_REAL[r["app"]]).strftime("%Y-%m-%d %H:%M")
        return rows
    R.collect = collect
    T.DIR = f"{SIM}/telegram"
    T.CFG, T.STATE = f"{T.DIR}/config.json", f"{T.DIR}/state.json"

    def fake_api(token, method, http_timeout=20, **p):
        if method in ("sendMessage", "editMessageText"):
            MSGS.append({"scenario": CUR["name"], "sim": sim_dt().strftime("%d.%m.%Y %H:%M"), "method": method,
                         "text": p.get("text", ""), "loud": not p.get("disable_notification", False),
                         "buttons": [b["text"] for row in (p.get("reply_markup") or {}).get("inline_keyboard", [])
                                     for b in row]})
        return {"message_id": len(MSGS)}
    T.api = fake_api


def new_bot():
    shutil.rmtree(T.DIR, ignore_errors=True)
    T.save_private(T.CFG, {"token": "SIM", "owner": 1, "owner_name": "репетиция", "settings": dict(T.DEFAULTS)})
    return T.Bot()


def user_says(bot, text):
    bot.on_message({"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": text})


def user_taps(bot, data):
    bot.on_callback({"id": "q", "from": {"id": 1}, "data": data, "message": {"message_id": 7}})


# ------------------------------------------------------------------ сценарий
def run(name, app, replay, speed, until, max_real_s, actions=(), control=None, start_offset_days=0.0, on_tick=None):
    CUR["name"] = name
    print(f"\n=== {name}", flush=True)
    res = {"name": name, "app": app, "checks": [], "notes": []}
    api(app, "/api/live/stop", {})
    if app == "turbo":
        api(app, "/api/bet/reset", {})
    shutil.rmtree(f"{SIM}/{app}/monitor", ignore_errors=True)
    with open(f"{KIT}/replays/{replay}.json", encoding="utf-8") as f:
        rp = json.load(f)
    shutil.copy(f"{KIT}/replays/{replay}.json", f"{SIM}/{app}_sim/replay.json")
    CLOCK.update(t0_real=time.time() + 1.0, sim_start_ms=rp["equity"][0][0] + int(start_offset_days * 86_400_000),
                 speed=speed)
    write_sim(app, "clock.json", CLOCK)
    write_sim(app, "control.json", control or {})
    write_sim(app, "exchange.json", {})
    write_sim(app, "guard_data.json", {})             # защита от делистинга: «KuCoin» без делистингов
    try:
        os.remove(f"{SIM}/{app}/tb4/panel/data/coin_guard.json")
    except OSError:
        pass
    for fn in os.listdir(R.REP):                  # свежие отчёты и история капитала на каждый сценарий
        os.remove(os.path.join(R.REP, fn))
    T.TIER_EVERY_S, T.ERR_EVERY_S, T.COIN_ORANGE_EVERY_S = 1800 / speed, 3600 / speed, 6 * 3600 / speed
    bot = new_bot()
    CUR["bot"] = bot                              # для on_tick (команды в Telegram по событию)
    bot.check()                                   # запомнить конец журналов до запуска
    time.sleep(max(0.0, CLOCK["t0_real"] - time.time()))
    r = api(app, "/api/live/start", {"confirm": "ЗАПУСК"})
    if r.get("error"):
        res["notes"].append(f"запуск: {r['error'][:200]}")
    START_REAL[app] = time.time()
    t_real0, last_hour, last_day, samples = time.time(), None, None, []
    acts = sorted(actions, key=lambda a: a[0])
    end_ms = rp["equity"][-1][0]
    while True:
        now_sim = sim_ms()
        st = api(app, "/api/state")
        rows = R.collect()
        if now_sim // 3_600_000 != last_hour:
            last_hour = now_sim // 3_600_000
            R.append_equity(rows)
        d = sim_dt()
        if (d.hour, d.minute) >= (7, 5) and d.date() != last_day:
            last_day = d.date()
            R.daily(rows, R.alerts_for(rows))
        bot.check()
        live = st.get("live") or {}
        sn = live.get("snapshot") or {}
        samples.append({"sim": now_sim, "running": live.get("running"), "equity": sn.get("equity"),
                        "positions": len(sn.get("positions") or []), "hsl": sn.get("hsl"), "bet": st.get("bet"),
                        "gave_up": live.get("gave_up"), "procs": len(sim_pids(app))})
        if on_tick:
            on_tick(st)
        while acts and now_sim >= rp["equity"][0][0] + acts[0][0] * 86_400_000:
            _, fn = acts.pop(0)
            try:
                fn(bot, res, st)
            except Exception as exc:
                res["notes"].append(f"действие упало: {exc}")
                traceback.print_exc()
        if until(st, now_sim, end_ms) or time.time() - t_real0 > max_real_s:
            if time.time() - t_real0 > max_real_s:
                res["notes"].append(f"остановлено по времени ({max_real_s} с)")
            break
        time.sleep(0.3)
    for _ in range(3):
        bot.check()
        time.sleep(0.3)
    res["final_state"] = api(app, "/api/state")
    res["samples"] = samples
    res["replay_stats"] = rp["stats"]
    res["bot"] = bot
    return res


def check(res, name, cond, detail=""):
    res["checks"].append({"name": name, "ok": bool(cond), "detail": str(detail)})
    print(("  OK   " if cond else "  FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def msgs(scn, *needles, loud=None):
    out = [m for m in MSGS if m["scenario"] == scn and m["method"] == "sendMessage"
           and all(n in m["text"] for n in needles)]
    return [m for m in out if loud is None or m["loud"] == loud]


# ------------------------------------------------------------------ сценарии
def scenario_failures():
    name = "3. Сбои: падение бота, перезапуск панели, перезагрузка сервера, бот падает при запуске, остановка из Telegram"
    CUR["name"] = name
    res = {"name": name, "app": "turbo", "checks": [], "notes": []}
    print(f"\n=== {name}", flush=True)
    with open(f"{KIT}/replays/turbo5_pause.json", encoding="utf-8") as f:
        rp = json.load(f)
    shutil.copy(f"{KIT}/replays/turbo5_pause.json", f"{SIM}/turbo_sim/replay.json")
    shutil.rmtree(f"{SIM}/turbo/monitor", ignore_errors=True)
    CLOCK.update(t0_real=time.time(), sim_start_ms=rp["equity"][0][0] + 5 * 86_400_000, speed=3600)
    write_sim("turbo", "clock.json", CLOCK)
    write_sim("turbo", "exchange.json", {})
    write_sim("turbo", "control.json", {"crash_after_real_s": 4})
    T.TIER_EVERY_S, T.ERR_EVERY_S = 1800 / 3600, 3600 / 3600
    bot = new_bot()
    bot.check()
    api("turbo", "/api/live/start", {"confirm": "ЗАПУСК"})
    START_REAL["turbo"] = time.time()

    def pump(seconds):
        t0 = time.time()
        while time.time() - t0 < seconds:
            bot.check()
            time.sleep(0.3)

    # 5.1 бот внезапно упал
    pump(3)
    pid1 = sim_pids("turbo")
    pump(6)
    pid2 = sim_pids("turbo")
    check(res, "3.1 бот упал → панель подняла его сама", len(pid2) == 1 and pid1 != pid2, f"{pid1} → {pid2}")
    check(res, "3.1 Telegram: «автовосстановление» со звуком", msgs(name, "автовосстановление", loud=True))
    pump(2)
    check(res, "3.1 Telegram: в сводке ошибок — суть падения, а не «Traceback»", msgs(name, "RuntimeError"))
    # 5.2 перезапуск панели (обновление, сбой панели) — бот не должен задеваться
    before = sim_pids("turbo")
    n_msgs = len(msgs(name, "автовосстановление"))
    n_down = len(msgs(name, "процесса нет"))
    kill_panel("turbo")
    pump(2)
    start_panel("turbo")
    pump(4)
    after = sim_pids("turbo")
    st = api("turbo", "/api/state")
    check(res, "3.2 панель перезапущена — бот тот же процесс, не дублируется", before == after and st["live"]["running"],
          f"{before} → {after}")
    check(res, "3.2 без лишнего «автовосстановления»", len(msgs(name, "автовосстановление")) == n_msgs)
    check(res, "3.2 пока панели нет — только «панель не отвечает», без ложного «процесса нет»",
          len(msgs(name, "процесса нет")) == n_down)
    # 5.3 перезагрузка сервера: пропали и панель, и бот
    kill_panel("turbo")
    for pid in sim_pids("turbo"):
        os.kill(pid, signal.SIGKILL)
    pump(1)
    start_panel("turbo")
    pump(5)
    check(res, "3.3 после «перезагрузки» панель сама подняла бота", len(sim_pids("turbo")) == 1
          and api("turbo", "/api/state")["live"]["running"])
    # 5.4 бот падает сразу при запуске (например, неверный ключ) — не дёргать без конца
    api("turbo", "/api/live/stop", {})
    write_sim("turbo", "control.json", {})
    api("turbo", "/api/live/start", {"confirm": "ЗАПУСК"})
    pump(2)
    write_sim("turbo", "control.json", {"crash_on_start": True})
    for pid in sim_pids("turbo"):
        os.kill(pid, signal.SIGKILL)
    pump(12)
    st = api("turbo", "/api/state")
    log = open(f"{SIM}/turbo/tb4/panel/data/live_console.log", encoding="utf-8").read()
    check(res, "3.4 падает при запуске — 3 попытки, затем стоп автоперезапуска",
          st["live"].get("gave_up") and log.count("KuCoin rejected API key") == 3, f"попыток: {log.count('KuCoin rejected API key')}")
    check(res, "3.4 Telegram: «автоперезапуск остановлен» со звуком", msgs(name, "автоперезапуск остановлен", loud=True))
    check(res, "3.4 Telegram: тревога «бот должен работать, но процесса нет»", msgs(name, "Тревога", "процесса нет"))
    write_sim("turbo", "control.json", {})
    api("turbo", "/api/live/start", {"confirm": "ЗАПУСК"})
    START_REAL["turbo"] = time.time()
    pump(4)
    check(res, "3.4 после исправления «Запуск» снова работает, тревога снята",
          api("turbo", "/api/state")["live"]["running"] and msgs(name, "Снято"))
    # 5.5 аварийная остановка из Telegram
    user_taps(bot, "ask:stop:turbo")
    nonce = next(iter(bot.pending), None)
    user_taps(bot, f"do:{nonce}")
    pump(3)
    check(res, "3.5 остановка из Telegram (с подтверждением) — бот остановлен",
          not api("turbo", "/api/state")["live"]["running"] and not sim_pids("turbo"))
    check(res, "3.5 Telegram: подтверждение и «бот остановлен»",
          any("остановлен" in m["text"] for m in MSGS if m["scenario"] == name and m["method"] == "editMessageText"))
    res["bot"] = bot
    return res


def slow_on_lock_factory():
    slowed = {}

    def f(st):              # фиксация занимает минуты: на это время — почти настоящее время
        if (st.get("bet") or {}).get("state") == "locking" and not slowed:
            slowed["at"] = sim_ms()
            set_speed("turbo", 5)
    return f


def scenario_turbo5_lock():
    """Turbo 5.0: обычная ставка (17.11.2025, до обвала BTC), статус, фиксация из Telegram с подтверждением → passivbot
    в режиме panic закрывает позиции → ставка закрыта, бот остановлен."""
    name = "5. Turbo 5.0: ставка, статус и фиксация из Telegram (ноябрь 2025)"
    CUR["name"] = name
    api("turbo", "/api/live/stop", {})
    api("turbo", "/api/bet/reset", {})
    st0 = api("turbo", "/api/state")
    s, params = st0.get("settings") or {}, ((st0.get("coins") or {}).get("params") or {})

    def status_cmd(bot, res, st):
        user_says(bot, "📊 Статус")

    def lock_cmd(bot, res, st):
        user_taps(bot, "ask:lock:turbo")
        user_taps(bot, f"do:{next(iter(bot.pending), None)}")
    res = run(name, "turbo", "turbo5_pause", speed=8640, max_real_s=300, on_tick=slow_on_lock_factory(),
              actions=[(1.0, status_cmd), (1.5, lock_cmd)],
              until=lambda st, t, end: (st.get("bet") or {}).get("state") == "done" and not st["live"]["running"])
    b = res["final_state"].get("bet") or {}
    log = open(f"{SIM}/turbo/tb4/panel/data/live_console.log", encoding="utf-8").read()
    check(res, "единственный пресет 5.0: EMA Anchor 7 × 9, плечо 10, выход по BTC −5%/12 ч",
          list(st0.get("presets") or {}) == ["turbo_ema5"] and s.get("preset") == "turbo_ema5" and params.get("strategy") == "ema_anchor"
          and params.get("exposure") == 9 and params.get("positions") == 7 and s.get("leverage") == 10 and s.get("btc_exit") == 0.05,
          {k: s.get(k) for k in ("preset", "leverage", "btc_exit")})
    check(res, "Telegram: «капитал на старте ставки … без цели»", msgs(name, "капитал на старте ставки", "без цели"))
    check(res, "Telegram /status: «Ставка: идёт … без цели»", msgs(name, "Ставка: идёт", "без цели"))
    check(res, "Telegram: подтверждение фиксации", any("Фиксация началась" in m["text"] for m in MSGS if m["scenario"] == name))
    check(res, "Telegram: «фиксация: вручную из Telegram» со звуком", msgs(name, "фиксация", "вручную из Telegram", loud=True))
    check(res, "бот перезапускался в режиме panic и закрыл позиции", "forced_mode_long=panic" in log and "все позиции закрыты" in log)
    check(res, "ставка закрыта с итогом, бот остановлен",
          b.get("state") == "done" and b.get("final_equity") and not res["final_state"]["live"]["running"],
          f"старт {b.get('start_equity')}, итог {b.get('final_equity')}")
    check(res, "Telegram: «ставка закрыта», без лишнего «⏹ бот остановлен» и тревог",
          msgs(name, "ставка закрыта") and not msgs(name, "⏹") and not msgs(name, "Тревога"))
    api("turbo", "/api/bet/reset", {})
    return res


def scenario_turbo5_pause():
    """Turbo 5.0 (EMA Anchor 7 × 9, плечо 10): обвал BTC 20.11.2025 17:00 UTC → закрыть всё, ставка не заканчивается —
    пауза; в 00:00 21.11 BTC ещё падает — пауза продлена; 22.11 00:00 — торговля в той же ставке. Ход торговли — passivbot
    на пресете 5.0 17–23.11.2025 (как сценарий 4 у основного бота)."""
    name = "6. Turbo 5.0: обвал BTC → пауза до следующего дня → та же ставка (ноябрь 2025)"
    CUR["name"] = name
    api("turbo", "/api/live/stop", {})
    api("turbo", "/api/bet/reset", {})
    st0 = api("turbo", "/api/state")
    s, params = st0.get("settings") or {}, ((st0.get("coins") or {}).get("params") or {})
    with open(f"{KIT}/replays/turbo5_pause.json", encoding="utf-8") as f:
        t_start = json.load(f)["equity"][0][0]
    H = 3_600_000
    phases = [(t_start + 3 * 86_400_000 + 15 * H, 360),          # 20.11 15:00 — за 2 ч до обвала: медленно
              (t_start + 4 * 86_400_000 + 1 * H, 8640),          # 21.11 01:00 — пауза продлена, сутки быстро
              (t_start + 4 * 86_400_000 + 23 * H, 720)]          # 21.11 23:00 — к запуску в 00:00 22.11
    seen = {"resumed": None, "pause_states": set()}

    def on_tick(st):
        while phases and sim_ms() >= phases[0][0]:
            set_speed("turbo", phases.pop(0)[1])
        b = st.get("bet") or {}
        if b.get("state") == "btc_pause":
            seen["pause_states"].add((b.get("btc_pause") or {}).get("state"))
        L = st.get("live") or {}
        if seen["resumed"] is None and L.get("running") and (L.get("mode") or "normal") == "normal" \
                and b.get("state") == "running" and b.get("resumed_ms"):
            seen["resumed"] = sim_ms()

    def status_cmd(bot, res, st):
        user_says(bot, "📊 Статус")
    res = run(name, "turbo", "turbo5_pause", speed=8640, max_real_s=600, on_tick=on_tick, actions=[(3.9, status_cmd)],
              until=lambda st, t, end: t >= end or (seen["resumed"] and t >= seen["resumed"] + 6 * H))
    L, b = res["final_state"].get("live") or {}, res["final_state"].get("bet") or {}
    pauses = [x for x in res["samples"] if not x["running"] and x["procs"] == 0]
    check(res, "пресет 5.0: EMA Anchor 7 × 9, плечо 10, выход по BTC −5%/12 ч с паузой",
          s.get("preset") == "turbo_ema5" and params.get("strategy") == "ema_anchor" and params.get("exposure") == 9
          and params.get("positions") == 7 and s.get("leverage") == 10 and s.get("btc_exit") == 0.05,
          {k: s.get(k) for k in ("preset", "leverage", "btc_exit", "btc_exit_hours")})
    check(res, "Telegram: «обвал BTC … закрываю все позиции» со звуком", msgs(name, "обвал BTC", "закрываю все позиции", loud=True))
    check(res, "ставка не закончилась: закрытие → ожидание", seen["pause_states"] >= {"closing", "waiting"}, str(seen["pause_states"]))
    check(res, "Telegram: «позиции закрыты — пауза, ставка продолжится сама»", msgs(name, "позиции закрыты", "продолжится сама"))
    check(res, "во время паузы бот остановлен (процесса нет)", len(pauses) >= 3, f"проверок с паузой: {len(pauses)}")
    check(res, "Telegram /status во время паузы: «пауза после обвала BTC»", msgs(name, "пауза после обвала BTC"))
    check(res, "нет ложной тревоги и «⏹ бот остановлен» во время паузы", not msgs(name, "Тревога") and not msgs(name, "⏹"))
    check(res, "в 00:00 21.11 BTC ещё падал — «пауза продлена»", msgs(name, "пауза продлена"))
    check(res, "Telegram: «пауза после обвала BTC закончилась — торговля возобновлена»",
          msgs(name, "пауза после обвала BTC закончилась"))
    check(res, "возобновление — не раньше 00:00 22.11 (UTC), та же ставка снова идёт",
          seen["resumed"] and seen["resumed"] >= t_start + 5 * 86_400_000 and L.get("running")
          and b.get("state") == "running" and b.get("pauses") == 1 and b.get("start_equity"),
          datetime.fromtimestamp((seen["resumed"] or 0) / 1000, R.TZ).strftime("%d.%m %H:%M") if seen["resumed"] else "нет")
    check(res, "ровно один процесс бота", max(x["procs"] for x in res["samples"]) == 1)
    api("turbo", "/api/live/stop", {})
    api("turbo", "/api/bet/reset", {})
    return res


def delist_drill(name, app, replay, start_day=0.0, hot=False):
    """Учебный делистинг: в день 1 сценария, когда у бота 2+ позиции, «KuCoin» назначает делистинг первой из них (дата
    закрытия контракта через 3 дня, как у PROMPT 01.10.2026) → панель: монета в исключения, перезапуск passivbot с panic
    только для неё → позиция закрыта, остальные торгуются. hot (Turbo) — затем учебная тревога фильтра разогнанных:
    «KuCoin» показывает, что цена другой монеты бота за 30 дней менялась в 4 раза → не входить, без перезапуска и без
    звука, позицию бот ведёт как обычно."""
    CUR["name"] = name
    api(app, "/api/live/stop", {})
    if app == "turbo":
        api(app, "/api/bet/reset", {})
    with open(f"{KIT}/replays/{replay}.json", encoding="utf-8") as f:
        t0 = json.load(f)["equity"][0][0] + int(start_day * 86_400_000)
    seen = {"coin": None, "at": None, "others": [], "after": None}

    def on_tick(st):
        sn = (st.get("live") or {}).get("snapshot") or {}
        held = [x["symbol"].split("/")[0] for x in sn.get("positions") or []]
        if seen["coin"] is None and sim_ms() >= t0 + 86_400_000 and len(held) >= 2:
            seen.update(coin=held[0], at=sim_ms(), others=held[1:])
            write_sim(app, "guard_data.json", {"expire": {held[0]: sim_ms() + 3 * 86_400_000}})
            set_speed(app, 360)          # проверка «KuCoin» и перезапуск — секунды; позиция не должна успеть закрыться сама
        if seen["coin"] and not seen.get("asked") and msgs(name, f"закрываю позицию по {seen['coin']}"):
            seen["asked"] = True
            user_says(CUR["bot"], "📊 Статус")
        if seen["coin"] and seen["after"] is None and msgs(name, f"позиция по {seen['coin']} закрыта"):
            seen["after"] = {"held": held, "time": time.time()}
        if hot and seen["after"] and not seen.get("hot"):
            seen["hot"] = next((x for x in held if x != seen["coin"]), None) or "SOL"
            seen["pid"] = (st.get("live") or {}).get("started")
            write_sim(app, "guard_data.json", {"expire": {seen["coin"]: seen["at"] + 3 * 86_400_000},
                                               "hot": {seen["hot"]: 4.0}})
        if seen.get("hot") and not seen.get("hot_t") and msgs(name, f"фильтр разогнанных монет: {seen['hot']}"):
            seen["hot_t"] = time.time()
            seen["hot_pid"] = (st.get("live") or {}).get("started")
            user_says(CUR["bot"], "📊 Статус")

    def done(st, t, end):
        if seen["after"] is None or time.time() - seen["after"]["time"] <= 4:
            return False
        return not hot or (seen.get("hot_t") and time.time() - seen["hot_t"] > 4)

    res = run(name, app, replay, speed=8640, max_real_s=300, on_tick=on_tick, start_offset_days=start_day, until=done)
    c = seen["coin"] or "?"
    L = res["final_state"].get("live") or {}
    held_after = ((L.get("snapshot") or {}).get("positions") or [])
    check(res, "в день 1 у бота были позиции — «KuCoin» назначил делистинг одной из них", seen["coin"],
          f"{c}, остальные: {', '.join(seen['others'])}")
    check(res, f"Telegram: «защита от делистинга: {c} — KuCoin закроет контракт …» со звуком",
          msgs(name, f"защита от делистинга: {c} — KuCoin закроет контракт", loud=True))
    check(res, f"Telegram: «закрываю позицию по {c} — перезапуск бота, остальные монеты торгуются» со звуком, один раз",
          len(msgs(name, f"закрываю позицию по {c}", loud=True)) == 1)
    check(res, f"Telegram: «позиция по {c} закрыта»", msgs(name, f"позиция по {c} закрыта"))
    check(res, f"после закрытия позиции {c} нет, бот работает",
          L.get("running") and not any(x["symbol"].startswith(c + "/") for x in held_after),
          ", ".join(x["symbol"].split("/")[0] for x in held_after))
    check(res, "Telegram /status: «🚫 Защита от делистинга: не торгуем …»", msgs(name, "Защита от делистинга", f"{c} — KuCoin закроет"))
    check(res, "нет тревог и ложного «процесса нет», ровно один процесс бота",
          not msgs(name, "Тревога") and max(x["procs"] for x in res["samples"]) == 1)
    if app == "turbo":
        b = res["final_state"].get("bet") or {}
        check(res, "ставка продолжается (не закрыта и не на паузе)", b.get("state") == "running", b.get("state"))
    if hot:
        h = seen.get("hot") or "?"
        g = ((res["final_state"].get("coin_guard") or {}).get("coins") or {}).get(h) or {}
        check(res, f"фильтр разогнанных: {h} (×4 за 30 дней) — «не входить», в итоге проверки с пометкой",
              g.get("level") == "ignore" and g.get("hot"), str(g))
        hm = msgs(name, f"фильтр разогнанных монет: {h}")
        quiet = CUR["bot"].quiet_now()      # обычные сообщения Telegram-бот глушит только ночью, тревоги — никогда
        check(res, f"Telegram: «фильтр разогнанных монет: {h} …» — обычное сообщение, не тревога (ночью без звука)",
              hm and not msgs(name, f"защита от делистинга: {h}") and (not quiet or not hm[0]["loud"]),
              "сейчас ночь — без звука" if quiet else "сейчас день — обычные сообщения со звуком")
        check(res, "разогнанная монета бота не перезапускает (то же время запуска бота)",
              seen.get("hot_pid") and seen.get("hot_pid") == seen.get("pid"), f"{seen.get('pid')} → {seen.get('hot_pid')}")
        check(res, "Telegram /status: «🔥 Разогнанные монеты: не покупаем …»", msgs(name, "Разогнанные монеты", h))
    api(app, "/api/live/stop", {})
    if app == "turbo":
        api(app, "/api/bet/reset", {})
    return res


def scenario_turbo5_delist():
    return delist_drill("8. Turbo 5.0: учебный делистинг монеты с открытой позицией — ставка продолжается; учебная тревога "
                        "фильтра разогнанных монет (ноябрь 2025)", "turbo", "turbo5_pause", hot=True)


# ------------------------------------------------------------------ отчёт
def transcript():
    L = ["# Репетиция реальной торговли — стенограмма", "",
         "Ход торговли — настоящее ядро passivbot на реальных ценах (Binance) и правилах KuCoin, те же пресеты и "
         "настройки, что у реальной торговли. Панели, автоперезапуск, наблюдатель ставки, отчёты и Telegram-бот — "
         "тот же код, что на сервере. Время в сообщениях — время сценария (исторические даты).", ""]
    for res in RESULTS:
        L += [f"## {res['name']}", ""]
        ok = sum(c["ok"] for c in res["checks"])
        L.append(f"Проверок пройдено: **{ok} из {len(res['checks'])}**")
        for c in res["checks"]:
            L.append(f"- {'✅' if c['ok'] else '❌'} {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""))
        for n in res["notes"]:
            L.append(f"- ℹ️ {n}")
        rs = res.get("replay_stats")
        if rs:
            L.append(f"- Расчёт passivbot: {rs['days']:.0f} дн., сделок {rs['fills']} ({rs['fills_per_day']}/день), "
                     f"позиций открыто/закрыто {rs['position_opens']}/{rs['position_closes']} → "
                     f"уведомлений о позициях было бы **{rs['position_msgs_per_day']} в день**, капитал "
                     f"{rs['start_equity']:.2f} → {rs['end_equity']:.2f}, худшая просадка {rs['max_dd_pct']}%")
        ms = [m for m in MSGS if m["scenario"] == res["name"]]
        cats = {}
        for m in ms:
            key = m["text"].split(" ")[0] if m["method"] == "sendMessage" else "правка сообщения"
            cats[key] = cats.get(key, 0) + 1
        L.append(f"- Сообщений в Telegram: {len(ms)} ({', '.join(f'{k} {v}' for k, v in sorted(cats.items(), key=lambda x: -x[1]))})")
        L += ["", "Сообщения (кроме позиций и утренних отчётов — их много, показаны первые):", "", "```"]
        shown_pos = shown_rep = 0
        for m in ms:
            t = m["text"].replace("<b>", "").replace("</b>", "").replace("<pre>", "").replace("</pre>", "") \
                .replace("<code>", "").replace("</code>", "")
            if t.startswith(("📥", "📤")):
                shown_pos += 1
                if shown_pos > 4:
                    continue
            if "Утренний отчёт" in t:
                shown_rep += 1
                if shown_rep > 1:
                    continue
            mark = "🔔" if m["loud"] else "🔕"
            L.append(f"[{m['sim']}] {mark} {t}" + (f"   [кнопки: {' | '.join(m['buttons'])}]" if m["buttons"] else ""))
            L.append("")
        L += ["```", ""]
    with open(f"{SIM}/transcript.md", "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(f"{SIM}/results.json", "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in r.items() if k not in ("bot", "samples")} for r in RESULTS], f, ensure_ascii=False,
                  indent=1, default=str)


def main():
    setup()
    telegram_setup()
    for app in APPS:
        start_panel(app)
    scenarios = (scenario_failures, scenario_turbo5_lock, scenario_turbo5_pause,
                 scenario_turbo5_delist)
    if "--only" in sys.argv:                      # например: --only 4  или  --only 1,3
        pick = {int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(",")}
        scenarios = [fn for i, fn in enumerate(scenarios, 1) if i in pick]
    try:
        for fn in scenarios:
            try:
                RESULTS.append(fn())
            except Exception as exc:
                traceback.print_exc()
                RESULTS.append({"name": fn.__name__, "checks": [{"name": "сценарий выполнился", "ok": False,
                                                                 "detail": repr(exc)}], "notes": []})
            transcript()
    finally:
        for app in APPS:
            api(app, "/api/live/stop", {}, timeout=30)
            kill_panel(app)
        for pid in sim_pids():
            os.kill(pid, signal.SIGKILL)
        transcript()
    total = sum(len(r["checks"]) for r in RESULTS)
    ok = sum(c["ok"] for r in RESULTS for c in r["checks"])
    print(f"\nИТОГ: {ok} из {total} проверок", flush=True)


if __name__ == "__main__":
    main()
