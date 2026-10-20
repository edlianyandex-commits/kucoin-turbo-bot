"""Ход торговли для репетиции: настоящее ядро passivbot на реальных ценах, конфиг — ровно как панель соберёт
для реальной торговли. Результат — replay-файл: капитал по часам и все сделки (размер позиции после каждой).

  venv\\Scripts\\python deploy\\sim\\make_replays.py --root "C:\\bot\\turbo" --preset turbo_ema5 --balance 300 \
      --start 2026-08-30 --end 2026-09-29 --label turbo5_pause --out deploy\\sim\\replays\\turbo5_pause.json --btc
Запускать питоном из venv того проекта, чей пресет нужен (--root). --btc — добавить часовые цены BTC за период:
тогда на стенде выход при обвале BTC смотрит на BTC «во время сценария» (sim_panel.py), а не на сегодняшний.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

import pandas as pd


def btc_hourly(t_from, t_to):
    """Часовые закрытия BTC [[время открытия свечи, мс, закрытие], …] — Binance USDⓈ-M, публичные данные."""
    import requests
    s = requests.Session()
    s.trust_env = False
    out, t = [], t_from
    while t < t_to:
        rows = s.get("https://fapi.binance.com/fapi/v1/klines",
                     params={"symbol": "BTCUSDT", "interval": "1h", "startTime": t, "endTime": t_to, "limit": 1500},
                     timeout=20).json()
        if not rows:
            break
        out += [[int(r[0]), float(r[4])] for r in rows]
        t = rows[-1][0] + 3_600_000
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--preset", required=True)
    ap.add_argument("--balance", type=float, required=True)
    ap.add_argument("--leverage", type=int, default=None)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--btc", action="store_true", help="добавить часовые закрытия BTC (для выхода при обвале BTC в турбо)")
    ap.add_argument("--minute", action="store_true", help="капитал поминутно, а не по часам (стоп ставки на резком падении)")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    sys.path.insert(0, os.path.join(root, "tb4", "panel"))
    os.chdir(root)
    import server as P

    s = dict(P.settings(), preset=a.preset, balance=a.balance, exposure=None, positions=None,
             hsl_enabled=None, hsl_threshold=None)
    if a.leverage:
        s["leverage"] = a.leverage
    base_dir = f"backtests/tb4/sim_{a.label}"
    cfg = P.build_cfg(s, a.start, a.end, base_dir)
    if a.minute:
        cfg["backtest"]["balance_sample_divider"] = 1
    cfg_path = os.path.join(root, "configs", "tb4", f"sim_{a.label}.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    print(f"[{a.label}] прогон passivbot {a.start} — {a.end}, пресет {a.preset}, ${a.balance:g}", flush=True)
    for _attempt in range(6):   # монеты, которых в тот период ещё не было на Binance, исключаются (как в тестах по годам)
        P.save_json(cfg_path, cfg)
        before = set(glob.glob(os.path.join(root, base_dir, "*", "*")))
        r = subprocess.run([P.PASSIVBOT, "backtest", cfg_path, "-dp", "y"], cwd=root, env=P.child_env(),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", **P.proc.hidden_kwargs())
        runs = sorted(set(glob.glob(os.path.join(root, base_dir, "*", "*"))) - before, key=os.path.getmtime)
        if r.returncode == 0 and runs:
            break
        m = re.search(r"No exchange data found for coin (\w+)", r.stdout + r.stderr)
        if not m:
            print(r.stdout[-3000:], r.stderr[-3000:])
            sys.exit(f"[{a.label}] прогон не удался")
        coin = m.group(1)
        print(f"[{a.label}] монеты {coin} в этот период не было — исключаю", flush=True)
        for side in ("long", "short"):
            cfg["live"]["approved_coins"][side] = [c for c in cfg["live"]["approved_coins"][side] if c != coin]
        for key in ("coin_sources", "market_settings_sources"):
            cfg["backtest"].get(key, {}).pop(coin, None)
    else:
        sys.exit(f"[{a.label}] прогон не удался")
    run = runs[-1]

    eq = pd.read_csv(os.path.join(run, "balance_and_equity.csv.gz"))
    eq["t"] = pd.to_datetime(eq.iloc[:, 0], utc=True)
    fills = pd.read_csv(os.path.join(run, "fills.csv"))
    fills["t"] = pd.to_datetime(fills["timestamp"], utc=True)
    ms = lambda t: int(t.value // 1_000_000)  # noqa: E731

    equity = [[ms(t), round(float(b), 4), round(float(e), 4), round(float(se), 4)]
              for t, b, e, se in zip(eq["t"], eq["usd_total_balance"], eq["usd_total_equity"], eq["strategy_equity"])]
    rows = [[ms(t), c, ty, float(q), float(p), float(ps), float(pp), float(pnl), float(fee), float(we)]
            for t, c, ty, q, p, ps, pp, pnl, fee, we in zip(
                fills["t"], fills["coin"], fills["type"], fills["qty"], fills["price"], fills["psize"],
                fills["pprice"], fills["pnl"], fills["fee_paid"], fills["wallet_exposure"])]

    # сколько раз позиция открывалась/закрывалась (переход размера 0 ↔ не 0) — столько было бы уведомлений
    size = defaultdict(float)
    opens = closes = 0
    for _, c, _ty, _q, _p, ps, *_ in rows:
        was = size[c] > 0
        now = ps > 1e-12
        opens += (not was) and now
        closes += was and not now
        size[c] = ps if now else 0.0
    days = max(1e-9, (equity[-1][0] - equity[0][0]) / 86_400_000)
    peak, max_dd = equity[0][2], 0.0
    for _, _, e, _ in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, 1 - e / peak if peak > 0 else 0)
    hsl = cfg["bot"]["long"]["hsl"]
    panic = [r_ for r_ in rows if "panic" in r_[2]]
    out = {
        "meta": {"label": a.label, "app": "turbo", "preset": a.preset,
                 "start": a.start, "end": a.end, "balance": a.balance, "leverage": s["leverage"],
                 "twel": cfg["bot"]["long"]["risk"]["total_wallet_exposure_limit"],
                 "n_positions": cfg["bot"]["long"]["risk"]["n_positions"],
                 "hsl": {"enabled": bool(hsl["enabled"]), "red_threshold": float(hsl["red_threshold"]),
                         "restart_after_red_policy": hsl["restart_after_red_policy"],
                         "cooldown_minutes_after_red": float(hsl["cooldown_minutes_after_red"]),
                         "tier_ratios": hsl.get("tier_ratios", {"yellow": 0.5, "orange": 0.75}),
                         "signal_mode": cfg["live"].get("hsl_signal_mode", "coin")},     # coin — стоп по монете
                 "run_dir": run},
        "stats": {"days": round(days, 2), "fills": len(rows), "fills_per_day": round(len(rows) / days, 1),
                  "position_opens": opens, "position_closes": closes,
                  "position_msgs_per_day": round((opens + closes) / days, 1),
                  "start_equity": equity[0][2], "end_equity": equity[-1][2], "max_dd_pct": round(max_dd * 100, 1),
                  "panic_fills": len(panic), "first_panic_ms": panic[0][0] if panic else None,
                  "last_panic_ms": panic[-1][0] if panic else None},
        "equity": equity,
        "fills": rows,
    }
    if a.btc:
        out["btc"] = btc_hourly(equity[0][0] - 2 * 86_400_000, equity[-1][0] + 3_600_000)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    st = out["stats"]
    print(f"[{a.label}] готово: {st['days']} дн., сделок {st['fills']} ({st['fills_per_day']}/день), "
          f"открытий/закрытий позиций {opens}/{closes} ({st['position_msgs_per_day']} уведомл./день), "
          f"капитал {st['start_equity']:.2f} → {st['end_equity']:.2f}, просадка {st['max_dd_pct']}%, "
          f"аварийных закрытий {st['panic_fills']}", flush=True)


if __name__ == "__main__":
    main()
