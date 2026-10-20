#!/usr/bin/env python3
"""Проверка конфига реальной торговли без запуска торговли.

Строит конфиг ровно так, как его строит панель при нажатии «Запуск» (текущие настройки панели), и прогоняет
через загрузчик TB Core (тот же путь, что `passivbot live`): схема, типы, HSL. На биржу ничего не отправляется.
  venv/bin/python deploy/validate_live.py        (из корня проекта: /opt/tradebot/turbo)
"""
import json
import logging
import os
import sys
import tempfile

ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tb4", "panel"))
sys.path.insert(0, os.path.join(ROOT, "src"))
logging.disable(logging.INFO)

import server as S  # noqa: E402  (панель: при импорте ничего не запускает)
from config import load_input_config, prepare_config  # noqa: E402
from config.hsl_revised import require_runtime_support  # noqa: E402


def get(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def engine_has_ema_cap():
    """Есть ли в исходниках ядра этого проекта наш предел доли монеты для EMA Anchor (правка только у Turbo).
    Исходный TB Core применяет position_exposure_enforcer_* лишь к trailing-стратегиям."""
    path = os.path.join(ROOT, "passivbot-rust", "src", "strategies", "ema_anchor.rs")
    try:
        with open(path, encoding="utf-8") as f:
            return "fn cap_entry_qty_by_wel" in f.read()
    except OSError:
        return False


def main():
    s = S.settings()
    variants = {"live": S.build_cfg(s, "2025-01-01", S.today(), "backtests/tb4/live")}
    if "forced_mode" in S.build_cfg.__code__.co_varnames:          # турбо: конфиг фиксации ставки
        variants["live_panic"] = S.build_cfg(s, "2025-01-01", S.today(), "backtests/tb4/live", forced_mode="panic")
    keys = S.load_json(S.KEYS_PATH, {}).get(S.USER) or {}
    print(f"панель: пресет {s['preset']}, баланс {s['balance']}, плечо {s.get('leverage')}, пользователь {S.USER}, "
          f"ключи {'заданы' if keys.get('key') and keys.get('secret') and keys.get('passphrase') else 'НЕ заданы'}"
          f"{', биржа ' + str(keys.get('exchange')) if keys else ''}")
    ok = True
    for name, raw in variants.items():
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(raw, f)
            path = f.name
        try:
            src, base_path, snap = load_input_config(path)
            cfg = prepare_config(src, base_config_path=base_path, live_only=True, verbose=False, target="live",
                                 raw_snapshot=snap)
            require_runtime_support(cfg, supported_modes=("coin", "pside", "unified"))
        except Exception as exc:
            ok = False
            print(f"[{name}] ОШИБКА конфига: {type(exc).__name__}: {exc}")
            continue
        finally:
            os.remove(path)
        L = "bot.long"
        src_app = (cfg.get("_coins_sources") or {}).get("approved_coins")
        if isinstance(src_app, str) or isinstance(get(cfg, "live.approved_coins"), str):   # файл — как его прочтёт TB Core
            from utils import normalize_coins_source
            approved = normalize_coins_source(src_app if isinstance(src_app, str) else get(cfg, "live.approved_coins"),
                                              allow_all=True)["long"]
        else:
            approved = get(cfg, "live.approved_coins.long") or []
        print(f"[{name}] конфиг принят TB Core")
        print(f"  пользователь {get(cfg, 'live.user')}, плечо {get(cfg, 'live.leverage')}, "
              f"режим long «{get(cfg, 'live.forced_mode_long') or 'обычный'}», short «{get(cfg, 'live.forced_mode_short') or 'обычный'}»")
        print(f"  экспозиция long {get(cfg, L + '.risk.total_wallet_exposure_limit')}, позиций {get(cfg, L + '.risk.n_positions')}, "
              f"short экспозиция {get(cfg, 'bot.short.risk.total_wallet_exposure_limit')}")
        print(f"  стоп HSL: {'вкл' if get(cfg, L + '.hsl.enabled') else 'выкл'}, порог {get(cfg, L + '.hsl.red_threshold')}, "
              f"после стопа {get(cfg, L + '.hsl.restart_after_red_policy')}, пауза {get(cfg, L + '.hsl.cooldown_minutes_after_red')} мин, "
              f"режим сигнала {get(cfg, 'live.hsl_signal_mode')}, сглаживание {get(cfg, L + '.hsl.ema_span_minutes')} мин, "
              f"закрытие {get(cfg, L + '.hsl.panic_close_order_type')}")
        cap = get(cfg, L + '.risk.position_exposure_enforcer_enabled')
        print(f"  предел доли монеты: {'вкл, ' + str(get(cfg, L + '.risk.position_exposure_enforcer_threshold')) + ' × (экспозиция / позиций)' if cap else 'выкл'}")
        if cap and get(cfg, "live.strategy_kind") == "ema_anchor" and not engine_has_ema_cap():
            ok = False
            print("  ВНИМАНИЕ: предел доли включён, но в ядре этого проекта нет правки для EMA Anchor "
                  "(cap_entry_qty_by_wel) — TB Core его молча проигнорирует")
        print(f"  монет разрешено {len(approved)}, фильтр по минимальному ордеру {get(cfg, 'live.filter_by_min_effective_cost')}, "
              f"мин. возраст монеты {get(cfg, 'live.minimum_coin_age_days')} дн.")
        print(f"  память: минутных свечей на монету до {get(cfg, 'live.max_memory_candles_per_symbol')}; мониторинг: снимок раз в "
              f"{get(cfg, 'monitor.snapshot_interval_seconds')} с, цены {get(cfg, 'monitor.retain_price_ticks')}, "
              f"свечи {get(cfg, 'monitor.emit_completed_candles')}, до {get(cfg, 'monitor.max_total_bytes', 0) // 1024 ** 2} МБ")
        src_ign = (cfg.get("_coins_sources") or {}).get("ignored_coins")
        ign_raw = get(cfg, "live.ignored_coins")
        if isinstance(ign_raw, str) or isinstance(src_ign, str):            # путь к файлу — как его прочтёт TB Core
            from utils import normalize_coins_source
            ign = normalize_coins_source(src_ign if isinstance(src_ign, str) else ign_raw, allow_all=False)["long"]
        else:
            ign = get(cfg, "live.ignored_coins.long") or []
        panic = sorted(c for c, v in (cfg.get("coin_overrides") or {}).items()
                       if ((v or {}).get("live") or {}).get("forced_mode_long") == "panic")
        print(f"  защита от делистинга: не входить — {', '.join(ign) or 'нет'} (список из "
              f"{'файла, перечитывается на ходу' if isinstance(src_ign, str) else 'конфига'}), закрыть — {', '.join(panic) or 'нет'}")
        if s.get("leverage") and float(get(cfg, L + ".risk.total_wallet_exposure_limit") or 0) > float(get(cfg, "live.leverage") or 0):
            ok = False
            print("  ВНИМАНИЕ: экспозиция больше плеча — биржа не даст открыть позиции полностью")
    print("ИТОГ:", "ошибок нет" if ok else "есть ошибки")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
