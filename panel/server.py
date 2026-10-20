"""Панель бота на ядре TB Core (KuCoin Futures). Одна кодовая база на двух ботов.

Запуск: venv\\python tb4\\panel\\server.py → http://127.0.0.1:<порт из profile.json>
Сервер слушает только 127.0.0.1. Ключи API — в api-keys.json в корне бота.

Личность панели (порт, профиль ключей, режим, пресет по умолчанию) задаётся файлом profile.json
рядом с панелью; он ставится при установке бота, см. profile.turbo.json и profile.v4.json.
Режим из UI не меняется — он определяет набор правил и то, какой поток следит за BTC:

  mode = bet   турбо-ставка на отдельном субаккаунте. EMA Anchor без цели и без стопа панели,
               прибыль в обороте. Следит bet_watcher: обвал BTC → режим TB Core «panic»
               (закрыть позиции) → пауза до 00:00 UTC следующего дня → та же ставка продолжается.
               «Зафиксировать сейчас» — закрыть всё и закончить ставку.
  mode = main  основной бот. Следит btc_watcher: тот же выход при обвале BTC, но без ставки.

Общее у обоих правило одно — выход при обвале BTC. Различаются они тем, какой файл состояния
переводится в переход (bet.json против live.json) и какие правила подставляет пресет.
"""
import glob
import hashlib
import json
import math
import re
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TB4 = os.path.dirname(HERE)
ROOT = os.path.dirname(TB4)
sys.path.insert(0, TB4)
sys.path.insert(0, HERE)
import coin_guard as CG  # noqa: E402
import kucoin_refresh as KR  # noqa: E402
import make_config as MC  # noqa: E402
import proc  # noqa: E402

def _read_profile():
    """Личность панели: порт, профиль ключей, режим, пресет по умолчанию, метка. Ставится вместе с ботом."""
    with open(os.path.join(HERE, "profile.json"), encoding="utf-8-sig") as f:
        return json.load(f)


PROFILE = _read_profile()
MODE = PROFILE.get("mode") or "main"
if MODE not in ("main", "bet"):
    raise SystemExit(f"mode в profile.json — «main» или «bet», а не «{MODE}».")

DATA = os.path.join(HERE, "data")
PORT = int(PROFILE.get("port") or 8770)
PASSIVBOT = proc.venv_bin(ROOT, "passivbot")
LITE = os.environ.get("TB_LITE") == "1"   # сервер: без бэктестов и бумажной торговли (мало памяти)
KEYS_PATH = os.path.join(ROOT, "api-keys.json")
USER = PROFILE.get("user") or "kucoin_01"
MONITOR = os.path.join(ROOT, "monitor", "kucoin", USER, "state.latest.json")
BASE_CFG = MC.BASE
os.makedirs(DATA, exist_ok=True)

# Правила панели, которые подставляет пресет при его выборе. Режим main — выход при обвале BTC.
# Режим bet (турбо-ставка) — то же плюс плечо и фильтр разогнанных монет (hot_ratio: дневное закрытие за 30 дней
# менялось в столько раз → не входить; 0 — выкл). Набор берётся из mode в profile.json, поэтому одна кодовая база
# обслуживает обоих ботов, а различие поведения остаётся в presets.json.
MAIN_RULES = ("btc_exit", "btc_exit_hours")
BET_RULES = ("btc_exit", "btc_exit_hours", "leverage", "hot_ratio")
PRESET_RULES = BET_RULES if MODE == "bet" else MAIN_RULES

DEFAULT_SETTINGS = {"preset": PROFILE.get("default_preset") or "ema_wide",
                    "balance": 300.0, "leverage": 10, "btc_exit": 0.05, "btc_exit_hours": 12,
                    "exposure": None, "positions": None, "hsl_enabled": None, "hsl_threshold": None}
if MODE == "bet":
    DEFAULT_SETTINGS["hot_ratio"] = 3.0


class UIError(Exception):
    pass


# ------------------------------------------------------------------ файлы
def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:   # -sig: файл мог быть сохранён Блокнотом с BOM
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for attempt in range(40):          # Windows: замена не проходит, пока другой поток читает файл — повторить
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def presets(mode=None):
    """Пресеты режима (у каждого пресета свой mode: main|bet). Файл общий на обоих ботов."""
    p = load_json(os.path.join(HERE, "presets.json"), {})
    want = mode or MODE
    return {k: v for k, v in p.items()
            if not k.startswith("_") and (v.get("mode") or "main") == want}


def preset_rules(name):
    """Правила, которые подставляет пресет: у режима main — из «defaults», у bet — из «bet_defaults»."""
    body = presets().get(name) or {}
    return (body.get("bet_defaults") if MODE == "bet" else body.get("defaults")) or {}


def settings():
    """Настройки панели. Разбор сохранённого файла у двух режимов был разным — и сохранён как был, потому что
    от него зависит, какие правила окажутся в конфиге бота.

    Режим bet: ключи прежних версий (цель, стоп ставки, фильтры) в сохранённом файле пропускаются; сохранён
    убранный пресет — берётся оставшийся с его правилами (выход по BTC, плечо), а не правилами убранного.
    Режим main: сохранённое принимается целиком; убранный пресет заменяется пресетом по умолчанию, а правила,
    которых не было в файле, добираются из его defaults."""
    s = dict(DEFAULT_SETTINGS)
    saved = load_json(os.path.join(DATA, "settings.json"), {})
    if MODE == "bet":
        s.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        if s["preset"] not in presets():
            s["preset"] = next(iter(presets()))
            s.update({k: v for k, v in preset_rules(s["preset"]).items() if k in BET_RULES})
        return s
    s.update(saved)
    gone = s["preset"] not in presets()
    if gone:
        s["preset"] = DEFAULT_SETTINGS["preset"]
    for k in MAIN_RULES:
        if gone or k not in saved:
            s[k] = preset_rules(s["preset"]).get(k, 0 if k == "btc_exit" else 12)
    return s


def child_env():
    # MALLOC_ARENA_MAX: меньше «кусков» памяти у многопоточного процесса — ниже расход без потери скорости (Linux)
    return dict(os.environ, PYTHONUTF8="1", PATH=proc.env_path(ROOT), MALLOC_ARENA_MAX="2")


# ------------------------------------------------------------------ настройки → конфиг TB Core
def overrides_for(s):
    over = dict(presets()[s["preset"]].get("overrides") or {})
    if s.get("exposure"):
        over["bot.long.risk.total_wallet_exposure_limit"] = float(s["exposure"])
    if s.get("positions"):
        over["bot.long.risk.n_positions"] = float(int(s["positions"]))
    if s.get("hsl_enabled") is not None:
        over["bot.long.hsl.enabled"] = bool(s["hsl_enabled"])
    if s.get("hsl_threshold"):
        over["bot.long.hsl.red_threshold"] = float(s["hsl_threshold"])
    # Выход по обвалу BTC — в ядро, а не только в бумажную торговлю. До этого
    # правило жило в панели (btc_watcher) и работало, только пока панель запускает
    # бота. Теперь те же числа из настроек панели попадают в конфиг бота, и защита
    # не зависит от процесса панели. btc_exit = 0 выключает её полностью.
    if float(s.get("btc_exit") or 0) > 0:
        over["live.btc_crash_guard_enabled"] = True
        over["live.btc_crash_guard_drop_pct"] = float(s.get("btc_exit") or 0.05)
        over["live.btc_crash_guard_hours"] = int(s.get("btc_exit_hours") or 12)
        over["live.btc_crash_guard_extend_while_falling"] = True
        over["live.btc_crash_guard_resume_utc"] = "00:00"
    else:
        over["live.btc_crash_guard_enabled"] = False
    return over


def preset_base(s):
    """Базовый конфиг пресета: свой (подобранный оптимизатором) или настройки автора движка."""
    base = presets()[s["preset"]].get("base")
    return os.path.join(HERE, base) if base else BASE_CFG


