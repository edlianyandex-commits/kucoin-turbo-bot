#!/usr/bin/env python3
"""Наблюдатель за живым turbo-ботом. ТОЛЬКО ЧТЕНИЕ биржи, ничего не меняет.

Пишет:
  /opt/tradebot/reports/live_turbo.csv        — срез каждые 5 минут
  /opt/tradebot/reports/live_turbo_events.log — noteworthy события
  /opt/tradebot/reports/live_turbo_state.json — последний срез + счётчики

Отправляет в телеграм (тот же бот и чат, что у отчётов):
  падение и подъём процесса бота, срабатывание защиты BTC,
  зависание бота (журнал молчит при живом процессе), сбой самого наблюдателя.

На торговлю не влияет: ни одного действия с биржей, кроме чтения баланса и позиций.

Зачем отдельный отчёт.py: он не видит состояние защиты от обвала BTC внутри
ядра (в панели только пауза самой панели). А это главный вопрос live-теста.
"""
import csv
import json
import os
import re
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = "/opt/tradebot/turbo"
REPORTS = "/opt/tradebot/reports"
CSV = os.path.join(REPORTS, "live_turbo.csv")
EVT = os.path.join(REPORTS, "live_turbo_events.log")
STATE = os.path.join(REPORTS, "live_turbo_state.json")
COLS = ["ts", "free", "used", "total", "n_pos", "notional", "exposure_x", "n_orders",
        "n_trades", "guard_fires", "guard_ok", "new_err", "new_warn", "rss_mb", "bot_uptime",
        "transfers_cum", "trading_equity"]

# --- переводы владельца (ДИРЕКТИВА №4.1, вариант Г) ---
# Боевой учёт: transfers_cum берётся ТОЛЬКО из live_turbo_state.json и никогда не
# меняется автоматически — иначе метрика trading_equity дрейфует.
# Автодетект — advisory: только запись в live_turbo_events.log, на state не влияет.
ADVISORY_MIN_DELTA = 0.5     # USDT: |B - A| в строке [balance] raw A -> B
ADVISORY_FILL_WINDOW_S = 2   # ± сек Around: закрытие в ту же секунду
ADVISORY_PNL_RATIO = 0.5     # |pnl| >= |delta| * ratio  -> движение объяснено сделкой
# Стартовое значение накопленных переводов для наблюдателя, который запускается
# уже после того, как переводы состоялись (иначе нечего считать по журналу).
TRANSFERS_SEED = -191.58311653
TRANSFERS_SEED_FROM = datetime(2026, 10, 14, 21, 42, 0, tzinfo=timezone.utc).timestamp()

LOCK = "/tmp/live_watch_turbo.lock" if os.name != "nt" else os.path.join(
    os.environ.get("TEMP", "."), "live_watch_turbo.lock")

BALANCE_RE = re.compile(r"\[balance\]\s*raw\s+([+-]?\d+(?:\.\d+)?)\s*->\s*([+-]?\d+(?:\.\d+)?)")
FILL_PNL_RE = re.compile(r"pnl=([+-]?\d+(?:\.\d+)?)")
LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

