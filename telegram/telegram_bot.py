#!/usr/bin/env python3
"""Telegram-бот для ботов на сервере: уведомления, отчёты, настройки и аварийная остановка.

Работает службой tradebot-telegram (deploy/server_services.sh). Токен задаётся с компьютера: deploy/telegram_setup.bat.
  telegram_bot.py setup   — прочитать токен из stdin, проверить его и выдать код привязки
  telegram_bot.py pair    — новый код привязки (другой аккаунт Telegram), токен прежний
  telegram_bot.py test    — пробное сообщение владельцу
  telegram_bot.py         — работа (служба)
Бот отвечает только владельцу — тому, кто прислал код привязки. Запустить торговлю из Telegram нельзя, только остановить.
Только стандартная библиотека Python.
"""
import html
import json
import os
import re
import secrets
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402

DIR = os.path.join(R.BASE, "telegram")
CFG = os.path.join(DIR, "config.json")
STATE = os.path.join(DIR, "state.json")
PAIR_MINUTES = 30
CHECK_EVERY_S = 60
ERR_EVERY_S = 3600          # ошибки из журналов — одной сводкой не чаще раза в час
TIER_EVERY_S = 1800         # смена уровня защиты (кроме красного) — не чаще раза в 30 минут
COIN_ORANGE_EVERY_S = 6 * 3600   # «без докупок» по одной монете (стоп по монете) — не чаще раза в 6 часов
CONFIRM_S = 60
QUIET = (23, 8)             # «ночью без звука», МСК
# каждая сделка — выкл: по расчётам TB Core это 20–40 сообщений в день на бота; вместо них сводка за сутки
DEFAULTS = {"positions": False, "equity_step": 10, "dd_warn": 25, "daily": True, "errors": True,
            "quiet": False, "control": True}
EQ_STEPS = (5, 10, 20, 0)
DD_STEPS = (15, 20, 25, 30)
NAMES = {app: name for app, name, *_ in R.APPS}
PORTS = {app: port for app, _, port, _ in R.APPS}
TIERS = {"green": "🟢 зелёный", "yellow": "🟡 жёлтый", "orange": "🟠 оранжевый", "red": "🔴 красный"}
BET_STATES = R.BET_STATES
# при фиксации и конце ставки панель сама останавливает бота и пишет об этом — отдельное «⏹ бот остановлен» лишнее
BET_FINAL = ("locking", "done")

BUTTONS = {"📊 Статус": "status", "📈 Позиции": "positions", "📋 Отчёт": "report", "🔔 Тревоги": "alerts",
           "🖥 Сервер": "server", "⚙️ Настройки": "settings", "🛑 Управление": "control", "❓ Помощь": "help"}
KEYBOARD = {"keyboard": [["📊 Статус", "📈 Позиции"], ["📋 Отчёт", "🔔 Тревоги"], ["🖥 Сервер", "⚙️ Настройки"],
                         ["🛑 Управление", "❓ Помощь"]], "resize_keyboard": True, "is_persistent": True}
COMMANDS = [("status", "состояние ботов"), ("positions", "открытые позиции"), ("report", "утренний отчёт"),
            ("alerts", "тревоги"), ("server", "состояние сервера"), ("settings", "настройки уведомлений"),
            ("control", "аварийная остановка"), ("help", "что умеет бот")]

HELP = """<b>Что умеет бот</b>

<b>Пишет сам</b> (тревоги — всегда со звуком):
🚨 тревоги: служба или бот упали, бот завис, просадка выше порога, мало памяти или диска
🚨 стоп по просадке сработал / завершён / пауза закончилась
🤖 бот запущен, восстановлен после сбоя, остановлен
💰 турбо: старт ставки, цель ×2 достигнута, ставка закрыта
📈📉 капитал изменился на заданный шаг
☀️ утренний отчёт в 07:05 МСК и 📊 сводка сделок за сутки
📥📤 каждая сделка — по желанию (⚙️ Настройки): бот открывает и закрывает 20–40 позиций в день
⚠️ ошибки из журналов (сводкой, не чаще раза в час)

<b>Кнопки</b>: статус, позиции, отчёт, тревоги, сервер, настройки, управление.
В «Управлении» можно остановить бота или зафиксировать ставку турбо (с подтверждением). Запуск — только из панели.

Если утренний отчёт не пришёл — сервер или этот бот недоступны: проверьте через deploy\\report.bat."""


def now_ts():
    """Текущее время для счётчиков сделок (репетиция подменяет его своим ускоренным временем)."""
    return time.time()


def esc(text):
    return html.escape(str(text), quote=False)


def money(v):
    return "—" if v is None else f"{v:,.2f}".replace(",", " ")