def cfg_fp(s):
    """Отпечаток настроек стратегии (база пресета + правки): по нему страница видит, что расчёт на истории сделан на
    прежних настройках."""
    try:
        raw = open(preset_base(s), "rb").read()
    except OSError:
        raw = b""
    return hashlib.md5(raw + json.dumps(overrides_for(s), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def effective_params(s):
    """Итоговые экспозиция, позиции, доля первого входа — с учётом пресета и правок."""
    base = load_json(preset_base(s), {})
    risk = base["bot"]["long"]["risk"]
    strat = base["bot"]["long"]["strategy"]
    kind = (base.get("live") or {}).get("strategy_kind") or "trailing_martingale"
    initial = (strat["ema_anchor"]["base_qty_pct"] if kind == "ema_anchor"
               else strat["trailing_martingale"]["entry"]["initial_qty_pct"])
    hsl = base["bot"]["long"]["hsl"]
    # hsl_mode: "coin" — стоп по каждой монете (порог в долях доли монеты = баланс / позиций), иначе по всему счёту
    p = {"exposure": risk["total_wallet_exposure_limit"], "positions": risk["n_positions"],
         "allowance": risk["we_excess_allowance_pct"], "initial_pct": initial, "strategy": kind,
         "hsl_enabled": hsl["enabled"], "hsl_threshold": hsl["red_threshold"],
         "hsl_mode": (base.get("live") or {}).get("hsl_signal_mode") or "coin",
         "hsl_orange": (hsl.get("tier_ratios") or {}).get("orange", 0.5)}
    keys = {"bot.long.risk.total_wallet_exposure_limit": "exposure", "bot.long.risk.n_positions": "positions",
            "bot.long.risk.we_excess_allowance_pct": "allowance",
            "bot.long.strategy.trailing_martingale.entry.initial_qty_pct": "initial_pct",
            "bot.long.strategy.ema_anchor.base_qty_pct": "initial_pct",
            "bot.long.hsl.enabled": "hsl_enabled", "bot.long.hsl.red_threshold": "hsl_threshold",
            "live.hsl_signal_mode": "hsl_mode"}
    for k, v in overrides_for(s).items():
        if k in keys:
            p[keys[k]] = v
        elif k == "bot.long.hsl.tier_ratios":
            p["hsl_orange"] = v.get("orange", p["hsl_orange"])
    return p


def coins_status(s):
    """Какие монеты проходят минимальный ордер KuCoin при этом балансе (как фильтр TB Core)."""
    ku = load_json(os.path.join(TB4, "kucoin_coins.json"), {"coins": [], "kucoin": {}})
    mc = load_json(os.path.join(TB4, "kucoin_min_cost.json"), {})
    p = effective_params(s)
    n = max(1.0, float(p["positions"]))
    wel = p["exposure"] / n
    allow = min(max(0.0, p["allowance"]), max(0.0, p["exposure"] / wel - 1)) if wel > 0 else 0.0
    first = float(s["balance"]) * wel * (1 + allow) * p["initial_pct"]
    rows = []
    for c in ku["coins"]:
        t = ku["kucoin"][c]["turnover24h"]
        if t < MC.MIN_TURNOVER:
            continue
        rows.append({"coin": c, "min_cost": round(mc.get(c, 0.0), 2), "ok": mc.get(c, 1e9) <= first,
                     "turnover_m": round(t / 1e6)})
    rows.sort(key=lambda r: r["min_cost"])
    return {"first_entry": round(first, 2), "coins": rows, "ok_count": sum(r["ok"] for r in rows),
            "params": p}


# Сервер 2 ГБ / 1 ядро: меньше памяти, процессора и записи на диск. На торговлю не влияет:
# стратегии нужно ~3000 минутных свечей на монету (самая длинная средняя 2274 мин × 1,3), часовые свечи — отдельно;
# мониторинг TB Core — только выгрузка для его дашборда, панели нужен лишь state.latest.json.
LIVE_LIGHT = {
    "live.max_memory_candles_per_symbol": 20000,     # ~14 дней минутных свечей в памяти (было 200 000 — 139 дней)
    "monitor.snapshot_interval_seconds": 5.0,        # state.latest.json раз в 5 с (было раз в 1 с)
    "monitor.retain_price_ticks": False,             # не писать каждую цену на диск
    "monitor.retain_candles": False,
    "monitor.emit_completed_candles": False,
    "monitor.retain_days": 3.0,
    "monitor.max_total_bytes": 256 * 1024 ** 2,      # папка monitor не больше 256 МБ (было 1 ГБ)
}


def build_cfg(s, start, end, base_dir, balance=None, forced_mode="", coins=None):
    """Конфиг TB Core по настройкам панели: все ликвидные монеты KuCoin (tb4/kucoin_coins.json) или coins (бумага —
    без разогнанных на начало отрезка)."""
    over = {"backtest.filter_by_min_effective_cost": True, "backtest.base_dir": base_dir,
            "live.leverage": int(s.get("leverage") or 10), "live.user": USER,
            "live.forced_mode_long": forced_mode, **LIVE_LIGHT, **overrides_for(s)}
    cfg = MC.build(start, end, float(balance or s["balance"]), base_path=preset_base(s), overrides=over, coins=coins)
    cfg.pop("_source", None)
    if base_dir.endswith("/live"):          # боевой конфиг (его же проверяет deploy/validate_live.py)
        guard_cfg(cfg)
    return cfg


# ------------------------------------------------------------------ фоновые задачи (бэктест / бумага)
JOB = {"state": "idle"}
JOB_LOCK = threading.Lock()


def downsample(eq, n=400):
    step = max(1, len(eq) // n)
    idx = list(range(0, len(eq), step))
    if idx[-1] != len(eq) - 1:
        idx.append(len(eq) - 1)
    return [[int(t.timestamp() * 1000), round(float(v), 2)] for t, v in eq.iloc[idx].items()]


def summarize_run(run_dir, days):
    import pandas as pd                      # pandas (~60 МБ) грузится только для расчётов; на сервере их нет
    from run_tests import equity_stats
    st = equity_stats(os.path.join(run_dir, "balance_and_equity.csv.gz"), days)
    df = pd.read_csv(os.path.join(run_dir, "balance_and_equity.csv.gz"))
    df["t"] = pd.to_datetime(df.iloc[:, 0], utc=True)
    eq = df.set_index("t")["usd_total_equity"].astype(float)
    st["curve"] = downsample(eq)
    st["start_equity"] = round(float(eq.iloc[0]), 2)
    st["end_equity"] = round(float(eq.iloc[-1]), 2)
    fills = pd.read_csv(os.path.join(run_dir, "fills.csv"))
    st["fills"] = int(len(fills))
    if len(fills):
        g = fills.groupby("coin").agg(pnl=("pnl", "sum"), fees=("fee_paid", "sum"), n=("pnl", "size"))
        st["by_coin"] = [{"coin": c, "pnl": round(float(r.pnl + r.fees), 2), "fills": int(r.n)}
                         for c, r in g.sort_values("pnl", ascending=False).iterrows()]
    else:
        st["by_coin"] = []
    an = load_json(os.path.join(run_dir, "analysis.json"), {})
    st["completion"] = an.get("backtest_completion_ratio")
    cr = an.get("backtest_completion_ratio")
    st["liquidated"] = bool((cr is not None and cr < 0.999) or st["min_equity_pct"] <= -94)
    return st


def run_backtest_job(kind, s, start, end, days, stage_cb):
    run_dir = run_passivbot(kind, build_cfg(s, start, end, f"backtests/tb4/panel_{kind}"), stage_cb)
    res = summarize_run(run_dir, days)
    res.update(start=start, end=end, days=days, preset=s["preset"], balance=s["balance"],
               params=effective_params(s), fp=cfg_fp(s), finished=datetime.now().strftime("%Y-%m-%d %H:%M"))
    return res


MISSING_COIN = re.compile(r"No exchange data found for coin (\w+)")


class MissingCoin(Exception):
    def __init__(self, coin, msg):
        super().__init__(msg)
        self.coin, self.msg = coin, msg


def run_passivbot(kind, cfg, stage_cb):
    """Бэктест TB Core → папка прогона. Монета, по которой у биржи нет данных (убрана с KuCoin, а бумага считает по
    прошлому месячному списку), выбрасывается, и прогон повторяется — как в исследованиях, не больше 12 раз."""
    for _ in range(12):
        try:
            return run_passivbot_once(kind, cfg, stage_cb)
        except MissingCoin as e:
            long_ = list(cfg["live"]["approved_coins"]["long"])
            coins = [c for c in long_ if c != e.coin]
            if not coins or len(coins) == len(long_):
                raise UIError(e.msg)
            cfg["live"]["approved_coins"] = {"long": coins, "short": coins}
            for k in ("coin_sources", "market_settings_sources"):
                (cfg["backtest"].get(k) or {}).pop(e.coin, None)
            stage_cb(f"У биржи нет данных по {e.coin} — считаю без неё…")
    raise UIError("Слишком много монет без данных биржи — проверьте список монет.")


def run_passivbot_once(kind, cfg, stage_cb):
    """Один прогон TB Core по готовому конфигу → папка прогона (balance_and_equity.csv.gz, fills.csv)."""
    base_dir = cfg["backtest"]["base_dir"]
    cfg_path = os.path.join(ROOT, "configs", "tb4", f"panel_{kind}.json")
    save_json(cfg_path, cfg)
    before = set(glob.glob(os.path.join(ROOT, base_dir, "*", "*")))
    p = subprocess.Popen([PASSIVBOT, "backtest", cfg_path, "-dp", "y"], cwd=ROOT, env=child_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", **proc.hidden_kwargs())
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-40:]
        if "Fetched" in line or "Processing batch" in line:
            stage_cb("Загружаю историю цен… " + line.split(" => ")[0][-40:])
        elif "materializer" in line and "coin done" in line:
            stage_cb("Готовлю данные…")
        elif "Backtesting" in line:
            stage_cb("Считаю сделки…")
    p.wait()
    runs = sorted(set(glob.glob(os.path.join(ROOT, base_dir, "*", "*"))) - before, key=os.path.getmtime)
    if p.returncode != 0 or not runs:
        msg = "\n".join(tail[-12:])
        m = MISSING_COIN.search("\n".join(tail)) if p.returncode != 0 else None
        if m:
            raise MissingCoin(m.group(1), msg)
        if "no tradable candles" in msg:
            msg = "Слишком короткий период: стратегии нужен запас истории для индикаторов. Выберите период подлиннее."
        raise UIError(msg)
    return runs[-1]


def start_job(kind, fn):
    if LITE:
        raise UIError("На сервере бэктесты и бумажная торговля отключены (мало памяти). Запускайте их на компьютере.")
    with JOB_LOCK:
        if JOB.get("state") == "running":
            raise UIError("Уже идёт расчёт — дождитесь окончания.")
        JOB.clear()
        JOB.update(state="running", kind=kind, stage="Запуск…", started=time.time())

    def stage(text):
        JOB["stage"] = text

    def work():
        try:
            res = fn(stage)
            save_json(os.path.join(DATA, f"{kind}_result.json"), res)
            JOB.update(state="done", finished=time.time())
        except UIError as exc:
            JOB.update(state="error", error=str(exc))
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            JOB.update(state="error", error=f"Непредвиденная ошибка: {exc}")
    threading.Thread(target=work, daemon=True).start()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def backtest(days):
    days = int(days)
    if days not in (30, 60, 90, 160, 300):
        raise UIError("Период: 30, 60, 90, 160 или 300 дней.")
    s = settings()
    end = today()
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    start_job("backtest", lambda cb: run_backtest_job("backtest", s, start, end, days, cb))


def kucoin_session():
    """Сессия для публичных данных KuCoin — мимо системного прокси (как ключи и проверка BTC)."""
    import requests
    s = requests.Session()
    s.trust_env = False
    return s


def kucoin_btc_closes(t_from_ms, t_to_ms):
    """Часовые закрытия BTC-USDT на KuCoin за период: [[время открытия свечи, мс, закрытие], …] (публичные данные)."""
    import requests
    s = requests.Session()
    s.trust_env = False
    out, t = {}, t_from_ms // 1000
    end = t_to_ms // 1000
    while t < end:
        stop = min(end, t + 1400 * 3600)
        rows = s.get("https://api.kucoin.com/api/v1/market/candles",
                     params={"type": "1hour", "symbol": "BTC-USDT", "startAt": t, "endAt": stop}, timeout=15).json()["data"]
        for r in rows:
            out[int(r[0]) * 1000] = float(r[2])
        t = stop
    return sorted([k, v] for k, v in out.items())


def apply_bet_rules(t_ms, eq, rules, btc=None):
    """Где торговля прервалась бы по правилам панели: (индекс минуты или None, причина).
    t_ms/eq — капитал по минутам с начала отрезка; rules — btc_drop, btc_hours; btc — часовые закрытия BTC
    [[открытие, мс, закрытие]]. «сгорела» — капитал ≤ 10% начала отрезка (ликвидация). Одна минута: сгорела > обвал BTC."""
    import numpy as np
    eq = np.asarray(eq, dtype=float)
    t_ms = np.asarray(t_ms, dtype=np.int64)
    rel = eq / eq[0]

    def first(mask):
        idx = np.flatnonzero(mask)
        return int(idx[0]) if idx.size else None

    ev = {"сгорела": first(rel <= 0.10)}
    if rules.get("btc_drop") and btc:
        hours = int(rules.get("btc_hours") or 12)
        closes = np.array([c for _, c in btc], dtype=float)
        known = np.array([o + 3_600_000 for o, _ in btc], dtype=np.int64)       # закрытие свечи известно в конце часа
        flag = np.zeros(len(closes), dtype=bool)
        for i in range(hours - 1, len(closes)):
            flag[i] = closes[i] <= (1 - rules["btc_drop"]) * closes[i - hours + 1:i + 1].max()
        pos = np.searchsorted(known, t_ms, side="right") - 1                    # последняя закрытая свеча на минуту
        ev["обвал BTC"] = first((pos >= 0) & flag[np.clip(pos, 0, None)])
    ev = {k: v for k, v in ev.items() if v is not None}
    if not ev:
        return None, None
    order = ["сгорела", "обвал BTC"]
    kind = min(ev, key=lambda k: (ev[k], order.index(k)))
    return ev[kind], kind


def btc_crash_at(btc, t_ms, drop, hours):
    """Обвал BTC в момент t_ms по последней закрытой часовой свече (как правило панели)."""
    known = [o + 3_600_000 for o, _ in btc]
    i = max((k for k, t in enumerate(known) if t <= t_ms), default=None)
    if i is None or i < hours - 1:
        return False
    window = [c for _, c in btc[i - hours + 1:i + 1]]
    return btc[i][1] <= (1 - drop) * max(window)


def run_paper_bets(s, start, end, stage_cb):
    """Бумажная торговля Turbo «как в жизни»: ставка с правилом панели — выход при обвале BTC и пауза до следующего дня.
    Каждый отрезок — прогон TB Core с капиталом по минутам; после обвала BTC следующий отрезок — с 00:00 UTC
    следующего дня на остаток счёта (если в 00:02 BTC ещё падает — ещё сутки). Стоп TB Core с перезапуском
    через 3 дня отрезок не обрывает: TB Core сам торгует дальше."""
    import pandas as pd
    rules = {"btc_drop": float(s.get("btc_exit") or 0), "btc_hours": int(s.get("btc_exit_hours") or 12),
             "hot_ratio": float(s.get("hot_ratio") or 0)}
    t0, t_end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    btc = kucoin_btc_closes(int((t0 - pd.Timedelta(days=2)).value // 1_000_000), int(t_end.value // 1_000_000)) \
        if rules["btc_drop"] else None
    closes = None
    if rules["hot_ratio"]:                  # фильтр разогнанных: дневные закрытия KuCoin с запасом в 30 дней до старта
        stage_cb("Цены монет для фильтра разогнанных…")
        closes = CG.fetch_closes(kucoin_session(), guard_coins(), int(t_end.value // 1_000_000),
                                 days=(t_end - t0).days + CG.HOT_DAYS + 2, pause=0.05)
    balance, d, bets, pieces, fills_all = float(s["balance"]), t0, [], [], []
    while d < t_end:
        stage_cb(f"Отрезок {len(bets) + 1}: с {d:%d.%m}…")
        hot = CG.hot(closes, rules["hot_ratio"], int(d.value // 1_000_000)) if closes else {}
        base = coins_on(d.strftime("%Y-%m-%d"))                   # список монет, действовавший на начало отрезка
        coins = [c for c in (base or MC.default_coins()) if c not in hot] if (hot or base) else None
        cfg = build_cfg(s, d.strftime("%Y-%m-%d"), end, "backtests/tb4/panel_paper", balance=balance, coins=coins)
        cfg["backtest"]["balance_sample_divider"] = 1                          # капитал поминутно — как видит панель
        run_dir = run_passivbot("paper", cfg, stage_cb)
        df = pd.read_csv(os.path.join(run_dir, "balance_and_equity.csv.gz"))
        tt = pd.to_datetime(df.iloc[:, 0], utc=True)
        keep = (tt < t_end).values          # свечей за сегодня нет: в 00:00 данные кончаются и TB Core сам закрывает всё
        df, tt = df[keep], tt[keep].reset_index(drop=True)
        if df.empty:
            break
        eq = df["usd_total_equity"].astype(float).values
        t_ms = (tt.astype("int64") // 1_000_000).values
        fills = pd.read_csv(os.path.join(run_dir, "fills.csv"))
        fills = fills[pd.to_datetime(fills["timestamp"], utc=True) < t_end].copy()
        i, kind = apply_bet_rules(t_ms, eq, rules, btc)
        last = len(eq) - 1 if i is None else i
        t_exit = tt.iloc[last]
        fills["t"] = pd.to_datetime(fills["timestamp"], utc=True)
        fills_all.append(fills[fills["t"] <= t_exit])
        pieces.append(pd.Series(eq[:last + 1], index=tt.iloc[:last + 1].values))
        bets.append({"start": d.strftime("%Y-%m-%d"), "end": t_exit.strftime("%Y-%m-%d %H:%M"),
                     "kind": kind or "идёт", "start_equity": round(float(eq[0]), 2), "final_equity": round(float(eq[last]), 2),
                     "result_pct": round((eq[last] / eq[0] - 1) * 100, 1),
                     "min_pct": round((float(eq[:last + 1].min()) / eq[0] - 1) * 100, 1),
                     "hot": {c: hot[c] for c in sorted(hot)}})
        if i is None or kind == "сгорела":
            break
        balance = float(eq[last])
        if balance < 1:
            break
        d = (t_exit + pd.Timedelta(days=1)).normalize()
        # пауза после обвала BTC: в 00:00 (+2 мин) BTC ещё падает → ещё сутки
        while d < t_end and btc_crash_at(btc, int(d.value // 1_000_000) + 120_000, rules["btc_drop"], rules["btc_hours"]):
            d += pd.Timedelta(days=1)
    if not pieces:
        raise UIError("Нет данных для бумажной торговли за этот период — попробуйте завтра.")
    curve = pd.concat(pieces)
    curve = curve[~curve.index.duplicated(keep="last")].sort_index()
    hourly = curve.resample("1h").last().ffill()
    start_eq, end_eq = float(s["balance"]), float(curve.iloc[-1])
    days = max(1, (t_end - t0).days)
    # месяц, в котором кривая начинается, считаем от настроечного баланса — иначе он не сходится
    # с «Итогом за период»: hourly-ресемплинг берёт последнюю точку первого часа, а не стартовый
    # капитал (при недельном периоде это расхождение давало +0,2 % против −0,3 %). Остальные месяцы —
    # от первой точки месяца, чтобы доходность оставалась цепочкой.
    monthly = {}
    for _m, _g in hourly.groupby(hourly.index.tz_localize(None).to_period("M")):
        _base = start_eq if _g.index[0] == hourly.index[0] else float(_g.iloc[0])
        monthly[str(_m)] = round((float(_g.iloc[-1]) / _base - 1) * 100, 1)
    fills = pd.concat(fills_all) if fills_all else pd.DataFrame()
    by_coin = []
    if len(fills):
        g = fills.groupby("coin").agg(pnl=("pnl", "sum"), fees=("fee_paid", "sum"), n=("pnl", "size"))
        by_coin = [{"coin": c, "pnl": round(float(r.pnl + r.fees), 2), "fills": int(r.n)}
                   for c, r in g.sort_values("pnl", ascending=False).iterrows()]
    return {"mode": "bets", "bets": bets, "rules": rules,
            "return_pct": round((end_eq / start_eq - 1) * 100, 1),
            "month_pct": (round(((end_eq / start_eq) ** (30 / days) - 1) * 100, 1) if end_eq > 0 else -100.0)
            if days >= 30 else None,                   # пересчёт пары дней на месяц только путает
            "max_dd_pct": round(float((1 - curve / curve.cummax()).max()) * 100, 1),
            "min_equity_pct": round((float(curve.min()) / start_eq - 1) * 100, 1),
            "best_30d_pct": None, "worst_30d_pct": None, "median_30d_pct": None, "share_30d_target": None,
            "share_30d_positive": None, "monthly": monthly, "curve": downsample(hourly),
            "start_equity": round(start_eq, 2), "end_equity": round(end_eq, 2), "fills": int(len(fills)), "by_coin": by_coin,
            "liquidated": any(b["kind"] == "сгорела" for b in bets), "start": start, "end": end, "days": days,
            "preset": s["preset"], "balance": s["balance"], "params": effective_params(s),
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M")}


def paper_refresh(force=False):
    pp = load_json(os.path.join(DATA, "paper.json"), None)
    if not pp:
        raise UIError("Бумажная торговля не запущена.")
    res = load_json(os.path.join(DATA, "paper_result.json"), None)
    if not force and res and res.get("end") == today():
        return False
    s = dict(settings(), **pp["settings"])
    if s.get("preset") not in presets():          # бумага начата на убранном пресете — считать на нынешнем
        if MODE == "bet":
            s["preset"] = settings()["preset"]
        else:
            s.update({k: v for k, v in settings().items() if k == "preset" or k in PRESET_RULES})
    start, end = pp["start"], today()
    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    if days < 1:
        raise UIError("Бумажная торговля началась сегодня — первые результаты появятся завтра.")
    # Режим ставки считает месяц сплошным прогоном (bet-логика со своим состоянием); режим main — либо тем же
    # прогоном с выходом при обвале BTC, либо обычным бэктестом, если правило выключено.
    if MODE == "bet":
        start_job("paper", lambda cb: run_paper_bets(s, start, end, cb))
    elif s.get("btc_exit"):
        start_job("paper", lambda cb: run_paper_guarded(s, start, end, cb))
    else:
        start_job("paper", lambda cb: run_backtest_job("paper", s, start, end, days, cb))
    return True


PAPER_AUTO = {"t": 0}


def paper_auto():
    """Бумага обновляется сама раз в сутки, пока панель открыта (на компьютере; на сервере бумаги нет)."""
    if LITE or JOB.get("state") == "running" or time.time() - PAPER_AUTO["t"] < 3600:
        return
    pp = load_json(os.path.join(DATA, "paper.json"), None)
    res = load_json(os.path.join(DATA, "paper_result.json"), None)
    if not pp or (res and res.get("end") == today()) or pp["start"] >= today():
        return
    PAPER_AUTO["t"] = time.time()
    paper_refresh()


def paper_start(balance):
    if LITE:
        raise UIError("На сервере бумажная торговля отключена (мало памяти). Запускайте её на компьютере.")
    s = settings()
    s["balance"] = float(balance or s["balance"])
    save_json(os.path.join(DATA, "paper.json"), {"start": today(), "settings": s,
                                                  "created": datetime.now().strftime("%Y-%m-%d %H:%M")})
    try:
        os.remove(os.path.join(DATA, "paper_result.json"))
    except OSError:
        pass


# ------------------------------------------------------------------ ключи
def keys_info():
    k = load_json(KEYS_PATH, {}).get(USER) or {}
    def mask(v):
        v = str(v or "")
        return (v[:4] + "…" + v[-3:]) if len(v) > 8 else ("задан" if v else "")
    return {"set": bool(k.get("key") and k.get("secret") and k.get("passphrase")),
            "key": mask(k.get("key")), "secret": "задан" if k.get("secret") else "",
            "passphrase": "задан" if k.get("passphrase") else ""}


def save_keys(body):
    data = load_json(KEYS_PATH, {})
    cur = data.get(USER) or {}
    new = {"exchange": "kucoin", "key": cur.get("key", ""), "secret": cur.get("secret", ""),
           "passphrase": cur.get("passphrase", "")}
    for f in ("key", "secret", "passphrase"):
        v = str(body.get(f) or "").strip()
        if v:
            new[f] = v
    data[USER] = new
    save_json(KEYS_PATH, data)
    if not proc.IS_WIN:
        os.chmod(KEYS_PATH, 0o600)   # ключи читает только владелец


def check_keys():
    """Только чтение: баланс фьючерсного счёта. Напрямую, мимо системного прокси (биржа видит ваш IP)."""
    import ccxt
    k = load_json(KEYS_PATH, {}).get(USER) or {}
    if not (k.get("key") and k.get("secret") and k.get("passphrase")):
        raise UIError("Сначала впишите все три значения: API Key, Secret и Passphrase.")
    ex = ccxt.kucoinfutures({"apiKey": k["key"], "secret": k["secret"], "password": k["passphrase"],
                             "enableRateLimit": True, "timeout": 15000})
    ex.session.trust_env = False
    try:
        bal = ex.fetch_balance()
    except Exception as exc:
        text = str(exc)
        if "400302" in text or "unavailable in the U.S" in text:
            text = ("KuCoin считает ваш IP запрещённым регионом (обычно так бывает, если запросы идут через VPN в "
                    "режиме TUN). Переключите VPN в режим прокси или добавьте kucoin.com в исключения.")
        elif "400003" in text or "KC-API-KEY" in text or "Invalid" in text:
            text = "KuCoin не принял ключ: проверьте Key, Secret и Passphrase и право Futures у ключа."
        raise UIError(text[:400])
    usdt = bal.get("USDT") or {}
    return {"total": usdt.get("total"), "free": usdt.get("free"), "checked": datetime.now().strftime("%H:%M:%S")}


# ------------------------------------------------------------------ реальная торговля
LIVE_STATE = os.path.join(DATA, "live.json")
LIVE_LOG = os.path.join(DATA, "live_console.log")


pid_alive = proc.pid_alive


def live_status():
    st = load_json(LIVE_STATE, {})
    running = pid_alive(st.get("pid"), st.get("ctime"))
    snap = load_json(MONITOR, None) if running or os.path.exists(MONITOR) else None
    if snap and running and st.get("ctime") and \
            ((snap.get("meta") or {}).get("current_cycle_ts_ms") or 0) < float(st["ctime"]) * 1000:
        snap = None               # снимок от прошлого запуска — ждём свежий, чтобы не показывать старые цифры
    out = {"running": running, "started": st.get("started"), "preset": st.get("preset"),
           "gave_up": st.get("resume_gave_up") if st.get("want_running") and not running else None,
           "btc_pause": st.get("btc_pause"), "mode": st.get("mode") or "normal"}
    if snap:
        acc = snap.get("account") or {}
        pos = []
        for sym, sides in (snap.get("positions") or {}).items():
            for side in ("long", "short"):
                x = (sides or {}).get(side) or {}
                size = float(x.get("size") or 0)
                if size:
                    pos.append({"symbol": sym, "side": side, "size": size, "price": x.get("price"),
                                "upnl": x.get("upnl"), "we": x.get("wallet_exposure")})
        out["snapshot"] = {"balance": acc.get("balance_raw"), "equity": acc.get("equity"), "positions": pos,
                           "orders": sum(len(v) for v in (snap.get("open_orders") or {}).values()),
                           "ts": (snap.get("meta") or {}).get("current_cycle_ts_ms"), "hsl": hsl_summary(snap)}
    return out


def hsl_summary(snap):
    """Состояние стопа TB Core (HSL) по лонгу из снимка: уровень, пауза после стопа, стоп без перезапуска.
    Стоп по монете (в снимке есть coins): mode "coin" и монеты не на зелёном уровне — оранжевый = без докупок,
    красный / halted = монета закрывается или закрыта на паузу; остальные монеты торгуют (как в панели основного бота)."""
    h = ((snap.get("hsl") or {}).get("long") or {})
    if not h:
        return None
    out = {k: h.get(k) for k in ("enabled", "tier", "halted", "no_restart_latched", "cooldown_until_ms")}
    if "coins" in h:
        out["mode"], out["coins"] = "coin", []
        for sym, c in sorted((h.get("coins") or {}).items()):
            if c.get("tier") in ("orange", "red") or c.get("halted"):
                m = c.get("last_metrics") or {}
                out["coins"].append({"coin": str(sym).split("/")[0], "tier": c.get("tier"), "halted": bool(c.get("halted")),
                                     "no_restart_latched": bool(c.get("no_restart_latched")),
                                     "cooldown_until_ms": c.get("cooldown_until_ms"), "loss": m.get("drawdown_usd"),
                                     "slot_pct": round(float(m["drawdown_usd"]) / float(m["slot_budget"]) * 100)
                                     if m.get("drawdown_usd") is not None and m.get("slot_budget") else None,
                                     "orange_pct": round(float(m["red_threshold"]) * float(m["tier_ratio_orange"]) * 100)
                                     if m.get("red_threshold") is not None and m.get("tier_ratio_orange") is not None else None})
    return out


BET = os.path.join(DATA, "bet.json")


def bet_settings(bet=None, st=None):
    """Настройки, с которыми ставка была запущена (снапшот в BET/LIVE_STATE): правки в панели во время ставки
    не должны менять параметры бота при его перезапусках (пауза после обвала BTC, делистинг, сбой).
    Ставки без снапшота (запущены прежней панелью) — по нынешним настройкам, как раньше."""
    snap = (bet or {}).get("settings") or (st or {}).get("settings")
    if snap:
        return dict(snap)
    return dict(settings(), preset=(bet or {}).get("preset") or (st or {}).get("preset") or settings()["preset"])


def st_settings(st=None):
    """То же для режима main: там состояние торговли лежит в LIVE_STATE, отдельного файла ставки нет."""
    return bet_settings(st=st)
# Переходы ставки и бота (запуск, остановка, фиксация, пауза после обвала BTC, автовосстановление) — по одному:
# иначе наблюдатель ставки мог записать устаревшую ставку поверх «фиксирую»/«остановлена» от кнопки в панели
# или Telegram, а возобновление после паузы — запустить бота, которого только что остановили. RLock: фиксация
# вызывается и из наблюдателя, который уже держит блокировку.
STATE_LOCK = threading.RLock()
BET_LOCK = STATE_LOCK
LOCK_TIMEOUT_S = 30 * 60   # сколько ждать закрытия позиций в режиме panic


def rules_clock_ms():
    """Часы правил панели — от них отсчитывается пауза после обвала BTC до 00:00 UTC следующего дня.
    На стенде репетиции — время сценария (deploy/sim/sim_panel.py)."""
    return now_ms()


bet_clock_ms = rules_clock_ms     # прежнее имя ветки bet


def now_ms():
    return int(time.time() * 1000)


def log_note(text):
    """Строка в журнал торговли. С новой строки, чтобы запись не слиплась с выводом бота."""
    with open(LIVE_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {text} =====\n")


log_line = log_note     # прежнее имя ветки bet


def cfg_path_for(tag):
    return os.path.join(ROOT, "configs", "tb4", f"{tag}.json")


def spawn_path(cfg_path, note=None):
    """Запуск бота по уже сохранённому конфигу. Строку в журнал пишет вызывающий (note)."""
    log = open(LIVE_LOG, "a", encoding="utf-8")
    if note:
        log.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {note} =====\n")
        log.flush()
    p = subprocess.Popen([PASSIVBOT, "live", cfg_path], cwd=ROOT, env=child_env(), stdout=log,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **proc.detached_kwargs())
    proc.set_oom_score(p.pid, 0)    # при нехватке памяти система закроет панель, а не торгующего бота
    return p.pid


def spawn_live(cfg, tag, note=None):
    """Запуск бота. cfg — конфиг (словарь; сохраняется как configs/tb4/<tag>.json) либо уже готовый путь к файлу,
    и тогда tag трактуется как строка в журнал. Ветки bet и main вызывали эту функцию по-разному."""
    if not isinstance(cfg, dict):
        return spawn_path(cfg, note or tag)
    cfg_path = cfg_path_for(tag)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    save_json(cfg_path, cfg)
    return spawn_path(cfg_path, note)


RESUME_MAX_PER_HOUR = 3


def live_resume():
    """После перезагрузки сервера или сбоя: если бот должен работать, а процесса нет — поднять его снова
    в том же режиме (обычный или panic во время фиксации ставки).
    Если бот падает сразу после запуска (ключи, биржа, конфиг) — не больше 3 попыток в час, дальше ждать человека."""
    with BET_LOCK:
        _live_resume()


def _pid_confirmed_dead(pid):
    """psutil прямо подтвердил, что процесса нет (NoSuchProcess), а не «не смог проверить».
    Разница важна: при сломанном psutil pid_alive ложно говорит «мёртв», и без проверки панель
    подняла бы второго бота на тот же субаккаунт."""
    try:
        import psutil
    except Exception:
        return False
    try:
        psutil.Process(int(pid or 0))
        return False                      # процесс есть — pid_alive ошибся
    except psutil.NoSuchProcess:
        return True
    except Exception:
        return False                      # psutil сломан — не знаем


def _live_resume():
    st = load_json(LIVE_STATE, {})
    if (not st.get("want_running") or st.get("resume_gave_up") or pid_alive(st.get("pid"), st.get("ctime"))
            or not os.path.exists(st.get("cfg_path") or "")):
        return
    if not _pid_confirmed_dead(st.get("pid")):
        # смерть не подтверждена (psutil сломан): бот пишет снимок монитора раз в 5 с — свежий снимок
        # значит, процесс жив и второй экземпляр на тот же субаккаунт запускать нельзя
        snap = load_json(MONITOR, None)
        if snap and now_ms() - (((snap.get("meta") or {}).get("current_cycle_ts_ms") or 0)) < 90_000:
            return
    now = time.time()
    recent = [t for t in st.get("resume_times", []) if now - t < 3600]
    if len(recent) >= RESUME_MAX_PER_HOUR:
        st["resume_gave_up"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_json(LIVE_STATE, st)
        log_line(f"бот падает сразу после запуска ({len(recent)} раза за час) — автоперезапуск остановлен. "
                 "Проверьте ключи и журнал, затем снова «Запустить реальную торговлю»")
        return
    log_line("автовосстановление: бот не работал — запускаю снова")
    st["pid"] = spawn_path(st["cfg_path"])
    st["ctime"] = proc.create_time(st["pid"])
    st["resumed"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    st["resume_times"] = recent + [now]
    save_json(LIVE_STATE, st)


def resume_watcher():
    while True:
        try:
            live_resume()
        except Exception:
            traceback.print_exc()
        try:                      # список монет — раз в месяц, 1-го числа
            coin_list_step()
        except Exception:
            traceback.print_exc()
        try:                      # защита от делистинга: KuCoin раз в 15 мин, закрытие монеты — сразу
            coin_guard_fetch()
            coin_guard_close_step()
        except Exception:
            traceback.print_exc()
        try:
            paper_auto()
        except Exception:
            traceback.print_exc()
        time.sleep(60)


def btc_move(hours):
    """Выход при обвале BTC: последнее закрытие часа BTC к максимуму закрытий за hours часов (часовые свечи KuCoin)."""
    import requests
    s = requests.Session()
    s.trust_env = False
    end = int(time.time())
    rows = s.get("https://api.kucoin.com/api/v1/market/candles",
                 params={"type": "1hour", "symbol": "BTC-USDT", "startAt": end - (hours + 3) * 3600, "endAt": end},
                 timeout=15).json()["data"]
    rows = sorted(rows, key=lambda r: int(r[0]))
    closes = [float(r[2]) for r in rows if int(r[0]) + 3600 <= end][-hours:]    # только закрытые часы
    if len(closes) < hours:
        raise UIError("Не удалось получить часовые свечи BTC для выхода при обвале — попробуйте позже.")
    return {"btc": closes[-1], "high": max(closes), "hours": hours,
            "drop_pct": round((closes[-1] / max(closes) - 1) * 100, 2), "checked": datetime.now().strftime("%Y-%m-%d %H:%M")}


BTC_MOVE_CACHE = {"t": 0, "v": None, "warned": False, "err_since": None, "last_alert": 0.0}
BTC_ALERT_EVERY_S = 900        # пока проверка обвала недоступна, напоминание в журнал (и Telegram) раз в 15 минут


def btc_move_cached(hours, ttl=60):
    """Раз в ttl секунд; при ошибке — {"error": …}: выход по BTC пропускается, стоп ставки продолжает работать.
    Во время ставки сбой пишется в журнал сразу и повторяется, пока проверка недоступна, — торговать молча
    без этой защиты нельзя. Восстановление — тоже в журнал."""
    c = BTC_MOVE_CACHE
    if time.time() - c["t"] > ttl or not c["v"] or c["v"].get("hours") != hours:
        try:
            c.update(t=time.time(), v=btc_move(hours), err_since=None)
            if c["warned"]:
                log_line("цена BTC для выхода при обвале снова доступна")
                c["warned"] = False
        except Exception as exc:
            now = time.time()
            if c["err_since"] is None:
                c["err_since"] = now
            c.update(t=now, v={"error": str(exc)[:200], "hours": hours})
            if load_json(BET, {}).get("state") in ("running", "btc_pause"):
                mins = int((now - c["err_since"]) // 60)
                if not c["warned"]:
                    log_line(f"не удалось получить цену BTC для выхода при обвале ({str(exc)[:80]}) — повторю через минуту")
                    c["warned"] = True
                    c["last_alert"] = now
                elif now - c["last_alert"] >= BTC_ALERT_EVERY_S:
                    log_line(f"выход при обвале BTC недоступен уже {mins} мин — торговля идёт без этой защиты, "
                             f"проверьте связь с KuCoin ({str(exc)[:80]})")
                    c["last_alert"] = now
    return c["v"]


def live_start(body):
    with BET_LOCK:
        _live_start(body)


def _live_start(body):
    if str(body.get("confirm", "")).strip().upper() != "ЗАПУСК":
        raise UIError("Для запуска реальной торговли впишите слово ЗАПУСК.")
    if MODE == "bet":     # у режима main состояния ставки нет — там та же пауза живёт в LIVE_STATE
        paused = load_json(BET, None) or {}
        if paused.get("state") == "btc_pause":
            raise UIError(f"Ставка на паузе после обвала BTC — бот запустится сам после "
                          f"{(paused.get('btc_pause') or {}).get('resume_after', '00:00 UTC')}, если BTC перестанет падать. "
                          f"Чтобы начать новую ставку, сначала нажмите «Завершить ставку».")
    if live_status()["running"]:
        raise UIError("Бот уже работает.")
    if not keys_info()["set"]:
        raise UIError("Сначала впишите ключи API KuCoin.")
    s = settings()
    drop, hours = float(s.get("btc_exit") or 0), int(s.get("btc_exit_hours") or 12)
    if drop:
        mv = btc_move_cached(hours, ttl=0)
        if mv and "drop_pct" in mv and mv["drop_pct"] <= -drop * 100:
            if MODE == "bet":
                raise UIError(f"BTC сейчас на {-mv['drop_pct']:.1f}% ниже максимума за {hours} ч — это и есть обвал, при котором "
                              f"бот закрывает позиции. Начните ставку, когда BTC перестанет падать (правило «выход при обвале BTC»).")
            raise UIError(f"BTC сейчас на {-mv['drop_pct']:.1f}% ниже максимума за {hours} ч — это обвал, при котором бот "
                          f"закрывает позиции. Запустите торговлю, когда BTC перестанет падать.")
    exp = float(effective_params(s)["exposure"])
    if exp > int(s.get("leverage") or 1):
        raise UIError(f"Экспозиция {exp:g} больше плеча на бирже ({s.get('leverage')}). "
                      f"Поставьте плечо не меньше {math.ceil(exp)} во вкладке «Настройки».")
    preset_name = presets()[s["preset"]].get("name", s["preset"])
    cfg = build_cfg(s, "2025-01-01", today(), "backtests/tb4/live")
    if MODE == "bet":
        log_line(f"запуск ставки, пресет «{preset_name}», без цели, "
                 f"выход при обвале BTC {'−%g%% за %d ч (пауза до следующего дня, ставка продолжается)' % (drop * 100, hours) if drop else 'выкл'}")
        pid = spawn_live(cfg, "live")
    else:
        pid = spawn_live(cfg, "live", f"запуск, пресет «{preset_name}»"
                         + (f", выход при обвале BTC −{drop * 100:g}% за {hours} ч" if drop else ""))
    # cfg_normal и btc_exit нужны наблюдателю режима main; у ставки их выставляет btc_pause_start,
    # но состояние держим одинаковым, чтобы файлы не расходились.
    save_json(LIVE_STATE, {"pid": pid, "ctime": proc.create_time(pid), "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "preset": s["preset"], "mode": "normal", "cfg_path": cfg_path_for("live"),
                           "cfg_normal": cfg_path_for("live"), "want_running": True,
                           "btc_exit": {"drop": drop, "hours": hours} if drop else None,
                           "settings": s})
    if MODE == "bet":
        save_json(BET, {"state": "waiting_start", "started_ms": now_ms(), "start_equity": None,
                        "btc_exit": {"drop": drop, "hours": hours} if drop else None,
                        "preset": s["preset"], "started": datetime.now().strftime("%Y-%m-%d %H:%M"), "settings": s})


def lock_profit(reason):
    """Фиксация ставки: перезапуск бота в режиме TB Core «panic» (закрыть позиции, новых не открывать)."""
    with BET_LOCK:
        bet = load_json(BET, None)
        if not bet or bet.get("state") in ("locking", "done", "stopped"):
            return
        bet.update(state="locking", lock_reason=reason, lock_started_ms=now_ms())
        save_json(BET, bet)
        log_line(f"фиксация: {reason} — закрываю позиции (режим panic)")
        live_stop()
        s = bet_settings(bet)
        pid = spawn_live(build_cfg(s, "2025-01-01", today(), "backtests/tb4/live", forced_mode="panic"), "live_panic")
        save_json(LIVE_STATE, {"pid": pid, "ctime": proc.create_time(pid), "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
                               "preset": s["preset"], "mode": "panic", "cfg_path": cfg_path_for("live_panic"),
                               "want_running": True, "settings": s})


def fresh_snapshot(since_ms):
    snap = load_json(MONITOR, None)
    ts = ((snap or {}).get("meta") or {}).get("current_cycle_ts_ms") or 0
    return snap if snap and ts >= since_ms else None


def open_positions(snap):
    n = 0
    for sides in (snap.get("positions") or {}).values():
        for side in ("long", "short"):
            if float(((sides or {}).get(side) or {}).get("size") or 0):
                n += 1
    return n


# ------------------------------------------------------------------ режим main: выход при обвале BTC
# Ветка основного бота. Состояние перехода живёт в LIVE_STATE (у ставки — в BET), потому в режиме main
# нет файла bet.json. Логика та же, что у ставки, но переводится в переход сессия, а не ставка.
def btc_crash_minutes(t_ms, btc, drop, hours):
    """Поминутный флаг «обвал BTC»: последнее закрытое часовое закрытие на drop ниже максимума закрытий за hours часов."""
    import numpy as np
    closes = np.array([c for _, c in btc], dtype=float)
    known = np.array([o + 3_600_000 for o, _ in btc], dtype=np.int64)          # закрытие свечи известно в конце часа
    flag = np.zeros(len(closes), dtype=bool)
    for i in range(hours - 1, len(closes)):
        flag[i] = closes[i] <= (1 - drop) * closes[i - hours + 1:i + 1].max()
    pos = np.searchsorted(known, np.asarray(t_ms, dtype=np.int64), side="right") - 1
    return (pos >= 0) & flag[np.clip(pos, 0, None)]


def run_paper_guarded(s, start, end, stage_cb):
    """Бумага с выходом при обвале BTC «как в жизни»: прогон TB Core до первого обвала, закрытие всего (худшая из 2 минут
    минус 0,1% капитала на единицу экспозиции), пауза до следующего дня (00:00 UTC) и, пока BTC падает, — дальше;
    новый прогон — на остаток капитала. Капитал поминутно."""
    import numpy as np
    import pandas as pd
    drop, hours = float(s.get("btc_exit") or 0), int(s.get("btc_exit_hours") or 12)
    cost = 0.001 * float(effective_params(s)["exposure"])
    t0, t_end = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    btc = kucoin_btc_closes(int((t0 - pd.Timedelta(days=2)).value // 1_000_000), int(t_end.value // 1_000_000))
    balance, d, pieces, fills_all, exits = float(s["balance"]), t0, [], [], []
    while d < t_end:
        if btc_crash_minutes([d.value // 1_000_000], btc, drop, hours)[0]:     # обвал ещё идёт — ждём следующий день
            d += pd.Timedelta(days=1)
            continue
        stage_cb(f"Прогон с {d:%d.%m}…")
        cfg = build_cfg(s, d.strftime("%Y-%m-%d"), end, "backtests/tb4/panel_paper", balance=balance,
                        coins=coins_on(d.strftime("%Y-%m-%d")))       # список монет, действовавший на начало отрезка
        cfg["backtest"]["balance_sample_divider"] = 1
        run_dir = run_passivbot("paper", cfg, stage_cb)
        df = pd.read_csv(os.path.join(run_dir, "balance_and_equity.csv.gz"))
        tt = pd.to_datetime(df.iloc[:, 0], utc=True)
        keep = (tt < t_end).values           # свечей за сегодня нет: в 00:00 данные кончаются и TB Core сам закрывает всё
        df, tt = df[keep], tt[keep].reset_index(drop=True)
        if df.empty:
            break
        eq = df["usd_total_equity"].astype(float).values
        flag = btc_crash_minutes((tt.astype("int64") // 1_000_000).values, btc, drop, hours)
        hit = np.flatnonzero(flag)
        i = int(hit[0]) if hit.size else None
        last = len(eq) - 1 if i is None else i
        fills = pd.read_csv(os.path.join(run_dir, "fills.csv"))
        fills = fills[pd.to_datetime(fills["timestamp"], utc=True) <= tt.iloc[last]]
        fills_all.append(fills)
        seg = pd.Series(eq[:last + 1], index=pd.DatetimeIndex(tt.iloc[:last + 1]))     # время UTC, как у паузы ниже
        if i is None:
            pieces.append(seg)
            break
        out = float(eq[i:i + 3].min()) * (1 - cost)
        seg.iloc[-1] = out
        pieces.append(seg)
        exits.append({"time": tt.iloc[i].strftime("%Y-%m-%d %H:%M"), "equity": round(out, 2)})
        balance = out
        d = (tt.iloc[i] + pd.Timedelta(days=1)).normalize()
        pieces.append(pd.Series([out], index=[d]))                  # без позиций до следующего запуска
    if not pieces:
        raise UIError("Нет данных для бумажной торговли за этот период — попробуйте завтра.")
    curve = pd.concat(pieces)
    curve = curve[~curve.index.duplicated(keep="last")].sort_index()
    hourly = curve.resample("1h").last().ffill()
    start_eq, end_eq = float(s["balance"]), float(curve.iloc[-1])
    days = max(1, (t_end - t0).days)
    # месяц, в котором кривая начинается, считаем от настроечного баланса — иначе он не сходится
    # с «Итогом за период»: hourly-ресемплинг берёт последнюю точку первого часа, а не стартовый
    # капитал (при недельном периоде это расхождение давало +0,2 % против −0,3 %). Остальные месяцы —
    # от первой точки месяца, чтобы доходность оставалась цепочкой.
    monthly = {}
    for _m, _g in hourly.groupby(hourly.index.tz_localize(None).to_period("M")):
        _base = start_eq if _g.index[0] == hourly.index[0] else float(_g.iloc[0])
        monthly[str(_m)] = round((float(_g.iloc[-1]) / _base - 1) * 100, 1)
    fills = pd.concat(fills_all) if fills_all else pd.DataFrame()
    by_coin = []
    if len(fills):
        g = fills.groupby("coin").agg(pnl=("pnl", "sum"), fees=("fee_paid", "sum"), n=("pnl", "size"))
        by_coin = [{"coin": c, "pnl": round(float(r.pnl + r.fees), 2), "fills": int(r.n)}
                   for c, r in g.sort_values("pnl", ascending=False).iterrows()]
    return {"mode": "btc_guard", "btc_exits": exits, "return_pct": round((end_eq / start_eq - 1) * 100, 1),
            "month_pct": (round(((end_eq / start_eq) ** (30 / days) - 1) * 100, 1) if end_eq > 0 else -100.0)
            if days >= 30 else None,
            "max_dd_pct": round(float((1 - curve / curve.cummax()).max()) * 100, 1),
            "min_equity_pct": round((float(curve.min()) / start_eq - 1) * 100, 1),
            "best_30d_pct": None, "worst_30d_pct": None, "median_30d_pct": None, "share_30d_target": None,
            "share_30d_positive": None, "monthly": monthly, "curve": downsample(hourly),
            "start_equity": round(start_eq, 2), "end_equity": round(end_eq, 2), "fills": int(len(fills)), "by_coin": by_coin,
            "liquidated": end_eq < start_eq * 0.1, "start": start, "end": end, "days": days,
            "preset": s["preset"], "balance": s["balance"], "params": effective_params(s),
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M")}


def btc_guard_step():
    with STATE_LOCK:
        _btc_guard_step()


def _btc_guard_step():
    """Обвал BTC → закрыть всё (TB Core в режиме panic) → позиции закрыты: пауза → в 00:00 UTC следующего дня, если BTC
    перестал падать, — снова обычная торговля, иначе пауза до следующих суток (как в tb4/main_strategy.py).
    Состояние — в LIVE_STATE (переживает перезапуск панели)."""
    st = load_json(LIVE_STATE, {})
    bx, pause = st.get("btc_exit"), st.get("btc_pause")
    if pause and pause.get("state") == "closing":
        if not pid_alive(st.get("pid"), st.get("ctime")):         # панель перезапустилась посреди перехода — поднять снова
            pid = spawn_live(st["cfg_path"], "live_panic")
            st.update(pid=pid, ctime=proc.create_time(pid), want_running=True)
            save_json(LIVE_STATE, st)
            return
        snap = fresh_snapshot(pause["since_ms"] + 30_000)
        if snap and open_positions(snap) == 0:
            eq = (snap.get("account") or {}).get("equity")
            pause.update(state="waiting", closed=datetime.now().strftime("%Y-%m-%d %H:%M"), equity=eq)
            st.update(want_running=False, btc_pause=pause)        # пауза по плану — не «бот упал» (до остановки)
            save_json(LIVE_STATE, st)
            proc.stop_tree(st.get("pid"), ctime=st.get("ctime"))
            log_note(f"после обвала BTC позиции закрыты{', капитал %.2f USDT' % float(eq) if eq else ''} — пауза, "
                     f"торговля продолжится сама после {fmt_ms(pause['resume_ms'])}, если BTC перестанет падать")
        elif now_ms() - pause["since_ms"] > LOCK_TIMEOUT_S * 1000 and not pause.get("warned"):
            pause["warned"] = True
            save_json(LIVE_STATE, dict(st, btc_pause=pause))
            log_note("позиции не закрылись за 30 минут после обвала BTC — проверьте KuCoin и закройте вручную")
        return
    if pause and pause.get("state") == "waiting":
        if rules_clock_ms() < pause["resume_ms"] + 120_000 or not bx:
            return                       # +2 мин: свеча BTC, закрывшаяся в 00:00, должна появиться на KuCoin
        mv = btc_move_cached(int(bx["hours"]), ttl=0)            # решение — по свежей цене, не из кэша
        if not mv or "drop_pct" not in mv:
            return                                               # нет цены BTC — проверю через минуту
        if mv["drop_pct"] <= -bx["drop"] * 100:                  # в 00:00 обвал ещё идёт — этот день пропускаем
            pause.update(resume_ms=next_day_ms(pause["resume_ms"]), resume_after=fmt_ms(next_day_ms(pause["resume_ms"])))
            save_json(LIVE_STATE, dict(st, btc_pause=pause))
            log_note(f"BTC всё ещё падает ({mv['drop_pct']:+.1f}% за {bx['hours']} ч) — пауза продлена до {pause['resume_after']}")
            return
        pid = spawn_live(st.get("cfg_normal") or st["cfg_path"], "возобновление после обвала BTC")
        st.update(pid=pid, ctime=proc.create_time(pid), want_running=True,
                  cfg_path=st.get("cfg_normal") or st["cfg_path"], mode="normal",
                  btc_pause=None, resume_times=[], resumed=datetime.now().strftime("%Y-%m-%d %H:%M"))
        save_json(LIVE_STATE, st)
        return
    if not bx or not st.get("want_running") or (st.get("mode") or "normal") != "normal" \
            or not pid_alive(st.get("pid"), st.get("ctime")):
        return
    mv = btc_move_cached(int(bx["hours"]))
    if not (mv and "drop_pct" in mv and mv["drop_pct"] <= -bx["drop"] * 100):
        return
    since, resume = now_ms(), next_day_ms(rules_clock_ms())
    log_note(f"обвал BTC: {mv['drop_pct']:+.1f}% от максимума за {bx['hours']} ч — закрываю все позиции (режим panic), "
             f"торговля продолжится после паузы")
    s = st_settings(st)
    path = cfg_path_for("live_panic")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, build_cfg(s, "2025-01-01", today(), "backtests/tb4/live", forced_mode="panic"))   # заранее
    old_pid, old_ctime = st.get("pid"), st.get("ctime")
    st.update(want_running=False, cfg_path=path, cfg_normal=st.get("cfg_normal") or st.get("cfg_path"), mode="btc_panic",
              btc_pause={"state": "closing", "since_ms": since, "since": fmt_ms(since), "drop_pct": mv["drop_pct"],
                         "resume_ms": resume, "resume_after": fmt_ms(resume)})
    save_json(LIVE_STATE, st)          # на время перехода — не «должен работать», иначе отчёт увидит «процесса нет»
    proc.stop_tree(old_pid, ctime=old_ctime)
    pid = spawn_live(path, "live_panic")
    st.update(pid=pid, ctime=proc.create_time(pid), want_running=True)
    save_json(LIVE_STATE, st)


def btc_watcher():
    while True:
        time.sleep(15)
        try:
            btc_guard_step()
        except Exception:  # наблюдатель не должен падать
            traceback.print_exc()


def bet_watcher():
    """Раз в 15 с: запоминает капитал на старте ставки, следит за обвалом BTC (пауза) и за фиксацией (после закрытия
    позиций останавливает бота)."""
    while True:
        time.sleep(15)
        try:
            bet_step()
        except Exception:  # наблюдатель не должен падать
            traceback.print_exc()


def bet_step():
    bet = load_json(BET, None)
    if not bet or bet.get("state") in ("done", "stopped"):
        return
    # цена BTC — до блокировки: сетевой запрос (до 15 с) не должен задерживать кнопки панели и Telegram
    bx, pause, mv = bet.get("btc_exit") or {}, bet.get("btc_pause") or {}, None
    if bx and bet["state"] == "running":
        st = load_json(LIVE_STATE, {})
        if pid_alive(st.get("pid"), st.get("ctime")):
            mv = btc_move_cached(int(bx["hours"]))
    elif bet["state"] == "btc_pause" and pause.get("state") == "waiting" and bet_clock_ms() >= pause["resume_ms"] + 120_000:
        mv = btc_move_cached(int(bx.get("hours") or 12), ttl=0)       # решение о возобновлении — по свежей цене
    with BET_LOCK:            # за это время кнопка в панели или Telegram могла зафиксировать или остановить ставку
        cur = load_json(BET, None)
        if cur and cur.get("started_ms") == bet.get("started_ms") and cur.get("state") == bet.get("state"):
            _bet_step(cur, mv)


def _bet_step(bet, mv):
    if bet["state"] == "btc_pause":
        btc_pause_step(bet, mv)
        return
    st = load_json(LIVE_STATE, {})
    if not pid_alive(st.get("pid"), st.get("ctime")):
        return
    snap = fresh_snapshot(max(bet.get("started_ms", 0), bet.get("resumed_ms") or 0))
    if not snap:
        return
    eq = float((snap.get("account") or {}).get("equity") or 0)
    if eq <= 0:
        return
    if bet["state"] == "waiting_start":
        bet.update(state="running", start_equity=eq, peak_equity=eq, last_equity=eq)
        save_json(BET, bet)
        log_line(f"капитал на старте ставки: {eq:.2f} USDT, без цели")
    elif bet["state"] == "running":
        bet.update(last_equity=eq, peak_equity=max(eq, bet.get("peak_equity") or eq))
        save_json(BET, bet)
        bx = bet.get("btc_exit")
        if bx and mv and "drop_pct" in mv and mv["drop_pct"] <= -bx["drop"] * 100:
            btc_pause_start(bet, mv, eq)
    elif bet["state"] == "locking":
        after = fresh_snapshot(bet.get("lock_started_ms", 0) + 30_000)
        if after and open_positions(after) == 0:
            bet.update(state="done", final_equity=float((after.get("account") or {}).get("equity") or eq),
                       finished=datetime.now().strftime("%Y-%m-%d %H:%M"))
            save_json(BET, bet)       # сначала итог, потом остановка
            live_stop()
            log_line(f"ставка закрыта: {bet['final_equity']:.2f} USDT, бот остановлен")
        elif now_ms() - bet.get("lock_started_ms", 0) > LOCK_TIMEOUT_S * 1000 and not bet.get("lock_warned"):
            bet["lock_warned"] = True
            save_json(BET, bet)
            log_line("позиции не закрылись за 30 минут — проверьте KuCoin и закройте вручную")


def next_day_ms(ms):
    """00:00 UTC следующего дня."""
    d = datetime.fromtimestamp(ms / 1000, timezone.utc) + timedelta(days=1)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def fmt_ms(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M")


def btc_pause_start(bet, mv, eq):
    """Обвал BTC при правиле «пауза» (Turbo 5.0): закрыть всё (TB Core в режиме panic), ставка не заканчивается."""
    bx = bet["btc_exit"]
    since, resume = now_ms(), next_day_ms(bet_clock_ms())
    s = bet_settings(bet)
    cfg = build_cfg(s, "2025-01-01", today(), "backtests/tb4/live", forced_mode="panic")      # заранее, до остановки
    pause = {"state": "closing", "since_ms": since, "since": fmt_ms(since), "drop_pct": mv["drop_pct"],
             "resume_ms": resume, "resume_after": fmt_ms(resume)}
    with BET_LOCK:
        bet.update(state="btc_pause", btc_pause=pause, pauses=int(bet.get("pauses") or 0) + 1, last_equity=eq)
        save_json(BET, bet)
    start = bet.get("start_equity") or eq
    log_line(f"обвал BTC: {mv['drop_pct']:+.1f}% от максимума за {bx['hours']} ч — закрываю все позиции (режим panic), "
             f"капитал {eq:.2f} USDT (×{eq / start:.2f}); ставка продолжится после паузы, не раньше {pause['resume_after']}")
    st = load_json(LIVE_STATE, {})
    old_pid, old_ctime = st.get("pid"), st.get("ctime")
    st.update(want_running=False, mode="btc_panic", btc_pause=pause, cfg_normal=st.get("cfg_normal") or st.get("cfg_path"))
    save_json(LIVE_STATE, st)          # на время перехода — не «должен работать», иначе отчёт увидит «процесса нет»
    proc.stop_tree(old_pid, ctime=old_ctime)
    pid = spawn_live(cfg, "live_panic")
    st.update(pid=pid, ctime=proc.create_time(pid), want_running=True, cfg_path=cfg_path_for("live_panic"),
              started=datetime.now().strftime("%Y-%m-%d %H:%M"))
    save_json(LIVE_STATE, st)


def btc_pause_step(bet, mv=None):
    """Пауза после обвала BTC: позиции закрылись → бот остановлен до 00:00 UTC следующего дня; тогда (+2 мин, свежая цена)
    BTC ещё падает — пауза ещё на сутки, иначе обычная торговля в той же ставке. Состояние — в BET и LIVE_STATE."""
    st = load_json(LIVE_STATE, {})
    pause, bx = bet["btc_pause"], bet.get("btc_exit") or {}
    if pause.get("state") == "closing":
        if not pid_alive(st.get("pid"), st.get("ctime")):         # панель перезапустилась посреди перехода — поднять снова
            pid = spawn_path(cfg_path_for("live_panic"))
            st.update(pid=pid, ctime=proc.create_time(pid), want_running=True, cfg_path=cfg_path_for("live_panic"),
                      mode="btc_panic", btc_pause=pause)
            save_json(LIVE_STATE, st)
            return
        snap = fresh_snapshot(pause["since_ms"] + 30_000)
        if snap and open_positions(snap) == 0:
            eq = float((snap.get("account") or {}).get("equity") or 0) or None
            pause.update(state="waiting", closed=datetime.now().strftime("%Y-%m-%d %H:%M"), equity=eq)
            with BET_LOCK:
                bet.update(btc_pause=pause, last_equity=eq or bet.get("last_equity"))
                save_json(BET, bet)
            st.update(want_running=False, btc_pause=pause)        # пауза по плану — не «бот упал» (до остановки)
            save_json(LIVE_STATE, st)
            proc.stop_tree(st.get("pid"), ctime=st.get("ctime"))
            log_line(f"после обвала BTC позиции закрыты{', капитал %.2f USDT' % eq if eq else ''} — пауза, "
                     f"ставка продолжится сама после {pause['resume_after']}, если BTC перестанет падать")
        elif now_ms() - pause["since_ms"] > LOCK_TIMEOUT_S * 1000 and not pause.get("warned"):
            pause["warned"] = True
            with BET_LOCK:
                bet["btc_pause"] = pause
                save_json(BET, bet)
            log_line("позиции не закрылись за 30 минут после обвала BTC — проверьте KuCoin и закройте вручную")
        return
    if bet_clock_ms() < pause["resume_ms"] + 120_000:
        return                           # +2 мин: свеча BTC, закрывшаяся в 00:00, должна появиться на KuCoin
    if not mv or "drop_pct" not in mv:
        return                           # нет свежей цены BTC (запрос — в bet_step, до блокировки) — проверю позже
    if mv["drop_pct"] <= -float(bx.get("drop") or 0.05) * 100:    # в 00:00 обвал ещё идёт — этот день пропускаем
        nxt = next_day_ms(pause["resume_ms"])
        pause.update(resume_ms=nxt, resume_after=fmt_ms(nxt))
        with BET_LOCK:
            bet["btc_pause"] = pause
            save_json(BET, bet)
        save_json(LIVE_STATE, dict(st, btc_pause=pause))
        log_line(f"BTC всё ещё падает ({mv['drop_pct']:+.1f}% за {bx.get('hours')} ч) — пауза продлена до {pause['resume_after']}")
        return
    cfg_normal = st.get("cfg_normal") or cfg_path_for("live")
    log_line("пауза после обвала BTC закончилась — торговля возобновлена")
    pid = spawn_path(cfg_normal)
    st.update(pid=pid, ctime=proc.create_time(pid), want_running=True, cfg_path=cfg_normal, mode="normal", btc_pause=None,
              resume_times=[], started=datetime.now().strftime("%Y-%m-%d %H:%M"))
    save_json(LIVE_STATE, st)
    with BET_LOCK:
        bet.update(state="running", btc_pause=None, resumed_ms=now_ms())
        save_json(BET, bet)


def live_stop_manual():
    """«Остановить» в панели или из Telegram: ставка заканчивается («остановлена») — во время паузы после обвала BTC
    иначе панель сама запустила бы бота снова в 00:00, а во время торговли отчёт и Telegram показывали бы «ставка
    идёт» при остановленном боте. Позиции и ордера на бирже остаются (так и предупреждает кнопка)."""
    with BET_LOCK:
        bet = load_json(BET, None)
        if bet and bet.get("state") == "btc_pause":
            p = bet.get("btc_pause") or {}
            bet.update(state="stopped", final_equity=p.get("equity") or bet.get("last_equity"), btc_pause=None,
                       finished=datetime.now().strftime("%Y-%m-%d %H:%M"))
            save_json(BET, bet)
            log_line("ставка остановлена вручную во время паузы после обвала BTC")
        elif bet and bet.get("state") in ("running", "waiting_start"):
            bet.update(state="stopped", final_equity=bet.get("last_equity"), finished=datetime.now().strftime("%Y-%m-%d %H:%M"))
            save_json(BET, bet)
            log_line("ставка остановлена вручную: бот остановлен, открытые позиции и ордера на бирже остались")
        st = load_json(LIVE_STATE, {})
        if st.get("btc_pause"):
            st["btc_pause"] = None
            save_json(LIVE_STATE, st)
        live_stop()


def bet_reset():
    with BET_LOCK:
        if live_status()["running"]:
            raise UIError("Сначала остановите бота.")
        if (load_json(BET, None) or {}).get("state") == "btc_pause":
            raise UIError("Ставка на паузе после обвала BTC — сначала нажмите «Завершить ставку».")
        try:
            os.remove(BET)
        except OSError:
            pass


def live_stop():
    """Остановка по кнопке: бот больше не нужен — после перезапуска сервера не поднимать. Пауза после обвала BTC
    отменяется: сам он больше не запустится."""
    with STATE_LOCK:
        st = load_json(LIVE_STATE, {})
        if st:
            st.update(want_running=False, btc_pause=None, mode="normal")
            save_json(LIVE_STATE, st)
        proc.stop_tree(st.get("pid"), ctime=st.get("ctime"))


# ------------------------------------------------------------------ список монет раз в месяц (kucoin_refresh.py)
COINLIST = os.path.join(DATA, "coin_list.json")          # когда обновлён список монет и что изменилось
APPROVED = os.path.join(DATA, "approved_coins.json")     # монеты для TB Core (live.approved_coins — файл, читается на ходу)
COINLIST_RETRY_S = 3600
COINLIST_HISTORY = os.path.join(DATA, "coin_list_history.json")   # {«ГГГГ-ММ»: монеты} — для бумаги «как было тогда»
TESTED = os.path.join(DATA, "tested_coins.json")         # {"coins": [...]} — монеты, прогнанные в бэктесте; живой бот торгует только их


def remember_coins(month, coins):
    h = load_json(COINLIST_HISTORY, {}) or {}
    if h.get(month) != coins:
        h[month] = coins
        save_json(COINLIST_HISTORY, h)


def coins_on(day):
    """Список монет, действовавший в день day («ГГГГ-ММ-ДД»): последний записанный не позже этого месяца; раньше первой
    записи — самый ранний; истории нет — None (нынешний справочник). Бумага берёт его на начало каждого отрезка, чтобы
    обновление 1-го числа не пересчитывало прошлые дни по новому списку."""
    h = load_json(COINLIST_HISTORY, {}) or {}
    if not h:
        return None
    past = [k for k in sorted(h) if k <= day[:7]]
    return h[past[-1] if past else sorted(h)[0]]


def write_approved():
    """Монеты бота: справочник по обороту, урезанный до проверенных бэктестом (TESTED), если такой список есть. Монеты
    вне бэктеста дали весь убыток 04–19.10.2026 — без проверки на истории их в живую торговлю не пускаем."""
    coins = MC.default_coins()
    tested = (load_json(TESTED, {}) or {}).get("coins")
    if tested:
        coins = [c for c in coins if c in set(tested)]
    save_json(APPROVED, {"long": coins, "short": coins})
    return coins


def coin_list_step(now=None):
    """1-го числа каждого месяца (после 00:10 UTC) — справочник монет KuCoin заново по обороту за сутки (как в честной
    проверке 02.10.2026: список по месяцам по обороту того времени), монеты для TB Core — в файл, который он перечитывает
    на ходу. По убранным монетам бот ведёт открытые позиции как обычно, новых входов нет. KuCoin / Binance не ответили
    или монет стало подозрительно мало — прежний список, новая попытка через час."""
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    st = load_json(COINLIST, None)
    if st is None:                  # первый запуск: нынешний справочник — список этого месяца
        save_json(COINLIST, {"month": month, "updated": None, "count": len(MC.default_coins()), "added": [], "removed": [],
                             "error": None})
        remember_coins(month, MC.default_coins())
        return
    if not os.path.exists(COINLIST_HISTORY):        # панель, запущенная до появления истории: записать нынешний список
        remember_coins(st.get("month") or month, MC.default_coins())
    if st.get("month") == month or (now.day == 1 and now.hour == 0 and now.minute < 10) \
            or (st.get("tried_month") == month and time.time() - float(st.get("tried") or 0) < COINLIST_RETRY_S):
        return                      # этот месяц уже обновлён / первые минуты суток / попытка была меньше часа назад
    st.update(tried=time.time(), tried_month=month)
    save_json(COINLIST, st)         # попытка записана сразу: неожиданный сбой не превратится в запрос раз в минуту
    old = MC.default_coins()
    import requests
    sess = requests.Session()
    sess.trust_env = False
    try:
        rows = KR.fetch_rows(sess)
        new = [r[0] for r in rows if r[1]["turnover24h"] >= MC.MIN_TURNOVER]
        if len(new) < max(30, len(old) // 2):
            raise RuntimeError(f"монет с оборотом от {MC.MIN_TURNOVER / 1e6:g} млн $ всего {len(new)} (было {len(old)})")
    except Exception as exc:
        if not st.get("error"):
            log_line(f"список монет: обновить не удалось ({str(exc)[:100]}) — торгуем прежним, повторю через час")
        st["error"] = str(exc)[:200]
        save_json(COINLIST, st)
        return
    KR.write(rows, TB4)
    try:          # защита (делистинг, разогнанные монеты) — по новому списку ДО того, как TB Core увидит его файл ниже
        coin_guard_fetch(force=True)
    except Exception:
        traceback.print_exc()
    coins = write_approved()
    remember_coins(month, coins)
    added, removed = sorted(set(coins) - set(old)), sorted(set(old) - set(coins))
    log_line(f"список монет обновлён (раз в месяц): {len(old)} → {len(coins)}"
             + (f"; добавлены: {', '.join(added)}" if added else "")
             + (f"; убраны: {', '.join(removed)} — открытые позиции по ним бот ведёт как обычно, новых входов нет"
                if removed else ""))
    save_json(COINLIST, {"month": month, "updated": now.astimezone().strftime("%Y-%m-%d %H:%M"), "count": len(coins),
                         "added": added, "removed": removed, "error": None, "tried": st["tried"], "tried_month": month})


# ------------------------------------------------------------------ защита от делистинга (coin_guard.py)
GUARD = os.path.join(DATA, "coin_guard.json")                  # итог проверки KuCoin: монеты под защитой
GUARD_IGNORED = os.path.join(DATA, "coin_guard_ignored.json")  # их список для TB Core (live.ignored_coins)
GUARD_EVERY_S, GUARD_SPOT_EVERY_S = 900, 3600                  # контракты и объявления / метки ST
HOT_EVERY_S = 3600                                             # дневные закрытия для фильтра разогнанных монет
HOT_CLOSES = os.path.join(DATA, "coin_guard_closes.json")      # последние закрытия (после перезапуска панели — сразу)
GUARD_CACHE = {"t": 0, "spot_t": 0, "st": None, "warned": False, "hot_t": 0, "closes": {}, "hot_busy": False}


def hot_refresh(coins):
    """Дневные закрытия KuCoin для фильтра разогнанных (84 запроса, ~1 мин) → кэш и файл. KuCoin ответил не по всем
    монетам — остаются прежние цены, чтобы монеты не «качались» между «не входить» и «снова в работе»."""
    c = GUARD_CACHE
    try:
        closes = CG.fetch_closes(kucoin_session(), coins, now_ms())
        if len(closes) >= 0.8 * len(coins):
            c.update(closes=closes, hot_t=time.time())
            save_json(HOT_CLOSES, {"t": c["hot_t"], "closes": closes})
    finally:
        c["hot_busy"] = False


def hot_closes(coins, force):
    """Закрытия для фильтра: из памяти / файла; устарели (> часа) — обновить в фоне (первый раз и force — сразу)."""
    c = GUARD_CACHE
    if not c["closes"]:
        saved = load_json(HOT_CLOSES, {}) or {}
        c.update(closes=saved.get("closes") or {}, hot_t=float(saved.get("t") or 0))
    if force or not c["closes"]:
        hot_refresh(coins)
    elif time.time() - c["hot_t"] >= HOT_EVERY_S and not c["hot_busy"]:
        c["hot_busy"] = True
        threading.Thread(target=hot_refresh, args=(coins,), daemon=True).start()
    return c["closes"]


def guard_coins():
    """Монеты бота → символ фьючерса KuCoin (те же, что в конфиге: tb4/kucoin_coins.json, оборот от MC.MIN_TURNOVER)."""
    ku = load_json(os.path.join(TB4, "kucoin_coins.json"), {"kucoin": {}})
    return {c: ku["kucoin"][c]["symbol"] for c in MC.default_coins() if c in ku["kucoin"]}


def guard_write_ignored(coins):
    save_json(GUARD_IGNORED, {"long": sorted(coins), "short": sorted(coins)})


def guard_cfg(cfg):
    """Боевой конфиг: монеты под защитой не торговать (файл — TB Core перечитывает его на ходу), монеты с назначенным
    делистингом — закрыть (режим panic только для них, остальные торгуют)."""
    g = (load_json(GUARD, {}) or {}).get("coins") or {}
    if not os.path.exists(GUARD_IGNORED):
        guard_write_ignored(g)
    cfg["live"]["ignored_coins"] = GUARD_IGNORED
    write_approved()                                    # монеты бота — тоже файлом: раз в месяц список обновляется на ходу
    cfg["live"]["approved_coins"] = APPROVED
    close = sorted(c for c, x in g.items() if x.get("level") == "close")
    if close:
        cfg["coin_overrides"] = dict(cfg.get("coin_overrides") or {},
                                     **{c: {"live": {"forced_mode_long": "panic"}} for c in close})
    return cfg


def coin_guard_fetch(force=False):
    """Раз в 15 мин (метки ST — раз в час): данные KuCoin → GUARD и файл ignored_coins. Изменения пишутся в журнал бота
    (оттуда — в Telegram), только пока идёт ставка (в т.ч. пауза после обвала BTC)."""
    c = GUARD_CACHE
    if not force and time.time() - c["t"] < GUARD_EVERY_S:
        return
    import requests
    sess = requests.Session()
    sess.trust_env = False
    c["t"] = time.time()
    g = load_json(GUARD, {}) or {}
    st = load_json(LIVE_STATE, {})
    loud = bool(st.get("want_running") or (load_json(BET, None) or {}).get("state") == "btc_pause")
    try:
        spot = force or time.time() - c["spot_t"] >= GUARD_SPOT_EVERY_S
        data = CG.fetch(sess, spot=spot)
        if spot:
            c.update(spot_t=time.time(), st=data["st"])
        else:
            data["st"] = c["st"]
        ratio = float(settings().get("hot_ratio") or 0)
        coins = guard_coins()
        hot = CG.hot(hot_closes(coins, force), ratio, now_ms()) if ratio else {}
        new = CG.assess(coins, data, g.get("coins"), now_ms(), hot_coins=hot, ratio=ratio)
    except Exception as exc:
        if loud and not c["warned"]:
            log_line(f"защита от делистинга: не удалось получить данные KuCoin ({str(exc)[:80]}) — повторю через 15 минут")
        c["warned"] = True
        save_json(GUARD, dict(g, error=str(exc)[:200]))
        return
    c["warned"] = False
    old = g.get("coins") or {}
    if loud:
        for coin, x in sorted(new.items()):
            o = old.get(coin) or {}
            if x.get("hot") and not o.get("hot"):          # фильтр разогнанных — без тревоги в Telegram
                log_line(f"фильтр разогнанных монет: {coin} — {x['reason']} (открытую позицию бот ведёт как обычно)")
            elif not x.get("hot") and o.get("level") != x["level"]:
                log_line(f"защита от делистинга: {coin} — {x['reason']}. Бот больше не входит в {coin}"
                         + (", открытую позицию по ней закроет" if x["level"] == "close" else ""))
        for coin in sorted((set(old) - set(new)) & set(coins)):     # убранные из списка монет — не «снова в работе»
            log_line(f"фильтр разогнанных монет: {coin} снова в работе" if old[coin].get("hot")
                     else f"защита от делистинга: {coin} снова в работе — причина снята")
    g.update(coins=new, checked=datetime.now().strftime("%Y-%m-%d %H:%M"), error=None, hot_ratio=ratio or 0,
             hot_checked=datetime.fromtimestamp(c["hot_t"]).strftime("%Y-%m-%d %H:%M") if c["hot_t"] else None)
    save_json(GUARD, g)
    guard_write_ignored(new)
    if {k for k, x in old.items() if x["level"] == "close"} != {k for k, x in new.items() if x["level"] == "close"}:
        guard_refresh_saved_cfg(st)


def guard_refresh_saved_cfg(st):
    """Сохранённый обычный конфиг (по нему бот поднимается после паузы и сбоя) — с нынешним списком закрываемых монет:
    монета, которую KuCoin уже убрал, не должна остаться в coin_overrides."""
    path = st.get("cfg_normal") or cfg_path_for("live")
    if not os.path.exists(path) or (st.get("mode") or "normal") != "normal":
        return
    s = bet_settings(st=st)
    save_json(path, build_cfg(s, "2025-01-01", today(), "backtests/tb4/live"))


def held_coins(snap):
    return {str(sym).split("/")[0] for sym, sides in (snap.get("positions") or {}).items()
            if float(((sides or {}).get("long") or {}).get("size") or 0)}


def coin_guard_close_step():
    with BET_LOCK:
        _coin_guard_close_step()


def _coin_guard_close_step():
    """Назначен делистинг, а по монете открыта позиция → перезапуск бота с режимом panic только для неё (остальные
    монеты торгуют, ставка продолжается). Позиция закрылась — запись в журнал."""
    st = load_json(LIVE_STATE, {})
    bet = load_json(BET, None) or {}
    if (not st.get("want_running") or (st.get("mode") or "normal") != "normal" or bet.get("state") not in ("running", "waiting_start")
            or not pid_alive(st.get("pid"), st.get("ctime"))):
        return
    snap = fresh_snapshot(int(float(st.get("ctime") or 0) * 1000))
    if not snap:
        return
    held = held_coins(snap)
    closing = set(st.get("guard_panic") or [])
    gone = sorted(closing - held - set(st.get("guard_closed") or []))
    if gone:
        log_line(f"защита от делистинга: позиция по {', '.join(gone)} закрыта")
        st["guard_closed"] = sorted(set(st.get("guard_closed") or []) | set(gone))
        save_json(LIVE_STATE, st)
    close = {c for c, x in ((load_json(GUARD, {}) or {}).get("coins") or {}).items() if x.get("level") == "close"}
    need = sorted((close & held) - closing)
    if not need:
        return
    s = bet_settings(bet, st)
    path = cfg_path_for("live")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save_json(path, build_cfg(s, "2025-01-01", today(), "backtests/tb4/live"))     # заранее: panic для этих монет
    old_pid, old_ctime = st.get("pid"), st.get("ctime")
    st["want_running"] = False
    save_json(LIVE_STATE, st)          # на время перехода — не «должен работать», иначе отчёт увидит «процесса нет»
    proc.stop_tree(old_pid, ctime=old_ctime)
    log_line(f"защита от делистинга: закрываю позицию по {', '.join(need)} — перезапуск бота, остальные монеты торгуются, "
             f"ставка продолжается")
    pid = spawn_path(path)
    st.update(pid=pid, ctime=proc.create_time(pid), want_running=True, cfg_path=path, guard_panic=sorted(close),
              started=datetime.now().strftime("%Y-%m-%d %H:%M"))
    save_json(LIVE_STATE, st)


# движок для пользователя называется TB Core (02.10.2026): в показанных строках журнала — так же, пути не трогаем
BRAND_RE = re.compile(r"(?<![\w/\\.-])[Pp]assivbot(?![\w-])")


def brand(text):
    return BRAND_RE.sub("TB Core", text)


def live_log(n=120):
    try:
        with open(LIVE_LOG, encoding="utf-8", errors="replace") as f:
            return [brand(x) for x in f.readlines()[-n:]]
    except OSError:
        return []


# ------------------------------------------------------------------ сводное состояние
def state():
    s = settings()
    pp = load_json(os.path.join(DATA, "paper.json"), None)
    job = dict(JOB)
    if job.get("started"):
        job["elapsed"] = round(time.time() - job["started"])
    return {
        "settings": s, "presets": presets(), "coins": coins_status(s), "cfg_fp": cfg_fp(s),
        "job": job,
        "backtest": load_json(os.path.join(DATA, "backtest_result.json"), None),
        "paper": {"info": pp, "result": load_json(os.path.join(DATA, "paper_result.json"), None)},
        "keys": keys_info(), "live": live_status(), "bet": load_json(BET, None) if MODE == "bet" else None,
        "mode": MODE, "profile": {"label": PROFILE.get("label"), "app": PROFILE.get("app")},
        "lite": LITE,
        "coin_guard": load_json(GUARD, None), "coin_list": load_json(COINLIST, None),
        "btc_move": btc_move_cached(int(s.get("btc_exit_hours") or 12), ttl=300) if s.get("btc_exit") else None,
        "server_time": datetime.now().strftime("%H:%M:%S"),
    }


def save_settings(body):
    s = settings()
    if body.get("preset") in presets():
        if body["preset"] != s["preset"]:   # выбор пресета подставляет его правила (выход по BTC, плечо)
            bd = preset_rules(body["preset"])       # bet_defaults в режиме bet, defaults в режиме main
            s.update({k: v for k, v in bd.items() if k in PRESET_RULES})
            body = {k: v for k, v in body.items() if k not in bd}
        s["preset"] = body["preset"]
    try:
        if "balance" in body:
            s["balance"] = float(body["balance"])
        for k in ("exposure", "hsl_threshold"):
            if k in body:
                s[k] = float(body[k]) if body[k] not in (None, "") else None
        if "positions" in body:
            s["positions"] = int(body["positions"]) if body["positions"] not in (None, "") else None
        if "hsl_enabled" in body:
            s["hsl_enabled"] = None if body["hsl_enabled"] is None else bool(body["hsl_enabled"])
        if "leverage" in body:
            s["leverage"] = int(body["leverage"])
        if "btc_exit" in body:
            s["btc_exit"] = float(body["btc_exit"] or 0)
            if s["btc_exit"] and not 0.02 <= s["btc_exit"] <= 0.2:
                raise UIError("Выход при обвале BTC — падение от 2% до 20% или выключен.")
        if "btc_exit_hours" in body:
            s["btc_exit_hours"] = int(body["btc_exit_hours"])
            if not 2 <= s["btc_exit_hours"] <= 48:
                raise UIError("Окно для обвала BTC — от 2 до 48 часов.")
        if MODE == "bet" and "hot_ratio" in body:    # фильтр разогнанных монет есть только у ставки
            s["hot_ratio"] = float(body["hot_ratio"] or 0)
            if s["hot_ratio"] and not 1.5 <= s["hot_ratio"] <= 20:
                raise UIError("Фильтр разогнанных монет — рост или падение цены от 1,5 до 20 раз или выключен.")
    except (TypeError, ValueError):
        raise UIError("Проверьте числа в настройках.")
    if not 20 <= s["balance"] <= 1_000_000:
        raise UIError("Баланс — от 20 до 1 000 000 USDT.")
    if s["exposure"] is not None and not 0.2 <= s["exposure"] <= 10:      # плечо на бирже — не больше 10 (решение 01.10.2026)
        raise UIError("Экспозиция — от 0,2 до 10.")
    if s["positions"] is not None and not 1 <= s["positions"] <= 20:
        raise UIError("Позиций — от 1 до 20.")
    coin_mode = effective_params(s)["hsl_mode"] == "coin"
    if s["hsl_threshold"] is not None:
        if coin_mode and not 0.2 <= s["hsl_threshold"] <= 1.0:      # TB Core: в режиме coin красный уровень ≤ 1 доли
            raise UIError("Стоп по монете — от 20% до 100% доли монеты.")
        if not coin_mode and not 0.05 <= s["hsl_threshold"] <= 0.9:
            raise UIError("Аварийный стоп — от 5% до 90% просадки.")
    if not 1 <= s["leverage"] <= 20:
        raise UIError("Плечо на бирже — от 1 до 20.")
    # Проверка экспозиции у веток была разной и оставлена как была: у ставки — только явно заданная в панели,
    # у основного бота — фактическая из пресета (её задаёт оптимизатор, и она может быть больше заданной).
    too_big = (s["exposure"] is not None and s["exposure"] > s["leverage"]) if MODE == "bet" \
        else float(effective_params(s)["exposure"]) > s["leverage"]
    if too_big:
        raise UIError("Плечо на бирже должно быть не меньше экспозиции, иначе бирже не хватит маржи.")
    save_json(os.path.join(DATA, "settings.json"), s)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False,
                                                                     default=lambda o: None).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise UIError("Слишком большой запрос.")
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except ValueError:
            raise UIError("Некорректный запрос.")

    def do_GET(self):
        if not self._ok_host():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                # Страница одна на обоих ботов: имя и режим подставляются из profile.json. Иначе основной
                # бот увидел бы интерфейс ставки, которого в нём нет, — карточка, фиксация, отрезки.
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    html = f.read().decode("utf-8")
                html = (html.replace("{{PANEL_LABEL}}", str(PROFILE.get("label") or MODE))
                            .replace("{{PANEL_MODE}}", MODE))
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, clean(state()))
            if path == "/api/live/log":
                return self._send(200, {"lines": live_log()})
            return self._send(404, {"error": "not found"})
        except UIError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            return self._send(500, {"error": str(exc)})

    def do_POST(self):
        origin = self.headers.get("Origin")
        local = origin is None or re.match(r"^http://(127\.0\.0\.1|localhost):\d+$", origin)   # в т.ч. через SSH-туннель
        if not self._ok_host() or not local:
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/settings":
                save_settings(body)
            elif path == "/api/backtest":
                backtest(body.get("days", 160))
            elif path == "/api/paper/start":
                paper_start(body.get("balance"))
            elif path == "/api/paper/refresh":
                paper_refresh(force=True)
            elif path == "/api/paper/stop":
                for f in ("paper.json", "paper_result.json"):
                    try:
                        os.remove(os.path.join(DATA, f))
                    except OSError:
                        pass
            elif path == "/api/keys":
                save_keys(body)
            elif path == "/api/keys/check":
                return self._send(200, {"ok": True, "balance": check_keys()})
            elif path == "/api/live/start":
                live_start(body)
            elif path == "/api/live/stop":
                live_stop_manual() if MODE == "bet" else live_stop()
            elif path == "/api/bet/lock" and MODE == "bet":      # маршруты ставки в режиме main не существуют
                if not live_status()["running"]:
                    raise UIError("Бот не работает — фиксировать нечего.")
                src = "из Telegram" if body.get("source") == "telegram" else "из панели"
                threading.Thread(target=lock_profit, args=(f"вручную {src}",), daemon=True).start()
            elif path == "/api/bet/reset" and MODE == "bet":
                bet_reset()
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True})
        except UIError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            return self._send(500, {"error": f"Ошибка: {exc}"})


def clean(o):
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def main():
    import socket
    import webbrowser
    url = f"http://127.0.0.1:{PORT}/"
    with socket.socket() as sk:
        sk.settimeout(0.5)
        if sk.connect_ex(("127.0.0.1", PORT)) == 0:
            print("Панель уже запущена:", url)
            webbrowser.open(url)
            return
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.daemon_threads = True
    proc.set_oom_score(None, 300)   # панель — первая на закрытие при нехватке памяти (systemd перезапустит её)
    threading.Thread(target=bet_watcher if MODE == "bet" else btc_watcher, daemon=True).start()
    threading.Thread(target=resume_watcher, daemon=True).start()   # поднимает бота после перезагрузки/сбоя
    for stream in (sys.stdout, sys.stderr):      # консоль Windows не в UTF-8 — непечатный знак не должен ронять панель
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    print("=" * 60)
    print(f"  Панель «{PROFILE.get('label') or MODE}» [{MODE}]:", url)
    if MODE == "bet":
        print("  Пока бот торгует, держите это окно открытым: оно следит за ставкой (обвал BTC, пауза, фиксация).")
    else:
        print("  Пока бот торгует, держите это окно открытым: оно следит за выходом при обвале BTC.")
    print("  Не закрывайте это окно — вместе с ним остановится панель.")
    print("  Реальная торговля (если запущена) продолжит работать отдельно.")
    print("=" * 60)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
