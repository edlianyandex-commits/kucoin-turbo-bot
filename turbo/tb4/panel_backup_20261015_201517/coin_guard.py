"""Защита от делистинга (02.10.2026): монеты, которые KuCoin собирается убрать с фьючерсов или которые лучше не трогать.

Признаки — публичные данные KuCoin, без ключей:
- у бессрочного контракта появилась дата закрытия (expireDate / settleDate) — делистинг назначен → «закрыть»;
- объявление KuCoin о делистинге фьючерса называет контракт монеты (announcements, delistings) → «закрыть»;
- контракта больше нет среди активных → «не входить»;
- спот монеты помечен ST (особый контроль — кандидат на делистинг) → «не входить»;
- оборот фьючерса за сутки ниже 1 млн $ → «не входить» (вернуть — от 3 млн $); запасной признак: оборот шумный
  (01.10 у ARK 1,3 млн $ при обычных 5+), а наши позиции на порядки меньше — проскальзывание не страшно.
«Не входить» — монета в файле ignored_coins, TB Core перечитывает его на ходу (открытую позицию ведёт как обычно);
«закрыть» — ещё и перезапуск TB Core с режимом panic только для этой монеты (coin_overrides), остальные торгуют.
За 07.2025–09.2026 KuCoin делистил ~120 фьючерсов (в т.ч. TON 15.06.2026), предупреждая обычно за 2–6 дней.

Фильтр разогнанных монет (02.10.2026, включает панель Turbo): дневное закрытие фьючерса KuCoin за прошлые 30 полных
дней UTC менялось в ratio раз и больше (максимум / минимум) → «не входить». Такие монеты (SIREN, PIPPIN в 2026) перед
обвалом всегда успевали разогнаться или рухнуть; в честной проверке (список монет по месяцам) Turbo без фильтра −81%,
с ratio 3 +518% за 15 мес. (2 и 4 тоже спасают счёт; проект Turbo turbo/turnover_rule.py). Закрытия, а не экстремумы:
минутные проколы вроде 10.10.2025 не в счёт.
Файл одинаковый у основного бота и Turbo.
"""
import re
import time
from datetime import date, datetime, timedelta, timezone

FUT_URL = "https://api-futures.kucoin.com/api/v1/contracts/active"
SPOT_URL = "https://api.kucoin.com/api/v2/symbols"
ANN_URL = "https://api.kucoin.com/api/v3/announcements"
KLINE_URL = "https://api-futures.kucoin.com/api/v1/kline/query"
LOW_TURNOVER, BACK_TURNOVER = 1e6, 3e6      # $ за сутки: ниже — не входить, вернуть — от этого уровня
ANN_DAYS = 21                               # объявление о делистинге считается свежим столько дней
HOT_DAYS = 30                               # окно фильтра разогнанных монет, дней


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M")


def utc_day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def fetch_closes(session, coins, end_ms, days=HOT_DAYS + 2, pause=0.12):
    """Дневные закрытия фьючерсов KuCoin за days дней до end_ms: {монета: {день UTC «ГГГГ-ММ-ДД»: закрытие}}.
    KuCoin отдаёт не больше 200 свечей за запрос — длинный период берётся частями. Монета без ответа пропускается."""
    out = {}
    for coin, sym in coins.items():
        d, t0 = {}, end_ms - days * 86_400_000
        try:
            while t0 < end_ms:
                t1 = min(end_ms, t0 + 190 * 86_400_000)
                rows = session.get(KLINE_URL, params={"symbol": sym, "granularity": 1440, "from": t0, "to": t1},
                                   timeout=20).json().get("data") or []
                d.update({utc_day(r[0]): float(r[4]) for r in rows})
                t0 = t1
                time.sleep(pause)
        except Exception:
            continue
        out[coin] = d
    return out


