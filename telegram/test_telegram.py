"""Офлайн-проверка telegram_bot.py: Telegram API и панели подменены. Запуск: python deploy/test_telegram.py"""
import os, sys, tempfile, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R
import telegram_bot as T

tmp = tempfile.mkdtemp()
R.BASE = tmp; R.REP = os.path.join(tmp, "reports"); os.makedirs(R.REP)
R.EQ_CSV = os.path.join(R.REP, "equity.csv"); R.ALERTS = os.path.join(R.REP, "alerts.log"); R.STATE = os.path.join(R.REP, ".st.json")
T.DIR = os.path.join(tmp, "telegram"); T.CFG = os.path.join(T.DIR, "config.json"); T.STATE = os.path.join(T.DIR, "state.json")
R.mem_disk = lambda: (1500, 1967, 17.0, 25.0)
R.sh = lambda *a: "active"
for app in ("v4", "turbo"):
    os.makedirs(os.path.join(tmp, app, "tb4/panel/data"))
LOG = os.path.join(tmp, "v4", "tb4/panel/data/live_console.log")

calls = []
def fake_api(token, method, http_timeout=20, **p):
    calls.append((method, p))
    if method == "getMe":
        return {"username": "test_bot"}
    if method == "getUpdates":
        ups, UPD[:] = list(UPD), []
        return ups
    return {"message_id": 1}
T.api = fake_api
UPD = []
posts = []
T.panel_post = lambda app, path, body=None: posts.append((app, path, body)) or None

ROW = dict(app="v4", name="Основной", user="u", service="active", restarts="0", panel_ok=True, panel_error=None,
           preset="ema_wide", keys=True, running=True, want_running=True, started=R.now().strftime("%Y-%m-%d %H:%M"),
           equity=300.0, balance=300.0, positions=[], orders=2, snap_age_min=0.5, bet=None)
TURBO = dict(ROW, app="turbo", name="Турбо", preset="turbo_ema5", running=False, want_running=False, equity=None, bet=None)
rows = [dict(ROW), dict(TURBO)]
R.collect = lambda: [dict(r) for r in rows]

def sent():
    out = [p.get("text", "") for m, p in calls if m in ("sendMessage", "editMessageText")]
    calls.clear()
    return out

