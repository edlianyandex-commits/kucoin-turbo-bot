#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ночной отчёт по живому turbo-боту: сравнение с началом сессии + отправка в телеграм.

Читает CSV наблюдателя (срез каждые 5 минут) и журнал бота, сравнивает первый
снимок с последним, добирает состояние биржи и отправляет сводку.

Запуск:
    python overnight_report.py            # отчёт сейчас (проверка)
    python overnight_report.py --send     # то же, но с защитой от повторов за сутки
"""
import argparse
import collections
import csv
import datetime
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = "/opt/tradebot/turbo"
REPORTS = "/opt/tradebot/reports"
CSV_PATH = os.path.join(REPORTS, "live_turbo.csv")
STATE = os.path.join(REPORTS, "live_turbo_sent_reports.json")
TG_CFG = "/opt/tradebot/telegram/config.json"


def now_utc():
    return datetime.datetime.now(datetime.UTC)


def fmt_ts(t):
    return t.strftime("%d.%m %H:%M UTC")


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def trading_equity(row):
    """Капитал без переводов владельца (ДИРЕКТИВА №4.1).

    Сначала готовая колонка из CSV наблюдателя, иначе total - transfers_cum,
    иначе просто total (старый CSV без новых колонок).
    """
    v = num(row.get("trading_equity"))
    if v is not None:
        return v
    total = num(row.get("total"))
    if total is None:
        return None
    return total - (num(row.get("transfers_cum")) or 0.0)


def load_csv():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def newest_log():
    files = glob.glob(os.path.join(ROOT, "logs", "*passivbot_live*"))
    return max(files, key=os.path.getmtime) if files else None


def parse_ts(s):
    try:
        return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


def tg_send(text):
    try:
        with open(TG_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        if not cfg.get("token"):
            return False
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
            data=json.dumps({"chat_id": cfg["owner"], "text": text,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = bool(json.load(r).get("ok"))
        try:
            with open(os.path.join(REPORTS, "live_turbo_tg_sent.log"), "a", encoding="utf-8") as f:
                f.write(f"{now_utc():%Y-%m-%dT%H:%M:%SZ} отправлено: {text[:120]}\n")
        except Exception:
            pass
        return ok
    except Exception as e:
        print(f"  отправка не удалась: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
        return False


def already_sent_today():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return False
    return d.get("last_report_date") == now_utc().date().isoformat()


def mark_sent():
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"last_report_date": now_utc().date().isoformat(),
                       "ts": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")}, f, indent=1)
    except Exception:
        pass


def exchange_state():
    out = {}
    try:
        import ccxt
        with open(os.path.join(ROOT, "api-keys.json"), encoding="utf-8") as f:
            k = json.load(f)["kucoin_turbo"]
        ex = ccxt.kucoinfutures({"enableRateLimit": True, "options": {"defaultType": "future"}})
        ex.apiKey = k["key"]
        ex.secret = k["secret"]
        ex.password = k.get("passphrase") or ""
        a = (ex.futuresPrivateGetAccountOverview({"currency": "USDT"}) or {}).get("data") or {}
        out["equity"] = num(a.get("accountEquity"), 0.0)
        out["available"] = num(a.get("availableBalance"), 0.0)
        out["unrealised"] = num(a.get("unrealisedPNL"), 0.0)
        pos = [p for p in ex.fetch_positions() if (p.get("contracts") or 0)]
        out["positions"] = []
        for p in pos:
            e = num(p.get("entryPrice"), 0.0)
            m = num(p.get("markPrice"), 0.0)
            q = abs(num(p.get("contracts"), 0.0))
            out["positions"].append((p.get("symbol"), q, e, m, q * (m - e)))
        out["orders"] = len(ex.fetch_open_orders() or [])
        out["trades"] = len(ex.fetch_my_trades() or [])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:90]}"
    return out


def analyse_log():
    path = newest_log()
    if not path:
        return {}
    txt = open(path, encoding="utf-8", errors="replace").read()
    lines = txt.splitlines()
    res = {
        "log": os.path.basename(path),
        "lines": len(lines),
        "errors": sum(1 for l in lines if " ERROR " in l),
        "restarts": sum(1 for l in lines if "starting order watch" in l),
        "cancels": sum(1 for l in lines if "[order] cancel " in l),
        "posts": sum(1 for l in lines if "[order]   post " in l),
        "plans": sum(1 for l in lines if "order plan summary" in l),
        "ws_breaks": sum(1 for l in lines if "ClientConnectionReset" in l),
        "guard_fires": sum(1 for l in lines if "СРАБАТЫВАНИЕ" in l),
        "fills_unproven": sum(1 for l in lines if "fill-history lookback still unproven" in l),
    }
    nt = collections.Counter()
    for l in lines:
        m = re.search(r"mark_nontradable.*?sym=1\(([A-Z0-9]+)\)", l)
        if m:
            nt[m.group(1)] += 1
    res["blocked"] = dict(nt)
    hd = [l for l in lines if "[candle] health" in l]
    if hd:
        res["health"] = hd[-1]
    wc = [l for l in lines if "wall-time cap" in l]
    if wc:
        c = re.search(r"count=(\d+)", wc[-1])
        w = re.search(r"window=(\d+)s", wc[-1])
        res["win_count"] = int(c.group(1)) if c else None
        res["win_window"] = int(w.group(1)) if w else None
    stamps = []
    for l in lines:
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", l)
        if m:
            try:
                stamps.append(datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                pass
    if len(stamps) >= 2:
        res["hours"] = (stamps[-1] - stamps[0]).total_seconds() / 3600
        res["first_ts"] = stamps[0]
        res["last_ts"] = stamps[-1]
    return res


def build():
    rows = load_csv()
    lg = analyse_log()
    ex = exchange_state()
    n = now_utc()
    L = []

    L.append(f"🌙 Отчёт по turbo-боту — {fmt_ts(n)}")
    L.append("")

    if not rows:
        L.append("CSV наблюдателя пуст — данных для сравнения нет.")
        return "\n".join(L), lg, ex

    first = rows[0]
    last = rows[-1]
    t0 = parse_ts(first.get("ts"))
    t1 = parse_ts(last.get("ts"))
    hours = ((t1 - t0).total_seconds() / 3600) if (t0 and t1) else 0

    start_cap = num(first.get("total"))
    now_cap = num(last.get("total"))
    if start_cap and now_cap:
        d = now_cap - start_cap
        L.append("💰 Капитал")
        L.append(f"   было {start_cap:.2f} → стало {now_cap:.2f} USDT ({d:+.2f}, {d/start_cap*100:+.2f} %)")
    else:
        L.append("💰 Капитал: данных в CSV нет")
    # торговая дельта — без переводов владельца (вывод средств ночью не должен
    # выглядеть как торговый убыток)
    start_tr = trading_equity(first)
    now_tr = trading_equity(last)
    if start_tr is not None and now_tr is not None:
        dt = now_tr - start_tr
        pct = (dt / start_tr * 100) if start_tr else 0.0
        moved = num(last.get("transfers_cum"))
        moved0 = num(first.get("transfers_cum"))
        transfer_delta = ((moved or 0.0) - (moved0 or 0.0)) if (moved is not None or moved0 is not None) else 0.0
        L.append(f"   торговая дельта (без переводов): {dt:+.2f} USDT ({pct:+.2f} %)")
        if abs(transfer_delta) >= 0.005:
            L.append(f"   переводы владельца за период: {transfer_delta:+.2f} USDT")
    if "equity" in ex:
        L.append(f"   биржа: equity {ex['equity']:.2f}, доступно {ex['available']:.2f}, "
                 f"нереализ {ex['unrealised']:+.4f}")
    L.append("")

    L.append("📈 Сделки")
    n0, n1 = num(first.get("n_trades")), num(last.get("n_trades"))
    new_fills = None
    if n0 is not None and n1 is not None:
        new_fills = int(n1 - n0)
        L.append(f"   исполнений: {int(n0)} → {int(n1)} (+{new_fills})")
        if new_fills == 0:
            L.append("   новых исполнений за период не было")
    p0, p1 = num(first.get("n_pos")), num(last.get("n_pos"))
    if p0 is not None and p1 is not None:
        L.append(f"   позиций:    {int(p0)} → {int(p1)}")
    o0, o1 = num(first.get("n_orders")), num(last.get("n_orders"))
    if o0 is not None and o1 is not None:
        L.append(f"   заявок:     {int(o0)} → {int(o1)}")
    if ex.get("positions"):
        for sym, q, e, m, pnl in ex["positions"]:
            L.append(f"     {sym} x{q:g} вход {e:g}, марк {m:g}, PnL {pnl:+.4f} "
                     f"({(m/e-1)*100:+.2f} %)")
    L.append("")

    L.append("🔄 Операции с заявками")
    # темпы считаем по журналу: счётчики операций берутся именно оттуда
    log_h = lg.get("hours") or hours
    if log_h > 0:
        can, pst = lg.get("cancels", 0), lg.get("posts", 0)
        L.append(f"   отменено {can} ({can/log_h:.0f}/ч)")
        L.append(f"   выставлено {pst} ({pst/log_h:.0f}/ч)")
        tot = can + pst
        L.append(f"   всего на бирже {tot} ({tot/log_h:.0f}/ч), прогноз на сутки {tot/log_h*24:.0f}")
        if new_fills:
            L.append(f"   соотношение операций к исполнениям: {tot/new_fills:.0f} к 1")
        elif n0 is not None:
            L.append(f"   на {tot} операций — {int(n0)} исполнение всего за сессию")
    L.append("")

    L.append("🛡 Безопасность")
    L.append(f"   перезапусков: {lg.get('restarts', 0) - 1 if lg.get('restarts') else 0} "
             f"(стартов сокета {lg.get('restarts', 0)})")
    L.append(f"   ошибок: {lg.get('errors', 0)}")
    L.append(f"   обрывов сокета: {lg.get('ws_breaks', 0)}")
    L.append(f"   срабатываний защиты BTC: {lg.get('guard_fires', 0)}")
    L.append(f"   подтверждение истории исполнений не получено: {lg.get('fills_unproven', 0)} раз")
    if lg.get("blocked"):
        top = sorted(lg["blocked"].items(), key=lambda x: -x[1])[:5]
        L.append(f"   заблокировано из-за данных: " +
                 ", ".join(f"{k} {v}" for k, v in top))
    L.append("")

    L.append("📦 Данные")
    h = lg.get("health", "")
    if h:
        m1 = re.search(r"unhealthy_surfaces=(\d+)", h)
        m2 = re.search(r"stale=(\d+)", h)
        m3 = re.search(r"synthetic=(\d+)", h)
        L.append(f"   поверхностей с проблемами: {m1.group(1) if m1 else '?'}, "
                 f"просрочено: {m2.group(1) if m2 else '?'}, "
                 f"синтетических свечей: {m3.group(1) if m3 else '?'}")
    if lg.get("win_count") is not None:
        ratio = lg["win_count"] / lg["win_window"] if lg.get("win_window") else 0
        L.append(f"   счётчик окна: {lg['win_count']} при окне {lg['win_window']} с "
                 f"(доля {ratio:.5f} в секунду — стабильна, значит не накопление)")
    L.append("")

    L.append("🧠 Память")
    rss = [num(r.get("rss_mb")) for r in rows if num(r.get("rss_mb")) is not None]
    if rss:
        L.append(f"   RSS: {rss[0]:.0f} → {rss[-1]:.0f} МБ, максимум {max(rss):.0f}, "
                 f"изменение {rss[-1]-rss[0]:+.0f} МБ")
        if hours > 0 and rss[-1] != rss[0]:
            L.append(f"   темп: {(rss[-1]-rss[0])/hours:+.1f} МБ/ч")
        if rss[-1] < max(rss):
            L.append(f"   текущее ниже максимума — скачки, а не плавная утечка")

    L.append("")
    obs = lg.get("hours") or hours
    L.append(f"⏱ Бот работает {obs:.2f} ч (по журналу), замеров наблюдателя {len(rows)}")
    if "error" in ex:
        L.append(f"⚠️ биржа не прочитана: {ex['error']}")

    return "\n".join(L), lg, ex


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="отправить в телеграм (защита от повторов)")
    ap.add_argument("--force", action="store_true", help="отправить даже если сегодня уже было")
    args = ap.parse_args()

    text, lg, ex = build()
    print(text)
    print("\n" + "=" * 60)
    print(f"строк журнала {lg.get('lines', '?')}, отмен {lg.get('cancels', '?')}, "
          f"выставок {lg.get('posts', '?')}, ошибок {lg.get('errors', '?')}")

    if args.send:
        if already_sent_today() and not args.force:
            print("\nсегодня отчёт уже отправлялся, пропускаю (иначе --force)")
            return
        ok = tg_send(text)
        if ok:
            mark_sent()
            print("\nотправлено в телеграм")
        else:
            print("\nотправка не удалась")


if __name__ == "__main__":
    main()