def pct(x):
    return f"{x:+.1%}"


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def log(msg):
    print(f"{R.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def save_private(path, data):
    os.makedirs(DIR, exist_ok=True)
    os.chmod(DIR, 0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_cfg():
    c = R.load_json(CFG, {})
    c["settings"] = {**DEFAULTS, **(c.get("settings") or {})}
    return c


def sparkline(values):
    bars = "▁▂▃▄▅▆▇█"
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return bars[3] * len(values)
    return "".join(bars[int((v - lo) / (hi - lo) * (len(bars) - 1))] for v in values)


def short(symbol):
    return str(symbol).split("/")[0].split(":")[0]


class TgError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"{code}: {desc}")
        self.code = code


def api(token, method, http_timeout=20, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except ValueError:
            res = {"error_code": e.code, "description": e.reason}
    if not res.get("ok"):
        raise TgError(res.get("error_code"), res.get("description"))
    return res.get("result")


def panel_post(app, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORTS[app]}{path}", data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90):
            return None
    except urllib.error.HTTPError as e:
        try:
            return json.load(e).get("error") or str(e)
        except ValueError:
            return str(e)
    except Exception as e:
        return f"панель не отвечает ({e})"


class Bot:
    def __init__(self):
        self.cfg, self.cfg_mtime = load_cfg(), mtime(CFG)
        self.st = R.load_json(STATE, {})
        self.outbox = []
        self.pending = {}
        self.last_check = 0.0
        self.first_check = True
        self.started = time.time()

    # ---------------------------------------------------------------- основа
    @property
    def token(self):
        return self.cfg.get("token")

    @property
    def owner(self):
        return self.cfg.get("owner")

    @property
    def s(self):
        return self.cfg["settings"]

    def call(self, method, **params):
        return api(self.token, method, **params)

    def reload_cfg(self):
        m = mtime(CFG)
        if m != self.cfg_mtime:
            self.cfg, self.cfg_mtime = load_cfg(), m
            log("настройки перечитаны")

    def save_cfg(self):
        save_private(CFG, self.cfg)
        self.cfg_mtime = mtime(CFG)

    def save_state(self):
        save_private(STATE, self.st)

    def quiet_now(self):
        h = R.now().hour
        return self.s["quiet"] and (h >= QUIET[0] or h < QUIET[1])

    def send(self, text, critical=False, markup=None, chat=None):
        chat = chat or self.owner
        if not chat or not self.token:
            return None
        msg = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True,
               "disable_notification": (not critical) and self.quiet_now()}
        if markup:
            msg["reply_markup"] = markup
        try:
            return self.call("sendMessage", **msg)
        except TgError as e:
            log(f"не отправлено: {e}")
        except Exception as e:  # нет связи — повторим позже
            log(f"нет связи с Telegram: {e}")
            if chat == self.owner and len(self.outbox) < 100:
                self.outbox.append(msg)
        return None

    def send_pre(self, title, text, critical=False):
        """Длинный текст моноширинным шрифтом, кусками по ~3500 символов."""
        chunk, parts = [], []
        for line in text.splitlines():
            if sum(len(x) + 1 for x in chunk) + len(line) > 3500:
                parts.append(chunk)
                chunk = []
            chunk.append(line)
        parts.append(chunk)
        for i, p in enumerate(parts):
            self.send((f"{title}\n" if i == 0 and title else "") + "<pre>" + esc("\n".join(p)) + "</pre>", critical)

    def flush_outbox(self):
        while self.outbox:
            self.call("sendMessage", **self.outbox[0])
            self.outbox.pop(0)

    # ---------------------------------------------------------------- приём сообщений
    def poll(self):
        ups = self.call("getUpdates", http_timeout=40, timeout=25, offset=self.st.get("update_id", 0) + 1,
                        allowed_updates=["message", "callback_query"])
        for u in ups or []:
            self.st["update_id"] = u["update_id"]
            try:
                if "message" in u:
                    self.on_message(u["message"])
                elif "callback_query" in u:
                    self.on_callback(u["callback_query"])
            except Exception:
                traceback.print_exc()
        if ups:
            self.save_state()

    def on_message(self, m):
        chat = m.get("chat") or {}
        if chat.get("type") != "private":
            return
        text = (m.get("text") or "").strip()
        if chat.get("id") != self.owner:
            return self.try_pair(chat["id"], m.get("from") or {}, text)
        cmd = BUTTONS.get(text)
        if not cmd and text.startswith("/"):
            cmd = text.split()[0][1:].split("@")[0].lower()
        handler = {"start": self.cmd_help, "help": self.cmd_help, "status": self.cmd_status,
                   "positions": self.cmd_positions, "report": self.cmd_report, "alerts": self.cmd_alerts,
                   "server": self.cmd_server, "settings": self.cmd_settings, "control": self.cmd_control}.get(cmd)
        if handler:
            handler()
        else:
            self.send("Не понял. Нажмите кнопку внизу или /help.", markup=KEYBOARD)

    def try_pair(self, chat_id, frm, text):
        code, exp = self.cfg.get("pair_code"), self.cfg.get("pair_expires") or 0
        if not code or time.time() > exp:
            return                                   # чужим не отвечаем
        m = re.search(r"\b(\d{6})\b", text)
        if not m:
            return self.send("Пришлите 6-значный код из окна настройки на компьютере.", chat=chat_id)
        if not secrets.compare_digest(m.group(1), code):
            self.cfg["pair_fails"] = self.cfg.get("pair_fails", 0) + 1
            if self.cfg["pair_fails"] >= 5:
                self.cfg.update(pair_code=None, pair_expires=0)
            self.save_cfg()
            return self.send("Неверный код.", chat=chat_id)
        self.cfg.update(owner=chat_id, owner_name=frm.get("username") or frm.get("first_name"),
                        pair_code=None, pair_expires=0, pair_fails=0)
        self.save_cfg()
        log(f"привязан владелец {self.cfg['owner_name']}")
        try:
            self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except Exception:
            pass
        self.send("✅ Бот привязан к вашему аккаунту. Теперь он отвечает только вам.\n\n" + HELP, markup=KEYBOARD)

    def on_callback(self, q):
        if (q.get("from") or {}).get("id") != self.owner:
            return
        data, msg = q.get("data") or "", q.get("message") or {}
        mid = msg.get("message_id")
        note = ""
        if data.startswith("set:"):
            note = self.toggle(data[4:])
            self.edit(mid, self.settings_text(), self.settings_markup())
        elif data.startswith("ask:"):
            _, action, app = data.split(":")
            nonce = secrets.token_hex(4)
            self.pending = {k: v for k, v in self.pending.items() if v[2] > time.time()}
            self.pending[nonce] = (action, app, time.time() + CONFIRM_S)
            verb = {"stop": f"остановить {NAMES[app].lower()} бот",
                    "lock": "зафиксировать ставку турбо (закрыть все позиции)",
                    "unpause": f"отменить запуск после паузы — {NAMES[app].lower()} бот сам не запустится"}[action]
            self.edit(mid, f"❓ Точно {verb}?\nПодтверждение действует {CONFIRM_S} секунд.",
                      {"inline_keyboard": [[{"text": "Да", "callback_data": f"do:{nonce}"},
                                            {"text": "Отмена", "callback_data": "cancel"}]]})
        elif data.startswith("do:"):
            item = self.pending.pop(data[3:], None)
            if not item or item[2] < time.time():
                self.edit(mid, "Время подтверждения вышло. Откройте «Управление» снова.")
            elif not self.s["control"]:
                self.edit(mid, "Аварийная остановка выключена в настройках.")
            else:
                self.edit(mid, "⏳ Выполняю…")
                self.edit(mid, self.do_action(*item[:2]))
        elif data == "cancel":
            self.pending.clear()
            self.edit(mid, "Отменено.")
        try:
            self.call("answerCallbackQuery", callback_query_id=q["id"], text=note)
        except Exception:
            pass

    def edit(self, mid, text, markup=None):
        p = {"chat_id": self.owner, "message_id": mid, "text": text, "parse_mode": "HTML"}
        if markup:
            p["reply_markup"] = markup
        try:
            self.call("editMessageText", **p)
        except TgError as e:
            if "not modified" not in str(e):
                log(f"editMessageText: {e}")

    # ---------------------------------------------------------------- команды
    def cmd_help(self):
        self.send(HELP, markup=KEYBOARD)

    def cmd_status(self):
        rows = self.collect()
        text = "\n\n".join(self.fmt_bot(r) for r in rows)
        alerts = R.alerts_for(rows)
        text += "\n\n" + ("🚨 Тревоги:\n" + "\n".join(f"• {esc(a)}" for a in alerts) if alerts else "✅ Тревог нет")
        self.send(text)

    def cmd_positions(self):
        out = []
        for r in self.collect():
            if not r["running"]:
                out.append(f"⚪ <b>{r['name']}</b>: бот не запущен")
                continue
            if not r["positions"]:
                out.append(f"🟢 <b>{r['name']}</b>: позиций нет, ордеров {r['orders'] or 0}")
                continue
            lines = [f"🟢 <b>{r['name']}</b> — позиций {len(r['positions'])}, ордеров {r['orders'] or 0}"]
            for p in r["positions"]:
                upnl = R.num(p.get("upnl"))
                we = R.num(p.get("we"))
                lines.append(f"• <b>{esc(short(p['symbol']))}</b> {p['side']}: объём {p['size']:g}, цена {R.price(p['price'])}, "
                             f"результат {'—' if upnl is None else f'{upnl:+.2f}'} USDT"
                             + (f", загрузка {we:.2f}" if we is not None else ""))
            out.append("\n".join(lines))
        self.send("\n\n".join(out))

    def cmd_report(self):
        path = os.path.join(R.REP, "latest.txt")
        try:
            with open(path, encoding="utf-8") as f:
                self.send_pre("📋 <b>Последний утренний отчёт</b>", f.read())
        except OSError:
            self.send("Отчёта ещё нет — первый будет в 07:05 МСК. Сейчас — кнопка «📊 Статус».")

    def cmd_alerts(self):
        cur = R.alerts_for(self.collect())
        text = ("🚨 <b>Сейчас:</b>\n" + "\n".join(f"• {esc(a)}" for a in cur)) if cur else "✅ Сейчас тревог нет."
        try:
            with open(R.ALERTS, encoding="utf-8") as f:
                last = f.read().splitlines()[-10:]
        except OSError:
            last = []
        if last:
            text += "\n\n<b>Журнал тревог</b> (последние):\n<pre>" + esc("\n".join(last)) + "</pre>"
        self.send(text)

    def cmd_server(self):
        avail, total, free_gb, total_gb = R.mem_disk()
        lines = [f"🖥 <b>Сервер</b>: работает {esc(R.sh('uptime', '-p').replace('up ', ''))}",
                 f"Память: свободно {avail} из {total} МБ", f"Диск: свободно {free_gb:.1f} из {total_gb:.0f} ГБ"]
        lines += [f"  • {esc(n)}: {m:.0f} МБ" for n, m in R.process_memory()]
        for app, name, *_ in R.APPS:
            lines.append(f"Служба {name.lower()}: {R.sh('systemctl', 'is-active', f'tradebot-{app}') or '?'}, "
                         f"перезапусков {R.sh('systemctl', 'show', '-p', 'NRestarts', '--value', f'tradebot-{app}') or '?'}")
        up = timedelta(seconds=int(time.time() - self.started))
        lines.append(f"Telegram-бот работает {up}")
        self.send("\n".join(lines))

    def settings_text(self):
        return ("⚙️ <b>Настройки уведомлений</b>\nНажмите пункт, чтобы переключить.\n"
                "Тревоги и стоп по просадке приходят всегда и со звуком.")

    def settings_markup(self):
        s = self.s
        on = lambda k: "✅" if s[k] else "▫️"  # noqa: E731
        step = f"каждые {s['equity_step']}%" if s["equity_step"] else "выкл"
        rows = [(f"{on('positions')} Каждая сделка (20–40 сообщений в день)", "positions"),
                (f"📈 Изменение капитала: {step}", "equity_step"),
                (f"📉 Тревога просадки: от {s['dd_warn']}% от пика", "dd_warn"),
                (f"{on('daily')} Утренний отчёт в 07:05", "daily"),
                (f"{on('errors')} Ошибки из журналов (раз в час)", "errors"),
                (f"{on('quiet')} Ночью без звука (23:00–08:00)", "quiet"),
                (f"{on('control')} Аварийная остановка из Telegram", "control")]
        return {"inline_keyboard": [[{"text": t, "callback_data": f"set:{k}"}] for t, k in rows]}

    def toggle(self, key):
        s = self.s
        if key == "equity_step":
            s[key] = EQ_STEPS[(EQ_STEPS.index(s[key]) + 1) % len(EQ_STEPS)] if s[key] in EQ_STEPS else 10
        elif key == "dd_warn":
            s[key] = DD_STEPS[(DD_STEPS.index(s[key]) + 1) % len(DD_STEPS)] if s[key] in DD_STEPS else 25
        elif key in DEFAULTS:
            s[key] = not s[key]
        else:
            return ""
        self.save_cfg()
        return "Сохранено"

    def cmd_settings(self):
        self.send(self.settings_text(), markup=self.settings_markup())

    def cmd_control(self):
        if not self.s["control"]:
            return self.send("Аварийная остановка выключена в «⚙️ Настройках».")
        rows = {r["app"]: r for r in self.collect()}
        kb = []
        if rows.get("v4", {}).get("running"):
            kb.append([{"text": "⏹ Остановить основной бот", "callback_data": "ask:stop:v4"}])
        if rows.get("turbo", {}).get("running"):
            bet = rows["turbo"].get("bet") or {}
            if bet.get("state") in ("waiting_start", "running"):
                kb.append([{"text": "💰 Турбо: зафиксировать ставку", "callback_data": "ask:lock:turbo"}])
            kb.append([{"text": "⏹ Остановить турбо-бот", "callback_data": "ask:stop:turbo"}])
        for app, r in rows.items():       # пауза после обвала BTC: в 00:00 UTC бот запустится сам — дать это отменить
            if not r.get("running") and (r.get("btc_pause") or (r.get("bet") or {}).get("state") == "btc_pause"):
                kb.append([{"text": f"⏸ {NAMES[app]}: отменить запуск после паузы", "callback_data": f"ask:unpause:{app}"}])
        if not kb:
            return self.send("Сейчас ни один бот не торгует. Запуск — только из панели (deploy\\tunnel.bat).")
        self.send("🛑 <b>Управление</b>\n"
                  "• «Остановить» выключает бота, но <b>не закрывает позиции</b> — они остаются на бирже.\n"
                  "• «Зафиксировать» закрывает все позиции турбо и затем останавливает его.\n"
                  "Запустить бота снова можно только из панели.", markup={"inline_keyboard": kb})

    def do_action(self, action, app):
        if action == "lock":
            err = panel_post(app, "/api/bet/lock", {"source": "telegram"})
            ok = "✅ Фиксация началась: бот закрывает позиции. Сообщу, когда ставка будет закрыта."
        elif action == "unpause":
            err = panel_post(app, "/api/live/stop")
            ok = (f"✅ {NAMES[app]}: запуск после паузы отменён — сам бот не запустится"
                  + (", ставка завершена" if app == "turbo" else "") + ". Запуск — только из панели.")
        else:
            err = panel_post(app, "/api/live/stop")
            ok = f"✅ {NAMES[app]} бот остановлен. Открытые позиции остались на бирже."
        log(f"из Telegram: {action} {app} → {err or 'ok'}")
        return f"❌ Не получилось: {esc(err)}" if err else ok

    # ---------------------------------------------------------------- наблюдение
    def collect(self):
        R.DD_WARN = self.s["dd_warn"] / 100
        return R.collect()

    def fmt_bot(self, r):
        name = r["name"]
        if not r["panel_ok"]:
            return f"❌ <b>{name}</b>: панель не отвечает"
        if r["running"]:
            L = [f"🟢 <b>{name}</b> — работает с {esc(r['started'])}", f"Пресет: {esc(r.get('preset_name') or r['preset'])}"]
            eq = r["equity"]
            if eq is not None:
                parts = []
                first = R.history(r["app"], r["started"])
                day = R.history(r["app"], (R.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M"))
                te = R.trading_value(eq, R.transfers_cum())     # без переводов владельца; доля — от реального капитала
                if first and (first[0][2] or 0) > 0:
                    parts.append(f"с запуска {pct((te - first[0][1]) / first[0][2])}")
                if day and (day[0][2] or 0) > 0:
                    parts.append(f"за сутки {pct((te - day[0][1]) / day[0][2])}")
                L.append(f"Капитал: <b>{money(eq)}</b> USDT" + (f" ({', '.join(parts)})" if parts else ""))
                peak, cur, mx = R.drawdown(r)
                if peak:
                    L.append(f"Просадка от пика: {cur:.1%} (наибольшая {mx:.1%})")
                sp = sparkline([e for _, _, e in first[-48:]] + [eq])
                if len(sp) > 2:
                    L.append(f"{sp}  (по часам)")
            else:
                L.append("Данных о капитале пока нет.")
            L.append(f"Позиции: {len(r['positions'])} · ордеров: {r['orders'] or 0}")
            o, c = self.trades_24h(r["app"])
            if o or c:
                L.append(f"За сутки: позиций открыто {o}, закрыто {c}")
            if R.hsl_text(r.get("hsl")):
                L.append(f"🛡 {esc(R.hsl_text(r['hsl']))}")
            if R.guard_text(r.get("guard")):
                L.append(f"🚫 Защита от делистинга: {esc(R.guard_text(r['guard']))}")
            if R.hot_text(r.get("guard")):
                L.append(f"🔥 Разогнанные монеты: {esc(R.hot_text(r['guard']))}")
        elif r.get("btc_pause") and r["btc_pause"].get("state") == "waiting":
            L = [f"⏸ <b>{name}</b> — {esc(R.btc_pause_text(r['btc_pause']))}",
                 f"Пресет: {esc(r.get('preset_name') or r['preset'])}"]
        else:
            L = [f"⚪ <b>{name}</b> — не запущен" + (" ⚠️ но должен работать" if r["want_running"] else ""),
                 f"Пресет: {esc(r.get('preset_name') or r['preset'])} · ключи: {'заданы' if r['keys'] else 'не заданы'}"]
            if r.get("gave_up"):
                L.append("Бот падал сразу после запуска — автоперезапуск остановлен. Проверьте ключи и журнал в панели.")
        bet = r.get("bet")
        if bet:
            line = f"Ставка: {BET_STATES.get(bet.get('state'), bet.get('state'))}"
            start = R.num(bet.get("start_equity"))
            cur = r["equity"] if r["equity"] is not None else R.num(bet.get("last_equity"))
            if start and bet.get("state") in ("running", "locking"):
                line += f", старт {money(start)}, без цели"
                if cur:
                    line += f" (сейчас ×{cur / start:.2f})"
            if bet.get("final_equity"):
                line += f", итог {money(R.num(bet['final_equity']))} USDT"
            L.append(line)
        return "\n".join(L)

    def check(self):
        rows = self.collect()
        self.check_alerts(rows)
        for r in rows:
            self.check_log(r)
            self.check_running(r)
            self.check_positions(r)
            self.check_equity(r)
        self.flush_errors()
        self.check_daily()
        self.first_check = False
        self.save_state()

    def check_alerts(self, rows):
        """Тревога уходит, если держится 2 проверки подряд (~2 минуты); «снято» — после 2 проверок без неё."""
        cur = {R.alert_key(a): a for a in R.alerts_for(rows)}
        seen = self.st.setdefault("alert_seen", {})
        sent = self.st.setdefault("alert_sent", {})
        gone = self.st.setdefault("alert_gone", {})
        for k, a in cur.items():
            seen[k] = seen.get(k, 0) + 1
            gone.pop(k, None)
            if k not in sent and seen[k] >= 2:
                self.send(f"🚨 <b>Тревога</b>\n{esc(a)}", critical=True)
                sent[k] = a
        for k in list(seen):
            if k not in cur:
                seen.pop(k)
        for k in list(sent):
            if k not in cur:
                gone[k] = gone.get(k, 0) + 1
                if gone[k] >= 2:
                    self.send(f"✅ Снято: {esc(sent.pop(k))}")
                    gone.pop(k)

    def check_running(self, r):
        prev = self.st.setdefault("running", {})
        was = prev.get(r["app"])
        prev[r["app"]] = r["running"]
        if was and not r["running"] and not r["want_running"] and r["panel_ok"]:
            if (r.get("bet") or {}).get("state") not in BET_FINAL and not r.get("btc_pause"):   # пауза по плану
                self.send(f"⏹ <b>{r['name']}</b>: бот остановлен")

    def check_positions(self, r):
        store = self.st.setdefault("pos", {})
        app = r["app"]
        if not r["running"]:
            store.pop(app, None)
            return
        if r["snap_age_min"] is None or r["snap_age_min"] > 5:
            return
        cur = {f"{p['symbol']}|{p['side']}": p for p in r["positions"]}
        prev = store.get(app)
        store[app] = {k: {"price": p.get("price"), "size": p.get("size")} for k, p in cur.items()}
        if prev is None:
            return
        log = self.st.setdefault("trades", {}).setdefault(app, [])
        now = now_ts()
        log += [[now, "open"]] * len(cur.keys() - prev.keys()) + [[now, "close"]] * len(prev.keys() - cur.keys())
        log[:] = [x for x in log if now - x[0] < 48 * 3600]
        if not self.s["positions"]:
            return
        for k in sorted(cur.keys() - prev.keys()):
            p = cur[k]
            self.send(f"📥 <b>{r['name']}</b>: открыта позиция <b>{esc(short(p['symbol']))}</b> {p['side']}, "
                      f"объём {p['size']:g}, цена {R.price(p['price'])}")
        for k in sorted(prev.keys() - cur.keys()):
            sym, side = k.split("|")
            self.send(f"📤 <b>{r['name']}</b>: закрыта позиция <b>{esc(short(sym))}</b> {side}. "
                      f"Капитал {money(r['equity'])} USDT")

    def trades_24h(self, app):
        """Сколько позиций открыто и закрыто за последние сутки (по наблюдениям бота)."""
        log = (self.st.get("trades") or {}).get(app) or []
        recent = [k for t, k in log if now_ts() - t < 24 * 3600]
        return recent.count("open"), recent.count("close")

    def trades_line(self):
        parts = []
        for app, name, *_ in R.APPS:
            o, c = self.trades_24h(app)
            if o or c:
                parts.append(f"{name.lower()} — открыто {o}, закрыто {c}")
        return ("📊 Сделки за сутки: " + "; ".join(parts)) if parts else ""

    def check_equity(self, r):
        marks = self.st.setdefault("eq_mark", {})
        app, eq = r["app"], r["equity"]
        if not r["running"] or eq is None or eq <= 0:
            return
        m = marks.get(app)
        if not m or m.get("started") != r["started"]:
            marks[app] = {"started": r["started"], "eq": eq, "start_eq": eq}
            return
        step = self.s["equity_step"]
        if step and m["eq"] > 0 and abs(eq / m["eq"] - 1) >= step / 100:
            ch = eq / m["eq"] - 1
            self.send(f"{'📈' if ch > 0 else '📉'} <b>{r['name']}</b>: капитал {pct(ch)} — "
                      f"{money(m['eq'])} → <b>{money(eq)}</b> USDT (с запуска {pct(eq / m['start_eq'] - 1)})")
            m["eq"] = eq

    def check_log(self, r):
        """Новые строки консольного журнала панели: события панели, стоп по просадке, ошибки."""
        app = r["app"]
        path = os.path.join(R.BASE, app, "tb4/panel/data/live_console.log")
        offs = self.st.setdefault("log_off", {})
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        off = offs.get(app)
        if off is None or size < off or (self.first_check and size - off > 2 * 1024 ** 2):
            offs[app] = size                      # первый запуск, обрезка журнала или слишком старое — с конца
            return
        if size == off:
            return
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read(min(size - off, 5 * 1024 ** 2))
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        offs[app] = off + end + 1
        for line in chunk[:end].decode("utf-8", "replace").splitlines():
            try:
                self.log_event(app, line)
            except Exception:
                traceback.print_exc()

    def log_event(self, app, line):
        name = NAMES[app]
        m = re.match(r"^=====\s+\S+\s+\S+\s+(.*?)\s*=====$", line.strip())
        if m:
            text = m.group(1)
            if text in ("live", "live_panic"):
                return
            crit = any(w in text for w in ("фиксация", "не закрылись", "автовосстановление", "автоперезапуск",
                                           "закрываю все позиции")) \
                or ("защита от делистинга" in text and "снова в работе" not in text)
            icon = "💰" if ("ставк" in text or "фиксация" in text or "капитал на старте" in text) else "🤖"
            return self.send(f"{icon} <b>{name}</b>: {esc(text)}", critical=crit)
        if "[risk] HSL[" in line:
            return self.hsl_event(name, app, line)
        if not self.s["errors"]:
            return
        e = self.st.setdefault("errs", {}).setdefault(app, {"n": 0, "last": "", "sent": 0})
        if e.get("in_tb") and re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Interrupt|Exit)\b", line.strip()):
            e["last"], e["in_tb"] = line.strip()[:300], False      # суть ошибки — последняя строка Traceback
        elif " ERROR " in line or "Traceback" in line or "CRITICAL" in line:
            e["n"] += 1
            e["last"] = line.strip()[:300]
            e["in_tb"] = "Traceback" in line

    def hsl_event(self, name, app, line):
        mc = re.search(r"HSL\[\w+:([^\]]+)\]", line)
        if mc:                                  # стоп по монете: HSL[long:ZEC/USDT:USDT]
            return self.hsl_coin_event(name, app, mc.group(1).split("/")[0], line)
        score = re.search(r"drawdown_score=([\d.]+)", line)
        red = re.search(r"red_threshold=([\d.]+)", line)
        dd = f" (просадка {float(score.group(1)):.0%}, стоп при {float(red.group(1)):.0%})" if score and red else ""
        if "RED triggered" in line:
            return self.send(f"🚨 <b>{name}</b>: сработал стоп по просадке{dd} — бот закрывает все позиции", critical=True)
        if "RED stop finalized" in line:
            after = ("бот сам продолжит после паузы" if "auto-restart" in line
                     else "стоп без перезапуска: новых сделок на этом счёте не будет около 30 дней")
            return self.send(f"🛑 <b>{name}</b>: стоп завершён, позиции закрыты; {after}", critical=True)
        if "RED cooldown elapsed" in line:
            return self.send(f"▶️ <b>{name}</b>: пауза после стопа закончилась, торговля возобновлена")
        m = re.search(r"tier transition (\w+) -> (\w+)", line)
        if m:
            a, b = m.group(1).lower(), m.group(2).lower()
            last = self.st.setdefault("tier_sent", {})
            if b != "red" and time.time() - last.get(app, 0) < TIER_EVERY_S:
                return
            last[app] = time.time()
            self.send(f"🛡 <b>{name}</b>: уровень защиты капитала {TIERS.get(a, a)} → {TIERS.get(b, b)}{dd}",
                      critical=b in ("orange", "red"))

    def hsl_coin_event(self, name, app, coin, line):
        """Стоп по монете (live.hsl_signal_mode coin): закрывается только эта монета, остальные торгуют.
        Оранжевый уровень (без докупок) виден только в строках status (раз в 15 мин по монете с позицией)."""
        def num(k):
            m = re.search(rf"\b{k}=(-?[\d.]+)", line)
            return float(m.group(1)) if m else None
        peak, real, upnl, slot = num("peak_realized_pnl"), num("realized_pnl"), num("upnl"), num("slot_budget")
        loss = max(0.0, peak - (real + upnl)) if None not in (peak, real, upnl) else None
        lt = (f" (минус {loss:.2f} $" + (f", {loss / slot:.0%} доли монеты" if slot else "") + ")") if loss else ""
        tiers = self.st.setdefault("coin_tier", {}).setdefault(app, {})
        prev = tiers.get(coin) or {}
        if "RED triggered" in line:
            tiers[coin] = {"tier": "red", "sent": prev.get("sent", 0)}
            return self.send(f"🚨 <b>{name}</b>: стоп по монете {esc(coin)}{lt} — бот закрывает только эту монету, "
                             f"остальные торгуются", critical=True)
        if "RED stop finalized" in line:
            until = re.search(r"cooldown_until_ms=(\d+)", line)
            after = ("больше не торгуется (около 30 дней)" if "no_restart_latched=True" in line
                     else f"пауза по монете до {R.msk(int(until.group(1)))}" if until else "пауза по монете")
            return self.send(f"🛑 <b>{name}</b>: {esc(coin)} закрыта по стопу; {after}. Остальные монеты торгуются", critical=True)
        if "RED cooldown elapsed" in line:
            tiers.pop(coin, None)
            return self.send(f"▶️ <b>{name}</b>: пауза по монете {esc(coin)} закончилась — монета снова в работе")
        if "reset current episode" in line:     # позиция по монете закрылась — запрет докупок снят
            if prev:
                tiers[coin] = {"tier": "green", "sent": prev.get("sent", 0)}
            return
        m = re.search(r"\bstatus \| tier=(\w+)", line)
        if m:
            tier = m.group(1).lower()
            if tier == "orange" and prev.get("tier") not in ("orange", "red"):
                sent = prev.get("sent", 0)
                if time.time() - sent >= COIN_ORANGE_EVERY_S:
                    sent = time.time()
                    self.send(f"🟠 <b>{name}</b>: {esc(coin)}{lt} — докупки по монете остановлены, бот ждёт тейк")
                tiers[coin] = {"tier": tier, "sent": sent}
            elif tier in ("green", "yellow") and prev:
                tiers[coin] = {"tier": tier, "sent": prev.get("sent", 0)}

    def flush_errors(self):
        for app, e in (self.st.get("errs") or {}).items():
            if e["n"] and time.time() - e["sent"] >= ERR_EVERY_S:
                self.send(f"⚠️ <b>{NAMES.get(app, app)}</b>: ошибок в журнале — {e['n']}. Последняя:\n"
                          f"<code>{esc(R.brand(e['last']))}</code>")
                e.update(n=0, sent=time.time())

    def check_daily(self):
        path = os.path.join(R.REP, "latest.txt")
        mt, last = mtime(path), self.st.get("report_mtime")
        if last is None or mt is None:
            self.st["report_mtime"] = mt or 0
            return
        if mt > last:
            self.st["report_mtime"] = mt
            if self.s["daily"]:
                with open(path, encoding="utf-8") as f:
                    text = f.read().rstrip("\n")
                if self.trades_line():
                    text += "\n\n" + self.trades_line()      # одно сообщение в день: отчёт + сводка сделок
                self.send_pre("☀️ <b>Утренний отчёт</b>", text)

    # ---------------------------------------------------------------- цикл службы
    def run(self):
        log("Telegram-бот запущен")
        while True:
            self.reload_cfg()
            if not self.token:
                time.sleep(30)
                continue
            try:
                if self.outbox:
                    self.flush_outbox()
                self.poll()
            except TgError as e:
                log(f"Telegram: {e}")
                time.sleep(300 if e.code == 401 else 30 if e.code == 409 else 10)
            except Exception as e:
                log(f"нет связи с Telegram: {e}")
                time.sleep(10)
            if self.owner and time.time() - self.last_check >= CHECK_EVERY_S:
                self.last_check = time.time()
                try:
                    self.check()
                except Exception:
                    traceback.print_exc()


# -------------------------------------------------------------------- настройка с компьютера
def new_code(cfg):
    code = f"{secrets.randbelow(10 ** 6):06d}"
    cfg.update(pair_code=code, pair_expires=time.time() + PAIR_MINUTES * 60, pair_fails=0)
    save_private(CFG, cfg)
    u = cfg.get("bot_username")
    print(f"\nГотово. Откройте в Telegram бота @{u} (https://t.me/{u}), нажмите «Запустить» / Start")
    print(f"и отправьте ему код:   {code}")
    print(f"Код действует {PAIR_MINUTES} минут. После этого бот будет отвечать только вам.")


def setup():
    token = sys.stdin.readline().strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
        sys.exit("Это не похоже на токен Telegram (вида 123456789:AAH...). Скопируйте его из @BotFather целиком.")
    try:
        me = api(token, "getMe")
    except Exception as e:
        sys.exit(f"Telegram не принял токен: {e}")
    cfg = load_cfg()
    cfg.update(token=token, bot_username=me.get("username"), owner=None, owner_name=None)
    new_code(cfg)


def pair():
    cfg = load_cfg()
    if not cfg.get("token"):
        sys.exit("Токен ещё не задан — сначала настройка с токеном.")
    new_code(cfg)


def test():
    cfg = load_cfg()
    if not (cfg.get("token") and cfg.get("owner")):
        sys.exit("Бот ещё не привязан.")
    api(cfg["token"], "sendMessage", chat_id=cfg["owner"], text="✅ Проверка связи: сервер пишет в Telegram.")
    print(f"Отправлено владельцу {cfg.get('owner_name')}.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"setup": setup, "pair": pair, "test": test, "run": lambda: Bot().run()}[mode]()