def msg(text, uid=42):
    UPD.append({"update_id": len(calls) + int(time.time() * 1000) % 10**6, "message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid, "first_name": "Хозяин"}, "text": text}})

def cb(data, uid=42):
    UPD.append({"update_id": int(time.time() * 1e6) % 10**9, "callback_query": {"id": "q", "from": {"id": uid}, "data": data, "message": {"message_id": 5}}})

ok = 0
def check(cond, label):
    global ok
    print(("OK   " if cond else "FAIL ") + label)
    ok += bool(cond)
    if not cond:
        raise SystemExit(1)

# 1. setup
import io
sys.stdin = io.StringIO("123456789:AAHabcdefghijklmnopqrstuvwxyz0123456\n")
T.setup()
cfg = json.load(open(T.CFG, encoding="utf-8")); code = cfg["pair_code"]
check(len(code) == 6 and cfg["owner"] is None, "setup: токен сохранён, код выдан")

bot = T.Bot()
msg("привет", uid=99); bot.poll(); check(sent() == ["Пришлите 6-значный код из окна настройки на компьютере."], "чужой без кода — просьба о коде")
msg("/start 000000" if code != "000000" else "/start 111111", uid=99); bot.poll(); check(sent() == ["Неверный код."], "неверный код")
msg(f"/start {code}"); bot.poll(); out = sent(); check(bot.owner == 42 and "привязан" in out[-1], "привязка по коду")
msg("/status", uid=99); bot.poll(); check(sent() == [], "после привязки чужим не отвечает")

# 2. команды
msg("📊 Статус"); bot.poll(); out = sent(); check("Основной" in out[0] and "300.00" in out[0] and "Турбо" in out[0], "статус")
print(out[0]); print("---")
msg("/positions"); bot.poll(); check("позиций нет" in sent()[0], "позиции")
msg("📋 Отчёт"); bot.poll(); check("Отчёта ещё нет" in sent()[0], "отчёт (ещё нет)")
msg("/server"); bot.poll(); check("Сервер" in sent()[0], "сервер")
msg("/alerts"); bot.poll(); check("тревог нет" in sent()[0], "тревоги")

# 3. настройки
msg("⚙️ Настройки"); bot.poll(); sent()
cb("set:equity_step"); bot.poll(); sent(); check(bot.s["equity_step"] == 20, "шаг капитала 10→20")
check(bot.s["positions"] is False, "каждая сделка по умолчанию выключена (их 20–40 в день)")
cb("set:positions"); bot.poll(); sent(); check(bot.s["positions"] is True, "уведомления о каждой сделке включаются")
check(json.load(open(T.CFG, encoding="utf-8"))["settings"]["equity_step"] == 20, "настройки сохранены в файл")

# 4. наблюдение: первая проверка молчит
open(LOG, "w", encoding="utf-8").write("старое\n")
bot.check(); check(sent() == [], "первая проверка без сообщений")
# позиция открыта, события журнала
rows[0]["positions"] = [{"symbol": "SOL/USDT:USDT", "side": "long", "size": 3, "price": 150.1, "upnl": 1.5, "we": 0.4}]
with open(LOG, "a", encoding="utf-8") as f:
    f.write("\n===== 2026-09-30 10:00:00 автовосстановление после перезапуска =====\n")
    f.write("2026-09-30 [risk] HSL[long] tier transition green -> yellow | drawdown_score=0.180000 red_threshold=0.350000 yellow=0.5\n")
    f.write("2026-09-30 ERROR something failed\n")
bot.check(); out = sent()
check(any("открыта позиция <b>SOL</b>" in t for t in out), "открытие позиции")
check(any("автовосстановление" in t for t in out), "событие панели из журнала")
check(any("жёлтый" in t and "18%" in t for t in out), "уровень защиты капитала")
check(any("ошибок в журнале — 1" in t for t in out), "сводка ошибок")
# стоп сработал, позиция закрыта, капитал -25% → шаг 20%
rows[0]["positions"] = []; rows[0]["equity"] = 225.0
with open(LOG, "a", encoding="utf-8") as f:
    f.write("x [risk] HSL[long] RED triggered | strategy_equity=225 drawdown_score=0.360000 red_threshold=0.350000\n")
bot.check(); out = sent()
check(any("сработал стоп" in t and "36%" in t for t in out), "стоп по просадке")
check(any("закрыта позиция" in t for t in out), "закрытие позиции")
check(any("капитал -25.0%" in t for t in out), "изменение капитала")
check(bot.trades_24h("v4") == (1, 1), "счётчик сделок за сутки")
cb("set:positions"); bot.poll(); sent()
rows[0]["positions"] = [{"symbol": "ETH/USDT:USDT", "side": "long", "size": 1, "price": 3000, "upnl": 0, "we": 0.2}]
bot.check(); out = sent()
check(not any("открыта позиция" in t for t in out) and bot.trades_24h("v4") == (2, 1),
      "сделки без уведомлений всё равно считаются")
msg("/status"); bot.poll(); check("позиций открыто 2, закрыто 1" in sent()[0], "сводка сделок в статусе")
rows[0]["positions"] = []; bot.check(); sent()
crit = [p for m, p in calls]
# тревога: бот должен работать, но упал — только со 2-й проверки
rows[0]["running"] = False; rows[0]["equity"] = None
bot.check(); check(not any("Тревога" in t for t in sent()), "тревога не с первой проверки")
bot.check(); out = sent(); check(any("Тревога" in t and "процесса нет" in t for t in out), "тревога со второй проверки")
bot.check(); check(not any("Тревога" in t for t in sent()), "тревога не повторяется")
rows[0]["running"] = True; rows[0]["equity"] = 230.0
bot.check(); sent(); bot.check(); check(any("Снято" in t for t in sent()), "тревога снята")
# ночной режим: не критичное без звука, тревоги со звуком
# 5. управление
rows[1].update(running=True, want_running=True, equity=210.0, bet={"state": "running", "start_equity": 200, "last_equity": 210})
msg("🛑 Управление"); bot.poll(); calls_ctrl = [p for m, p in calls if m == "sendMessage"][-1]; sent()
labels = [b[0]["callback_data"] for b in calls_ctrl["reply_markup"]["inline_keyboard"]]
check(labels == ["ask:stop:v4", "ask:lock:turbo", "ask:stop:turbo"], "кнопки управления")
cb("ask:lock:turbo"); bot.poll(); out = sent(); check("Точно" in out[-1], "запрос подтверждения")
nonce = next(iter(bot.pending))
cb(f"do:{nonce}", uid=99); bot.poll(); check(posts == [], "чужое подтверждение игнорируется")
cb(f"do:{nonce}"); bot.poll(); out = sent(); check(posts == [("turbo", "/api/bet/lock", {"source": "telegram"})] and "Фиксация началась" in out[-1], "фиксация турбо")
cb(f"do:{nonce}"); bot.poll(); check("вышло" in sent()[-1] and len(posts) == 1, "повторное подтверждение не срабатывает")
msg("/status"); bot.poll(); st = sent()[0]
check("старт 200" in st and "без цели (сейчас ×1.05)" in st and "стоп при" not in st and "Фильтр рынка" not in st,
      "ставка в статусе: без цели, рост от старта")
keep0 = dict(rows[0])
rows[0].update(running=False, want_running=False, btc_pause={"state": "waiting", "closed": "2025-10-10 21:02", "equity": 312.5,
                                                             "resume_after": "11.10 03:00"})   # основной бот: пауза после обвала BTC
msg("/status"); bot.poll(); st = sent()[0]
check("пауза после обвала BTC" in st and "11.10 03:00" in st and "должен работать" not in st, "статус: пауза после обвала BTC, без тревоги")
calls.clear(); msg("🛑 Управление"); bot.poll()
kb_msg = [p for m, p in calls if m == "sendMessage"][-1]; sent()
labels = [b[0]["callback_data"] for b in kb_msg["reply_markup"]["inline_keyboard"]]
check("ask:unpause:v4" in labels and "ask:stop:v4" not in labels, "управление на паузе: «отменить запуск после паузы»")
cb("ask:unpause:v4"); bot.poll(); out = sent()
check("отменить запуск после паузы" in out[-1], "подтверждение отмены паузы")
n_posts, nonce = len(posts), next(iter(bot.pending))
cb(f"do:{nonce}"); bot.poll(); out = sent()
check(posts[n_posts:] == [("v4", "/api/live/stop", None)] and "запуск после паузы отменён" in out[-1], "пауза отменена из Telegram")
rows[0].clear(); rows[0].update(keep0)
# 6. утренний отчёт
open(os.path.join(R.REP, "latest.txt"), "w", encoding="utf-8").write("Отчёт о ботах — тест\nТревог нет.\n")
os.utime(os.path.join(R.REP, "latest.txt"), (time.time() + 5, time.time() + 5))
bot.check(); check(any("Утренний отчёт" in t for t in sent()), "утренний отчёт отправлен")
bot.check(); check(not any("Утренний отчёт" in t for t in sent()), "отчёт не дублируется")
# 6б. стоп passivbot, ставка закрыта фиксацией, автоперезапуск остановлен
rows[0]["hsl"] = {"enabled": True, "tier": "red", "halted": True, "no_restart_latched": False,
                  "cooldown_until_ms": int(time.time() * 1000) + 3 * 86400_000}
msg("/status"); bot.poll(); check("пауза до" in sent()[0], "пауза после стопа в статусе")
with open(os.path.join(tmp, "turbo", "tb4/panel/data/live_console.log"), "a", encoding="utf-8") as f:
    f.write("старое\n")
bot.check(); sent()
with open(os.path.join(tmp, "turbo", "tb4/panel/data/live_console.log"), "a", encoding="utf-8") as f:
    f.write("===== 2026-10-05 12:00:00 ставка закрыта: 290.00 USDT, бот остановлен =====\n")
rows[1].update(running=False, want_running=False, bet={"state": "done", "final_equity": 290.0})
bot.check(); out = sent()
check(any("💰" in t and "ставка закрыта: 290.00 USDT" in t for t in out), "«ставка закрыта» после фиксации")
check(not any("⏹" in t for t in out), "лишнего «бот остановлен» нет")
rows[1].update(running=True, want_running=True, bet={"state": "running", "start_equity": 300, "last_equity": 290})
bot.check(); sent()
pz = {"state": "waiting", "closed": "2026-10-05 13:00", "equity": 290.0, "resume_after": "06.10 03:00"}
rows[1].update(running=False, want_running=False, btc_pause=pz,      # Turbo 5.0: пауза после обвала BTC
               bet={"state": "btc_pause", "start_equity": 300, "btc_pause": pz})
bot.check(); out = sent()
check(not any("⏹" in t for t in out), "Turbo 5.0: пауза после обвала BTC — без «бот остановлен»")
msg("/status"); bot.poll(); st = sent()[0]
check("пауза после обвала BTC" in st and "06.10 03:00" in st, "статус Turbo: пауза после обвала BTC")
pzc = {"state": "closing", "since": "05.10 12:00", "warned": True, "resume_after": "06.10 03:00"}
rows[1].update(running=True, want_running=True, btc_pause=pzc, bet={"state": "btc_pause", "start_equity": 300, "btc_pause": pzc})
bot.check(); bot.check(); out = sent()
check(any("Тревога" in t and "не закрылись после обвала BTC" in t for t in out), "тревога: позиции не закрылись после обвала BTC")
rows[1].update(running=False, want_running=False, btc_pause=None, bet={"state": "done", "final_equity": 288.0})
bot.check(); bot.check(); sent()
rows[0].update(running=False, want_running=True, gave_up="2026-10-05 12:00", equity=None)
msg("/status"); bot.poll(); check("автоперезапуск остановлен" in sent()[0], "статус: автоперезапуск остановлен")
rows[0].update(running=True, want_running=True, gave_up=None, equity=230.0, hsl=None)
bot.check(); bot.check(); sent()

# 6в. стоп по монете (основной бот с 02.10.2026): закрывается только эта монета, остальные торгуют
until = int(time.time() * 1000) + 3 * 86400_000
rows[0]["hsl"] = {"enabled": True, "mode": "coin", "tier": "red", "halted": True, "coins": [
    {"coin": "DASH", "tier": "orange", "halted": False, "loss": 16.5, "slot_pct": 55, "orange_pct": 50},
    {"coin": "ZEC", "tier": "red", "halted": True, "no_restart_latched": False, "cooldown_until_ms": until}]}
msg("/status"); bot.poll(); st = sent()[0]
check("стоп по монете: DASH без докупок (минус 16.50 $); ZEC закрыта, пауза до" in st and "остальные монеты торгуются" in st,
      "статус: стоп по монете — какие монеты без докупок и закрыты")
rows[0]["hsl"] = None
st_line = ("x [risk] HSL[long:DASH/USDT:USDT] status | tier={} dist_to_red=0.480000 drawdown_raw=0.550000 drawdown_ema=0.520000 "
           "drawdown_score=0.520000 red_threshold=1.000000 cooldown_remaining=none last_red_ts=none pending_red_since_ms=none "
           "slot_budget=30.000000 realized_pnl=2.000000 peak_realized_pnl=3.000000 upnl=-15.500000\n")
with open(LOG, "a", encoding="utf-8") as f:
    f.write(st_line.format("yellow") + st_line.format("orange") + st_line.format("orange"))
bot.check(); out = sent()
check(sum("DASH" in t and "докупки по монете остановлены" in t and "минус 16.50 $, 55% доли монеты" in t for t in out) == 1,
      "монета на оранжевом уровне — одно сообщение «без докупок»")
with open(LOG, "a", encoding="utf-8") as f:
    f.write("x [risk] HSL[long:DASH/USDT:USDT] reset current episode after ordinary flat fill | flat_ts=1\n" + st_line.format("orange"))
bot.check(); check(not any("DASH" in t for t in sent()), "снова «без докупок» по той же монете — не чаще раза в 6 ч")
with open(LOG, "a", encoding="utf-8") as f:
    f.write("x [risk] HSL[long:ZEC/USDT:USDT] RED triggered | drawdown_raw=1.000000 drawdown_ema=1.000000 drawdown_score=1.000000 "
            "red_threshold=1.000000 slot_budget=30.000000 realized_pnl=0.000000 peak_realized_pnl=1.000000 upnl=-40.000000\n")
    f.write(f"x [risk] HSL[long:ZEC/USDT:USDT] RED stop finalized | stop_ts=1 drawdown_raw=1.000000 cooldown_until_ms={until} "
            "no_restart_latched=False latch=/tmp/x\n")
    f.write("x [risk] HSL[long:ZEC/USDT:USDT] RED cooldown elapsed; trading resumed\n")
bot.check(); calls_c = [p for m, p in calls if m == "sendMessage"]; out = sent()
check(any("стоп по монете ZEC (минус 41.00 $, 137% доли монеты)" in p["text"] and "остальные торгуются" in p["text"]
          and p["disable_notification"] is False for p in calls_c), "стоп по монете: закрывается только ZEC, со звуком")
check(any("ZEC закрыта по стопу; пауза по монете до " + R.msk(until) in t for t in out), "монета закрыта, пауза до …")
check(any("пауза по монете ZEC закончилась" in t for t in out), "пауза по монете закончилась — монета снова в работе")
check(not any("стоп по просадке" in t or "все позиции" in t for t in out), "не путает со стопом всего счёта")

# 7. тихий режим
bot.s["quiet"] = True; T.R.now = lambda: __import__("datetime").datetime(2026, 9, 30, 2, 0, tzinfo=R.TZ)
bot.send("обычное"); bot.send("тревога", critical=True)
sm = [p for m, p in calls if m == "sendMessage"]
check(sm[0]["disable_notification"] is True and sm[1]["disable_notification"] is False, "ночью обычные без звука, тревоги со звуком")

# 8. защита от делистинга (панели, coin_guard.py)
calls.clear()
rows[0]["guard"] = {"ZEC": {"level": "close", "reason": "KuCoin закроет контракт 05.10 11:00 (делистинг)"},
                    "ARK": {"level": "ignore", "reason": "оборот за сутки упал до 0.8 млн $"}}
msg("/status"); bot.poll(); st = sent()[0]
check("🚫 Защита от делистинга" in st and "ZEC — KuCoin закроет контракт 05.10 11:00" in st and "ARK — оборот" in st
      and "позицию по ZEC бот закрывает" in st, "статус: какие монеты не торгуем и почему")
rows[0]["guard"] = {}
with open(LOG, "a", encoding="utf-8") as f:
    f.write("===== 2026-10-02 12:00:00 защита от делистинга: ZEC — KuCoin закроет контракт 05.10 11:00 (делистинг). "
            "Бот больше не входит в ZEC, открытую позицию по ней закроет =====\n")
    f.write("===== 2026-10-02 12:01:00 защита от делистинга: закрываю позицию по ZEC — перезапуск бота, "
            "остальные монеты торгуются как обычно =====\n")
    f.write("===== 2026-10-06 12:00:00 защита от делистинга: SOL снова в работе — причина снята =====\n")
calls.clear(); bot.check()
gm = [p for m, p in calls if m == "sendMessage" and "защита от делистинга" in p["text"]]
check(len(gm) == 3 and gm[0]["disable_notification"] is False and gm[1]["disable_notification"] is False,
      "ночью: делистинг и закрытие позиции — со звуком")
check(gm[2]["disable_notification"] is True, "«монета снова в работе» — без звука")

# 9. фильтр разогнанных монет (панель Turbo, coin_guard.hot): отдельная строка статуса, без тревоги
calls.clear()
rows[0]["guard"] = {"SIREN": {"level": "ignore", "hot": True,
                              "reason": "цена разогнана: за 30 дней менялась в 22.4 раза (фильтр ×3) — новых входов нет"},
                    "ARK": {"level": "ignore", "reason": "оборот за сутки упал до 0.8 млн $"}}
msg("/status"); bot.poll(); st = sent()[0]
check("🔥 Разогнанные монеты: не покупаем SIREN ×22.4" in st and "🚫 Защита от делистинга" in st and "ARK — оборот" in st
      and "SIREN — цена" not in st, "статус: разогнанные — отдельной строкой, делистинг — своей")
check(R.guard_text({"SIREN": rows[0]["guard"]["SIREN"]}) is None and R.hot_text({}) is None,
      "только разогнанные — строки «защита от делистинга» нет; пусто — нет и строки фильтра")
rows[0]["guard"] = {}
with open(LOG, "a", encoding="utf-8") as f:
    f.write("===== 2026-10-07 03:00:00 фильтр разогнанных монет: SIREN — цена разогнана: за 30 дней менялась в 22.4 раза "
            "(фильтр ×3) — новых входов нет (открытую позицию бот ведёт как обычно) =====\n")
calls.clear(); bot.check()
hm = [p for m, p in calls if m == "sendMessage" and "фильтр разогнанных монет" in p["text"]]
check(len(hm) == 1 and hm[0]["disable_notification"] is True, "ночью: «фильтр разогнанных монет» — без звука")
print(f"\nВсего проверок пройдено: {ok}")