# --- уведомления в телеграм ---
TG_CFG = "/opt/tradebot/telegram/config.json"
TG_LAST = os.path.join(REPORTS, "live_turbo_tg_last.json")
TG_SENT = os.path.join(REPORTS, "live_turbo_tg_sent.log")
# сколько минут не повторять одно и то же событие
COOLDOWN_MIN = 60
# сколько минут журнал может молчать при живом процессе, прежде чем считать зависанием
HUNG_MIN = 25
# уведомлять ли об изменении числа позиций (часто, поэтому по умолчанию выключено)
NOTIFY_POSITIONS = False


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def acquire_lock():
    """Неблокирующая блокировка: наблюдатель не должен запускаться в двух копиях.

    Возвращает открытый файл (держит lock) либо None, если lock уже занят —
    тогда текущий процесс должен молча выйти.
    """
    try:
        import fcntl
        fh = open(LOCK, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        return fh
    except ImportError:
        pass
    # Windows: msvcrt.locking по одному байту (fallback, как и предусмотрено планом)
    try:
        import msvcrt
        fh = open(LOCK, "a+")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        return fh
    except ImportError:
        pass
    # Файловой блокировки нет вовсе — работаем по PID-файлу.
    try:
        if os.path.exists(LOCK):
            with open(LOCK, encoding="utf-8") as f:
                pid = int((f.read().strip() or "0"))
            os.kill(pid, 0)          # живой процесс — значит lock занят
            return None
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return None                  # разлочить нечем, работа PID-файла одноразовая
    except Exception:
        return None


def default_transfers_cum(now_ts=None):
    """Переводы, уже состоявшиеся до появления наблюдателя (иначе по журналу их не увидеть)."""
    ts = time.time() if now_ts is None else now_ts
    return TRANSFERS_SEED if ts > TRANSFERS_SEED_FROM else 0.0


def ensure_csv_cols():
    """Обратная совместимость заголовка: новые колонки дописываются в существующий CSV."""
    if not os.path.exists(CSV):
        try:
            with open(CSV, "w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(COLS)
        except Exception:
            pass
        return COLS
    try:
        with open(CSV, encoding="utf-8") as f:
            head = f.readline()
        cols = next(csv.reader([head]))
        missing = [c for c in COLS if c not in cols]
        if not missing:
            return cols
        new_cols = cols + missing
        with open(CSV, encoding="utf-8") as f:
            body = f.read()
        with open(CSV, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(new_cols)
            f.write(body)
        return new_cols
    except Exception:
        return COLS


def _save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def tg_send(text, key=None, cooldown_min=COOLDOWN_MIN):
    """Отправить сообщение в телеграм. key + cooldown защищает от повторов."""
    try:
        if key and cooldown_min > 0:
            last = _load_json(TG_LAST, {}).get(key, 0)
            if time.time() - last < cooldown_min * 60:
                return False
        cfg = _load_json(TG_CFG, None)
        if not cfg or not cfg.get("token"):
            return False
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
            data=json.dumps({"chat_id": cfg["owner"], "text": text,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = bool(json.load(r).get("ok"))
        if ok:
            try:
                with open(TG_SENT, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} отправлено: "
                            f"{text[:160]}\n")
            except Exception:
                pass
            if key:
                d = _load_json(TG_LAST, {})
                d[key] = time.time()
                _save_json(TG_LAST, d)
        return ok
    except Exception as e:
        try:
            with open(TG_SENT, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} "
                        f"НЕ ОТПРАВЛЕНО ({type(e).__name__}): {str(e)[:120]}\n")
        except Exception:
            pass
        return False


def log_event(msg, notify=False, key=None):
    try:
        with open(EVT, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}\n")
    except Exception:
        pass
    if notify:
        head = "turbo-бот · " + datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
        tg_send(f"⚠️ {head}\n{msg}", key=key)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def newest_log():
    logs = "/opt/tradebot/turbo/logs"
    try:
        files = [os.path.join(logs, n) for n in os.listdir(logs) if "passivbot_live" in n]
        return max(files, key=os.path.getmtime) if files else None
    except Exception:
        return None


def scan_log(path, prev_size):
    """Читает только НОВУЮ часть журнала: считает строки, ищет события и кандидатов в переводы.

    Advisory-детект (ДИРЕКТИВА №4.1): в строке `[balance] raw A -> B` величина
    `B - A` — это дельта одного последнего обновления баланса. Если она крупная
    (>= 0.5 USDT), начало не с нуля и рядом нет закрытия на сопоставимую сумму —
    это кандидат в перевод владельца. Только запись в журнал, state не меняется.
    """
    out = {"err": 0, "warn": 0, "guard_fires": 0, "guard_ok": 0,
           "entries": 0, "closes": 0, "size": 0, "candidates": []}
    if not path or not os.path.exists(path):
        return out
    size = os.path.getsize(path)
    out["size"] = size
    if prev_size is None or prev_size >= size:
        return out
    try:
        with open(path, "rb") as f:
            f.seek(prev_size)
            chunk = f.read().decode("utf-8", "replace")
    except Exception:
        return out
    recent_fills = []          # [(datetime, |pnl|)] — закрытия рядом по времени
    for line in chunk.splitlines():
        m_ts = LINE_TS_RE.match(line)
        ts = None
        if m_ts:
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                ts = None
        if " ERROR " in line:
            out["err"] += 1
        elif " WARNING " in line:
            out["warn"] += 1
        if "[btc_guard]" in line:
            if "СРАБАТЫВАНИЕ" in line:
                out["guard_fires"] += 1
                log_event("ЗАЩИТА BTC СРАБОТИЛА: " + line[-260:],
                          notify=True, key="guard")
            elif "проверка при старте" in line or "данные BTC снова есть" in line:
                out["guard_ok"] = 1
        if "] [order]" in line and (" post " in line or " executed " in line):
            if "entry" in line:
                out["entries"] += 1
        if "close" in line and "post" in line:
            out["closes"] += 1

        if "[fill]" in line:
            m = FILL_PNL_RE.search(line)
            if m and ts is not None:
                try:
                    recent_fills.append((ts, abs(float(m.group(1)))))
                except ValueError:
                    pass
                if len(recent_fills) > 50:
                    del recent_fills[:-50]
            continue

        mb = BALANCE_RE.search(line)
        if not mb:
            continue
        try:
            a = float(mb.group(1))
            b = float(mb.group(2))
        except ValueError:
            continue
        if a <= 0.0:                       # старт бота `0 -> X` — не кандидат
            continue
        delta = b - a
        if abs(delta) < ADVISORY_MIN_DELTA:
            continue
        # закрытие позиции в ту же секунду на сопоставимую сумму — это торговля, не перевод
        explained = False
        if ts is not None:
            for fts, fp in recent_fills:
                if abs((fts - ts).total_seconds()) <= ADVISORY_FILL_WINDOW_S:
                    if fp >= abs(delta) * ADVISORY_PNL_RATIO:
                        explained = True
                        break
        if explained:
            continue
        out["candidates"].append({"a": a, "b": b, "delta": delta,
                                   "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""})
    return out


def bot_proc():
    try:
        r = subprocess.run(["pgrep", "-f", "passivbot.*live"], capture_output=True, text=True)
        pids = []
        for p in r.stdout.split():          # pgrep -f ловит ботов — фильтр по папке приложения
            try:
                with open(f"/proc/{p}/cmdline", "rb") as f:
                    if ROOT.encode() in f.read():
                        pids.append(p)
            except Exception:
                pass
        if not pids:
            return None, 0, 0
        pid = pids[0]
        rss = 0
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) // 1024
        except Exception:
            pass
        up = 0
        try:
            s = subprocess.run(["ps", "-o", "etimes=", "-p", pid], capture_output=True, text=True)
            up = int(s.stdout.strip() or 0)
        except Exception:
            pass
        return pid, rss, up
    except Exception:
        return None, 0, 0


def exchange():
    import ccxt
    with open(os.path.join(ROOT, "api-keys.json"), encoding="utf-8") as f:
        k = json.load(f)["kucoin_turbo"]
    ex = ccxt.kucoinfutures({"enableRateLimit": True, "options": {"defaultType": "future"}})
    ex.apiKey = k["key"]
    ex.secret = k["secret"]
    ex.password = k.get("passphrase") or ""
    b = ex.fetch_balance()
    free = float((b.get("free") or {}).get("USDT", 0) or 0)
    used = float((b.get("used") or {}).get("USDT", 0) or 0)
    total = float((b.get("total") or {}).get("USDT", 0) or 0)
    pos = [p for p in ex.fetch_positions() if (p.get("contracts") or 0)]
    notional = sum(abs(float(p.get("notional") or 0)) for p in pos)
    try:
        orders = len(ex.fetch_open_orders() or [])
    except Exception:
        orders = -1
    try:
        trades = len(ex.fetch_my_trades() or [])
    except Exception:
        trades = -1
    return free, used, total, len(pos), notional, orders, trades


def main():
    os.makedirs(REPORTS, exist_ok=True)
    st = load_state()
    log = newest_log()
    sl = scan_log(log, st.get("log_size"))
    pid, rss, up = bot_proc()
    free = used = total = npos = notional = orders = trades = None
    ex_err = ""
    try:
        free, used, total, npos, notional, orders, trades = exchange()
    except Exception as e:
        ex_err = f"{type(e).__name__}: {str(e)[:120]}"
        log_event("ОШИБКА ЧТЕНИЯ БИРЖИ: " + ex_err)

    # экспозиция — к реальному капиталу на счёте; прежняя база из state застряла на прежнем уровне после вывода средств
    equity_base = total or st.get("equity_base") or 0.0
    expo = (notional / equity_base) if (equity_base and notional is not None) else None

    # --- переводы владельца: значение только из state, автодетекта нет (вариант Г) ---
    transfers_cum = st.get("transfers_cum")
    if transfers_cum is None:
        transfers_cum = default_transfers_cum()
    for c in sl.get("candidates") or []:
        log_event(f"[ADVISORY TRANSFER CANDIDATE] raw {c['a']:.6f} -> {c['b']:.6f} "
                  f"(delta={c['delta']:+.6f} USDT, {c['ts']}) — проверить вручную, "
                  f"state не изменён")
        tg_send(f"💡 Замечен вероятный ввод/вывод средств: {c['delta']:+.2f} USDT "
                f"(raw {c['a']:.2f} -> {c['b']:.2f}). Текущий transfers_cum в state "
                f"не изменен — проверьте и обновите при необходимости.",
                key=f"advisory_transfer_{c['ts'][:16]}")

    trading_equity = (round(total - transfers_cum, 4)
                      if total is not None else "")

    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "free": round(free, 2) if free is not None else "",
        "used": round(used, 2) if used is not None else "",
        "total": round(total, 2) if total is not None else "",
        "n_pos": npos if npos is not None else "",
        "notional": round(notional, 2) if notional is not None else "",
        "exposure_x": round(expo, 3) if expo is not None else "",
        "n_orders": orders if orders is not None else "",
        "n_trades": trades if trades is not None else "",
        "guard_fires": st.get("guard_fires", 0) + sl["guard_fires"],
        "guard_ok": sl["guard_ok"],
        "new_err": sl["err"],
        "new_warn": sl["warn"],
        "rss_mb": rss,
        "bot_uptime": up,
        "transfers_cum": round(transfers_cum, 8),
        "trading_equity": trading_equity,
    }

    fieldnames = ensure_csv_cols()
    try:
        with open(CSV, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, restval="", extrasaction="ignore")
            w.writerow(row)
    except Exception:
        pass

    if st.get("n_pos") is not None and npos is not None and npos != st.get("n_pos"):
        log_event(f"ЧИСЛО ПОЗИЦИЙ: {st.get('n_pos')} -> {npos}",
                  notify=NOTIFY_POSITIONS, key="positions")
    if st.get("n_trades") not in (None, -1) and trades not in (None, -1) and trades != st.get("n_trades"):
        log_event(f"СДЕЛОК ВСЕГО: {st.get('n_trades')} -> {trades}")

    # --- состояние процесса: главные уведомления ---
    prev_pid = st.get("pid")
    if pid is None and prev_pid is not None:
        log_event(f"БОТ УПАЛ: процесса нет (был pid={prev_pid}). "
                  f"Заявки и позиция остались на бирже.",
                  notify=True, key="down")
    if pid is not None and prev_pid is None:
        log_event(f"БОТ ПОДНЯЛСЯ pid={pid}", notify=True, key="up")
    if pid is not None and prev_pid is not None and str(pid) != str(prev_pid):
        log_event(f"БОТ ПЕРЕЗАПУЩЕН: pid {prev_pid} -> {pid}",
                  notify=True, key="restart")

    # --- зависание: процесс жив, но журнал не растёт ---
    now_ts = time.time()
    if pid is not None:
        size_now = sl["size"]
        size_prev = st.get("log_size")
        if size_prev is not None and size_now == size_prev:
            quiet_s = now_ts - (st.get("log_quiet_since") or now_ts)
        else:
            quiet_s = 0
        if quiet_s > HUNG_MIN * 60:
            log_event(f"БОТ ЗАВИС: процесс жив (pid={pid}), но журнал не "
                      f"продвинулся {int(quiet_s // 60)} мин при норме около 1 мин.",
                      notify=True, key="hung")
    else:
        quiet_s = 0

    if ex_err and st.get("last_ex_err") != ex_err:
        log_event("БИРЖА: " + ex_err)

    json.dump({"log_size": sl["size"], "n_pos": npos, "n_trades": trades,
               "guard_fires": row["guard_fires"], "pid": pid, "equity_base": equity_base,
               "last_ex_err": ex_err, "ts": row["ts"],
               "transfers_cum": round(transfers_cum, 8),
               "log_quiet_since": (now_ts if quiet_s > 0 else
                                   (st.get("log_quiet_since") if quiet_s else None))},
              open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    _lock = acquire_lock()
    if _lock is None:
        # Другой экземпляр наблюдателя уже работает — молча выходим.
        pass
    else:
        try:
            main()
        except Exception:
            tb = traceback.format_exc(limit=3)[-400:]
            try:
                log_event("СБОЙ НАБЛЮДАТЕЛЯ: " + tb)
            except Exception:
                pass
            try:
                tg_send("⚠️ turbo-бот · СБОЙ НАБЛЮДАТЕЛЯ\n"
                        "Скрипт наблюдения упал, состояние бота сейчас не проверяется.\n"
                        + tb, key="watcher_crash")
            except Exception:
                pass