#!/usr/bin/env python3
"""Отчёт о ботах на сервере. Только читает состояние панелей и чистит старые журналы; в торговлю не вмешивается.

  report.py sample  — раз в час: записать капитал в reports/equity.csv, новые тревоги — в reports/alerts.log
  report.py daily   — раз в день: отчёт reports/ГГГГ-ММ-ДД.txt (+ копия latest.txt) и чистка журналов
Запускается из cron пользователя tradebot (deploy/server_cron.sh). Только стандартная библиотека Python.
"""
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Moscow")
except Exception:
    TZ = timezone(timedelta(hours=3))

BASE = "/opt/tradebot"
REP = os.path.join(BASE, "reports")
EQ_CSV = os.path.join(REP, "equity.csv")
ALERTS = os.path.join(REP, "alerts.log")
STATE = os.path.join(REP, ".state.json")
# 16.10.2026 (ДИРЕКТИВА №4.1): накопленные переводы владельца ведёт наблюдатель live_watch.py
LIVE_STATE = os.path.join(REP, "live_turbo_state.json")
LIVE_EVENTS = os.path.join(REP, "live_turbo_events.log")
# 15.10.2026: основной бот удалён, остался turbo
APPS = (("turbo", "Турбо", 8771, "kucoin_turbo"),)
FIELDS = ["time", "app", "running", "want_running", "equity", "balance", "positions",
          "transfers_cum", "trading_equity"]

DD_WARN = 0.25          # просадка от пика с запуска бота, при которой стоит тревога (стоп Turbo — 35%, у основного — стоп по монете)
STALE_MIN = 15          # снимок TB Core старше — бот, вероятно, завис
MEM_MIN_MB = 250
DISK_MIN_GB = 3
KEEP_LOG_DAYS = 30      # журналы TB Core старше — удаляются
CONSOLE_MAX_MB = 20     # консольный журнал панели больше — обрезается до последних 5 МБ
KEEP_REPORT_DAYS = 90
KEEP_EQUITY_DAYS = 365


def now():
    return datetime.now(TZ)


def to_msk(text):
    """Время из панели ('ГГГГ-ММ-ДД ЧЧ:ММ', часовой пояс сервера) → МСК в том же виде."""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return text


def minutes_since(text):
    try:
        return (now() - datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def panel_state(port):
    try:
        return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=60))
    except Exception as exc:
        return {"error": str(exc)}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price(v):
    """Цена без хвоста знаков: 0.1655677419 → 0.165568."""
    x = num(v)
    return "—" if x is None else f"{x:.6g}"


def btc_pause_text(p):
    """Пауза после обвала BTC (основной бот и Turbo 5.0): панель сама продолжит торговлю."""
    if p.get("state") == "closing":
        return f"обвал BTC ({p.get('drop_pct')}%): закрываю все позиции, затем пауза до {p.get('resume_after')}"
    eq = num(p.get("equity"))
    return (f"пауза после обвала BTC: позиции закрыты {p.get('closed') or p.get('since')}"
            + (f", капитал {money(eq)}" if eq is not None else "")
            + f" — продолжит сам после {p.get('resume_after')}, если BTC перестанет падать")


def collect():
    """Снимок обоих ботов: служба, панель, бот, капитал, позиции."""
    out = []
    for app, name, port, user in APPS:
        s = panel_state(port)
        live = s.get("live") or {}
        snap = live.get("snapshot") or {}
        want = bool(load_json(os.path.join(BASE, app, "tb4/panel/data/live.json"), {}).get("want_running"))
        ts = snap.get("ts")
        out.append({
            "app": app, "name": name, "user": user,
            "service": sh("systemctl", "is-active", f"tradebot-{app}") or "?",
            "restarts": sh("systemctl", "show", "-p", "NRestarts", "--value", f"tradebot-{app}") or "?",
            "panel_ok": "error" not in s, "panel_error": s.get("error"),
            "preset": (s.get("settings") or {}).get("preset"), "keys": (s.get("keys") or {}).get("set"),
            "preset_name": ((s.get("presets") or {}).get((s.get("settings") or {}).get("preset")) or {}).get("name"),
            "running": bool(live.get("running")), "want_running": want, "started": to_msk(live.get("started")),
            "equity": num(snap.get("equity")), "balance": num(snap.get("balance")),
            "positions": snap.get("positions") or [], "orders": snap.get("orders"),
            "snap_age_min": round((time.time() * 1000 - ts) / 60000, 1) if ts else None,
            "bet": s.get("bet"), "hsl": snap.get("hsl"), "gave_up": live.get("gave_up"),
            "guard": (s.get("coin_guard") or {}).get("coins") or {},
            "btc_pause": live.get("btc_pause"),
        })
    return out