def hot(closes, ratio, day_ms, days=HOT_DAYS):
    """Разогнанные монеты на день day_ms: максимум / минимум дневного закрытия за days полных дней UTC до этого дня
    ≥ ratio (0 — фильтр выключен) → {монета: во сколько раз}."""
    if not ratio:
        return {}
    today = date.fromisoformat(utc_day(day_ms))
    lo = today - timedelta(days=days)
    out = {}
    for coin, d in closes.items():
        w = [v for k, v in d.items() if lo <= date.fromisoformat(k) < today and v > 0]
        if w and max(w) / min(w) >= ratio:
            out[coin] = round(max(w) / min(w), 1)
    return out


def fetch(session, spot=True):
    """Данные KuCoin: контракты (дата закрытия, оборот), объявления о делистинге, монеты с меткой ST (spot=False — без)."""
    fut = session.get(FUT_URL, timeout=20).json()["data"]
    ann = (session.get(ANN_URL, params={"annType": "delistings", "lang": "en_US", "pageSize": 50, "currentPage": 1},
                       timeout=20).json().get("data") or {}).get("items") or []
    st = None
    if spot:
        st = sorted({x["baseCurrency"] for x in session.get(SPOT_URL, timeout=30).json()["data"]
                     if x.get("st") and x.get("quoteCurrency") == "USDT"})
    return {"futures": {f["symbol"]: {"expire": f.get("expireDate") or f.get("settleDate"),
                                      "turnover": float(f.get("turnoverOf24h") or 0)} for f in fut},
            "announcements": [{"t": int(a.get("cTime") or 0), "title": str(a.get("annTitle") or "")} for a in ann],
            "st": st}


def assess(coins, data, prev, now_ms, hot_coins=None, ratio=None):
    """coins — {монета: символ фьючерса KuCoin}; prev — прошлый итог; hot_coins — итог hot() (фильтр разогнанных,
    у основного бота не передаётся); → {монета: {level, reason, since_ms, delist_ms, hot}} для монет под защитой
    (level «close» — закрыть позицию, «ignore» — не входить)."""
    fut, st = data["futures"], data.get("st")
    recent = [a for a in data["announcements"]
              if now_ms - a["t"] <= ANN_DAYS * 86_400_000 and "Futures" in a["title"] and "Delist" in a["title"]]
    out = {}
    for coin, sym in coins.items():
        p = (prev or {}).get(coin) or {}
        f = fut.get(sym)
        tick = sym[:-1] if sym.endswith("M") else sym                  # в объявлениях контракт без «M»: ZECUSDT
        ann = next((a for a in recent if re.search(rf"(?<![A-Z0-9]){re.escape(tick)}(?![A-Z0-9])", a["title"])), None)
        bare = coin[4:] if coin.startswith("1000") else coin            # спот 1000BONK — это BONK
        level = reason = delist = None
        if f is None:
            level, reason = "ignore", "контракта больше нет среди активных на KuCoin"
        elif f["expire"]:
            level, delist = "close", int(f["expire"])
            reason = f"KuCoin закроет контракт {fmt(delist)} (делистинг)"
        elif ann:
            level, reason = "close", f"KuCoin объявил делистинг: «{ann['title'][:100]}»"
        elif (st is not None and (coin in st or bare in st)) or (st is None and p.get("st")):
            level, reason = "ignore", "KuCoin пометил монету ST (особый контроль, кандидат на делистинг)"
        elif f["turnover"] < LOW_TURNOVER or (p.get("low_turnover") and f["turnover"] < BACK_TURNOVER):
            level, reason = "ignore", f"оборот за сутки упал до {f['turnover'] / 1e6:.1f} млн $"
        elif hot_coins and coin in hot_coins:
            level = "ignore"
            reason = (f"цена разогнана: за {HOT_DAYS} дней менялась в {hot_coins[coin]:g} раза "
                      f"(фильтр ×{ratio:g}) — новых входов нет")
        if level:
            out[coin] = {"level": level, "reason": reason, "since_ms": p.get("since_ms") or now_ms, "delist_ms": delist,
                         "st": bool(reason and "ST" in reason), "low_turnover": bool(reason and "оборот" in reason),
                         "hot": bool(reason and reason.startswith("цена разогнана"))}
    return out