BET_STATES = {"waiting_start": "ждёт первых данных с биржи", "running": "идёт", "locking": "фиксация — закрываю позиции",
              "done": "закрыта", "stopped": "остановлена",
              "btc_pause": "пауза после обвала BTC (продолжится сама)"}


def msk(ms):
    """Время в мс → «дд.мм чч:мм МСК»."""
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%d.%m %H:%M МСК")


def hsl_text(h):
    """Состояние стопа TB Core по просадке словами (None — всё спокойно)."""
    if not h or not h.get("enabled"):
        return None
    if h.get("mode") == "coin":
        return hsl_coin_text(h.get("coins") or [])
    if h.get("halted") and h.get("no_restart_latched"):
        return "сработал стоп по просадке (без перезапуска): позиции закрыты, новых сделок не будет"
    if h.get("halted"):
        until = h.get("cooldown_until_ms")
        when = msk(until) if until else "—"
        return f"сработал стоп по просадке: позиции закрыты, пауза до {when}, затем бот продолжит сам"
    if h.get("tier") == "red":
        return "сработал стоп по просадке — бот закрывает позиции"
    if h.get("tier") in ("yellow", "orange"):
        return f"защита капитала: уровень {'жёлтый' if h['tier'] == 'yellow' else 'оранжевый'} — просадка приближается к стопу"
    return None


def hsl_coin_text(coins):
    """Стоп по монете (сводка панели hsl_summary): какие монеты закрыты или без докупок (None — всё спокойно)."""
    def loss(c):
        return f" (минус {c['loss']:.2f} $)" if c.get("loss") else ""
    parts = []
    for c in coins:
        if c.get("halted"):
            until = c.get("cooldown_until_ms")
            parts.append(f"{c['coin']} закрыта, больше не торгуется" if c.get("no_restart_latched") else
                         f"{c['coin']} закрыта" + (f", пауза до {msk(until)}" if until else ""))
        elif c.get("tier") == "red":
            parts.append(f"{c['coin']} закрывается{loss(c)}")
        elif c.get("tier") == "orange":
            parts.append(f"{c['coin']} без докупок{loss(c)}")
    return "стоп по монете: " + "; ".join(parts) + " — остальные монеты торгуются" if parts else None


# движок для пользователя называется TB Core (02.10.2026): в показанных строках журнала — так же, пути не трогаем
BRAND_RE = re.compile(r"(?<![\w/\\.-])[Pp]assivbot(?![\w-])")


def brand(text):
    return BRAND_RE.sub("TB Core", text)


def guard_text(coins):
    """Защита от делистинга (панель, coin_guard.py): какие монеты бот не торгует и почему (None — всё в порядке).
    Монеты фильтра разогнанных (Turbo) — в hot_text."""
    coins = {c: x for c, x in (coins or {}).items() if not x.get("hot")}
    if not coins:
        return None
    close = [c for c, x in coins.items() if x.get("level") == "close"]
    return ("не торгуем: " + "; ".join(f"{c} — {x.get('reason')}" for c, x in sorted(coins.items()))
            + (f" (позицию по {', '.join(sorted(close))} бот закрывает)" if close else ""))


def hot_text(coins):
    """Фильтр разогнанных монет (панель Turbo, coin_guard.hot): какие монеты бот не покупает (None — таких нет)."""
    hot = {c: x for c, x in (coins or {}).items() if x.get("hot")}
    if not hot:
        return None
    times = {c: re.search(r"в ([\d.]+) раза", x.get("reason") or "") for c, x in hot.items()}
    return ("не покупаем " + ", ".join(c + (f" ×{times[c].group(1)}" if times[c] else "") for c in sorted(hot))
            + " — цена за 30 дней менялась в разы; открытые позиции бот ведёт как обычно")


def transfers_cum():
    """Накопленные переводы владельца из state наблюдателя (0.0, если их ещё нет)."""
    v = num(load_json(LIVE_STATE, {}).get("transfers_cum"))
    return v if v is not None else 0.0


def trading_value(equity, tc):
    """Скорректированный капитал: реальный эквити минус переводы владельца."""
    return None if equity is None else equity - tc


def row_trading(row):
    """Скорректированный капитал строки equity.csv (работает и до ретро-миграции)."""
    v = num(row.get("trading_equity"))
    if v is not None:
        return v
    eq = num(row.get("equity"))
    if eq is None:
        return None
    return eq - (num(row.get("transfers_cum")) or 0.0)


def last_minute_by_app():
    """Минутный таймстемп последней строки equity.csv по каждому боту."""
    out = {}
    try:
        with open(EQ_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row.get("app")] = row.get("time")
    except OSError:
        pass
    return out


def append_equity(rows):
    """Дописывает срез капитала. Повторный запуск в ту же минуту строку не дублирует."""
    tc = transfers_cum()
    t = now().strftime("%Y-%m-%d %H:%M")
    last = last_minute_by_app()
    written = {a for a in (r["app"] for r in rows) if last.get(a) == t}
    fresh = [r for r in rows if r["app"] not in written]
    if not fresh:
        return 0
    new = not os.path.exists(EQ_CSV)
    with open(EQ_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS)
        if new:
            w.writeheader()
        for r in fresh:
            eq = r["equity"]
            w.writerow({"time": t, "app": r["app"], "running": int(r["running"]), "want_running": int(r["want_running"]),
                        "equity": "" if eq is None else round(eq, 2),
                        "balance": "" if r["balance"] is None else round(r["balance"], 2),
                        "positions": len(r["positions"]),
                        "transfers_cum": round(tc, 8),
                        "trading_equity": "" if eq is None else round(trading_value(eq, tc), 4)})
    return len(fresh)


def history(app, since=None):
    """Скорректированный капитал бота из equity.csv (только пока бот работал), начиная с since."""
    pts = []
    try:
        with open(EQ_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["app"] == app and row["running"] == "1" and row["equity"] and (not since or row["time"] >= since):
                    v = row_trading(row)
                    if v is not None:
                        pts.append((row["time"], v, num(row["equity"])))
    except OSError:
        pass
    return pts


def drawdown(r):
    """(пик, текущая просадка, наибольшая просадка) с момента запуска бота.

    Потеря в USDT считается по торговому эквити (без переводов владельца), иначе вывод средств выглядит как обвал.
    Доля — от реального капитала на счёте: потеря / (эквити сейчас + потеря). Делить на торговый эквити нельзя: после
    вывода большей части средств он остаётся прежним, и просадка 8% реального счёта показывалась как 0,4%.
    Пик возвращается в реальных USDT (при нынешних переводах).
    """
    pts = history(r["app"], r["started"])
    tc = transfers_cum()
    cur_eq = trading_value(r["equity"], tc)
    if cur_eq is not None:
        pts.append(("now", cur_eq, r["equity"]))
    if not pts:
        return None, None, None
    peak, max_dd, cur = pts[0][1], 0.0, 0.0
    for _, te, eq in pts:
        peak = max(peak, te)
        loss = peak - te
        cur = loss / (eq + loss) if eq is not None and eq + loss > 0 else 0.0
        max_dd = max(max_dd, cur)
    return peak + tc, cur, max_dd


def mem_disk():
    avail = total = None
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
        total = int(info["MemTotal"].split()[0]) // 1024
        avail = int(info["MemAvailable"].split()[0]) // 1024
    except Exception:
        pass
    st = os.statvfs("/")
    free_gb = st.f_bavail * st.f_frsize / 1024 ** 3
    total_gb = st.f_blocks * st.f_frsize / 1024 ** 3
    return avail, total, free_gb, total_gb


def process_memory():
    """Память процессов ботов (МБ) по /proc: TB Core, панели, Telegram-бот."""
    names = {app: name for app, name, *_ in APPS}
    out = []
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if "/opt/tradebot/" not in cmd:
                continue
            with open(f"/proc/{pid}/status") as f:
                rss = next(int(x.split()[1]) for x in f if x.startswith("VmRSS")) / 1024
        except (OSError, StopIteration, ValueError):
            continue
        app = next((a for a in names if f"/opt/tradebot/{a}/" in cmd), None)
        if "telegram_bot.py" in cmd:
            out.append(("Telegram-бот", rss))
        elif app and "passivbot live" in cmd:
            out.append((f"{names[app]} бот (TB Core)", rss))
        elif app and "panel/server.py" in cmd:
            out.append((f"{names[app]}: панель", rss))
    return sorted(out, key=lambda x: -x[1])


def alerts_for(rows):
    out = []
    for r in rows:
        n = r["name"]
        if r["service"] != "active":
            out.append(f"{n}: служба не работает ({r['service']})")
        elif not r["panel_ok"]:
            out.append(f"{n}: панель не отвечает")
        if not r["panel_ok"]:
            continue                  # без панели не видно, работает ли бот — хватит тревоги «панель не отвечает»
        if r["want_running"] and not r["running"]:
            out.append(f"{n}: бот должен работать, но процесса нет")
            if r.get("gave_up"):
                out.append(f"{n}: бот падал сразу после запуска — автоперезапуск остановлен, проверьте ключи и журнал")
        age = minutes_since(r["started"])
        just_started = age is not None and age < STALE_MIN
        if r["running"] and not just_started and (r["snap_age_min"] is None or r["snap_age_min"] > STALE_MIN):
            out.append(f"{n}: бот не обновлял данные больше {STALE_MIN} минут — возможно, завис или нет связи с биржей")
        if r["running"]:
            _, cur, _ = drawdown(r)
            if cur is not None and cur >= DD_WARN:
                out.append(f"{n}: просадка {cur:.0%} от пика с запуска")
        bet = r.get("bet") or {}
        if bet.get("lock_warned") and bet.get("state") == "locking":
            out.append(f"{n}: позиции не закрылись после фиксации — закройте вручную на KuCoin")
        pz = (bet.get("btc_pause") if bet.get("state") == "btc_pause" else None) or r.get("btc_pause") or {}
        if pz.get("warned") and pz.get("state") == "closing":
            out.append(f"{n}: позиции не закрылись после обвала BTC — закройте вручную на KuCoin")
    avail, _, free_gb, _ = mem_disk()
    if avail is not None and avail < MEM_MIN_MB:
        out.append(f"мало свободной памяти: {avail} МБ")
    if free_gb < DISK_MIN_GB:
        out.append(f"мало места на диске: {free_gb:.1f} ГБ")
    return out


def alert_key(text):
    """Тревога та же, если отличаются только числа (просадка 26% → 28%)."""
    return re.sub(r"[\d.,]+", "#", text)


def log_alert_changes(current):
    """В alerts.log пишутся только новые тревоги и их снятие, чтобы файл не забивался одним и тем же."""
    st = load_json(STATE, {})
    before = {alert_key(a): a for a in st.get("alerts") or []}
    now_keys = {alert_key(a) for a in current}
    t = now().strftime("%Y-%m-%d %H:%M")
    lines = [f"{t}  ТРЕВОГА  {a}" for a in current if alert_key(a) not in before]
    lines += [f"{t}  снято    {a}" for k, a in sorted(before.items()) if k not in now_keys]
    if lines:
        with open(ALERTS, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    st["alerts"] = current
    save_json(STATE, st)


def new_errors(app, user):
    """Строки с ошибками, появившиеся в журналах бота с прошлого отчёта."""
    st = load_json(STATE, {})
    offs = st.setdefault("offsets", {})
    files = [os.path.join(BASE, app, "tb4/panel/data/live_console.log")]
    files += sorted(glob.glob(os.path.join(BASE, app, "logs", "*.log")))
    found = []
    for path in files:
        if os.path.islink(path):
            continue
        try:
            size = os.path.getsize(path)
            start = offs.get(path, 0)
            if start > size:        # файл обрезан или заменён
                start = 0
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(20 * 1024 * 1024).decode("utf-8", "replace")
            offs[path] = start + len(chunk.encode("utf-8"))
        except OSError:
            continue
        for line in chunk.splitlines():
            if "[risk] HSL[" in line:
                continue              # стоп по просадке — не ошибка, о нём отдельные сообщения
            if " ERROR " in line or "Traceback" in line or "CRITICAL" in line:
                found.append(line.strip()[:220])
    save_json(STATE, st)
    return found


def cleanup():
    removed, notes = 0, []
    cutoff = time.time() - KEEP_LOG_DAYS * 86400
    for app, *_ in APPS:
        for path in glob.glob(os.path.join(BASE, app, "logs", "*.log*")):
            if not os.path.islink(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        console = os.path.join(BASE, app, "tb4/panel/data/live_console.log")
        if os.path.exists(console) and os.path.getsize(console) > CONSOLE_MAX_MB * 1024 ** 2:
            with open(console, "rb") as f:
                f.seek(-5 * 1024 ** 2, os.SEEK_END)
                tail = f.read()
            with open(console, "r+b") as f:   # бот пишет в режиме дозаписи, обрезка на месте безопасна
                f.truncate(0)
                f.write(tail)
            notes.append(f"{app}: консольный журнал обрезан до 5 МБ")
    cutoff = time.time() - KEEP_REPORT_DAYS * 86400
    for path in glob.glob(os.path.join(REP, "20??-??-??.txt")):
        if os.path.getmtime(path) < cutoff:
            os.remove(path)
    keep_from = (now() - timedelta(days=KEEP_EQUITY_DAYS)).strftime("%Y-%m-%d")
    try:
        with open(EQ_CSV, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or FIELDS)
            rows = list(reader)
        if rows and rows[0]["time"] < keep_from:
            with open(EQ_CSV + ".tmp", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, header, restval="", extrasaction="ignore")
                w.writeheader()
                w.writerows(r for r in rows if r["time"] >= keep_from)
            os.replace(EQ_CSV + ".tmp", EQ_CSV)
    except OSError:
        pass
    return removed, notes


def du(path):
    return sh("du", "-sh", path).split("\t")[0] or "0"


def money(v):
    return "—" if v is None else f"{v:,.2f}".replace(",", " ")


def advisory_transfers(hours=24):
    """Кандидаты в переводы от наблюдателя за последние N часов.

    Их trading_equity не учитывает автоматически — владелец должен сверить
    transfers_cum в state вручную.
    """
    out = []
    cutoff = now() - timedelta(hours=hours)
    try:
        with open(LIVE_EVENTS, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "[ADVISORY TRANSFER CANDIDATE]" not in line:
                    continue
                try:
                    ts = datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=timezone.utc).astimezone(TZ)
                except ValueError:
                    continue
                if ts >= cutoff:
                    out.append(line.strip())
    except OSError:
        pass
    return out


def daily(rows, active):
    t = now()
    day_ago = (t - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    L = [f"Отчёт о ботах — {t:%d.%m.%Y %H:%M} (МСК)", ""]
    if active:
        L += ["ВНИМАНИЕ:"] + [f"  ! {a}" for a in active] + [""]
    else:
        L += ["Тревог нет.", ""]
    adv = advisory_transfers()
    if adv:
        L.append("ВНИМАНИЕ: замечен возможный ввод/вывод средств — trading_equity "
                 "не скорректирован автоматически:")
        L += [f"  ~ {a.split(' ', 1)[-1]}" for a in adv[-3:]]
        L.append("  Проверьте перевод и при необходимости обновите transfers_cum "
                 "в reports/live_turbo_state.json.")
        L.append("")
    for r in rows:
        L.append(f"== {r['name']} бот (пресет «{r.get('preset_name') or r['preset'] or '—'}»)")
        L.append(f"  служба: {r['service']}, перезапусков службы: {r['restarts']}, ключи: {'заданы' if r['keys'] else 'нет'}")
        if r["running"]:
            L.append(f"  бот работает с {r['started']}")
            L.append(f"  капитал {money(r['equity'])} USDT, баланс {money(r['balance'])} USDT, "
                     f"позиций {len(r['positions'])}, ордеров {r['orders'] or 0}")
            tc = transfers_cum()
            te = trading_value(r["equity"], tc)
            start_pts = history(r["app"], r["started"])
            first_te = start_pts[0][1] if start_pts else None
            if te is not None:
                pnl_te = (te - first_te) if first_te is not None else None
                L.append(f"  Реальный баланс: {money(r['equity'])} USDT | "
                         f"Торговый эквити (без переводов): {money(te)} USDT"
                         + (f" (PnL: {money(pnl_te)} USDT)" if pnl_te is not None else "")
                         + (f" | переводы владельца: {money(-tc)} USDT" if tc else ""))
            pts = history(r["app"], day_ago)
            # доли — от реального капитала в начале отрезка, а не от торгового эквити (в нём сидят выведенные деньги)
            if pts and te is not None and (pts[0][2] or 0) > 0:
                L.append(f"  за сутки: {(te - pts[0][1]) / pts[0][2]:+.1%} ({money(te - pts[0][1])} USDT)")
            if start_pts and te is not None and (start_pts[0][2] or 0) > 0:
                L.append(f"  с запуска: {(te - first_te) / start_pts[0][2]:+.1%}")
            peak, cur, mx = drawdown(r)
            if peak:
                L.append(f"  пик {money(peak)} USDT, просадка сейчас {cur:.1%}, наибольшая с запуска {mx:.1%}")
            for p in r["positions"]:
                L.append(f"    {str(p['symbol']).split('/')[0]} {p['side']}: объём {num(p['size']):g}, цена {price(p['price'])}, "
                         f"прибыль/убыток {money(num(p.get('upnl')))}")
            if hsl_text(r.get("hsl")):
                L.append(f"  стоп: {hsl_text(r['hsl'])}")
            if guard_text(r.get("guard")):
                L.append(f"  защита от делистинга: {guard_text(r['guard'])}")
            if hot_text(r.get("guard")):
                L.append(f"  фильтр разогнанных монет: {hot_text(r['guard'])}")
        elif r.get("btc_pause"):
            L.append("  " + btc_pause_text(r["btc_pause"]))
        else:
            L.append("  бот не запущен" + (" (но должен работать!)" if r["want_running"] else ""))
        bet = r.get("bet")
        if bet:
            line = f"  ставка: {BET_STATES.get(bet.get('state'), bet.get('state'))}"
            if bet.get("start_equity"):
                line += f", старт {money(bet['start_equity'])}, без цели"
            if bet.get("final_equity"):
                line += f", итог {money(bet['final_equity'])} ({bet.get('finished')})"
            L.append(line)
        errs = new_errors(r["app"], r["user"])
        if errs:
            L.append(f"  ошибок в журналах за сутки: {len(errs)}, последние:")
            L += [f"    {brand(e)}" for e in errs[-3:]]
        L.append("")
    avail, total, free_gb, total_gb = mem_disk()
    L.append(f"Сервер: работает {sh('uptime', '-p').replace('up ', '')}, память свободна {avail} из {total} МБ, "
             f"диск свободен {free_gb:.1f} из {total_gb:.0f} ГБ")
    procs = process_memory()
    if procs:
        L.append("Память процессов: " + ", ".join(f"{n} {m:.0f} МБ" for n, m in procs))
    L.append("Размер: " + ", ".join(f"{app} журналы {du(os.path.join(BASE, app, 'logs'))}, свечи {du(os.path.join(BASE, app, 'caches'))}"
                                    for app, *_ in APPS))
    removed, notes = cleanup()
    if removed or notes:
        L.append(f"Чистка: удалено журналов старше {KEEP_LOG_DAYS} дней: {removed}" + "".join(f"; {n}" for n in notes))
    text = "\n".join(L) + "\n"
    for name in (f"{t:%Y-%m-%d}.txt", "latest.txt"):
        with open(os.path.join(REP, name), "w", encoding="utf-8") as f:
            f.write(text)
    return text


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "sample"
    os.makedirs(REP, exist_ok=True)
    rows = collect()
    append_equity(rows)
    active = alerts_for(rows)
    log_alert_changes(active)
    if mode == "daily":
        print(daily(rows, active), end="")
    elif active:
        print("\n".join(active))


if __name__ == "__main__":
    main()
