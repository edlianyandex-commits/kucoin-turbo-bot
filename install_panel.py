"""Установка общей панели в бота.

Панель одна на двоих (C:\\bot\\panel), а у каждого бота своя копия в tb4/panel — потому что
там лежат настройки и состояние. Копии собираются из общей папки и profile.<app>.json.

    python install_panel.py turbo     поставить в C:\\bot\\turbo\\tb4\\panel
    python install_panel.py v4        поставить в C:\\bot\\v4\\tb4\\panel
    python install_panel.py both      поставить в оба
    python install_panel.py check     только показать, что разойдётся (ничего не пишет)

Папка data/ (состояние торговли, ставки, ключи) не трогается. Прежние копии панели
складываются в panel_backup_<метка>/ рядом — на случай отката.
"""
import json, pathlib, shutil, sys, io, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = pathlib.Path(r"C:\bot")
SRC = ROOT / "panel"
TARGETS = {"turbo": ROOT / "turbo" / "tb4" / "panel"}
# 15.10.2026: основной бот удалён. Готовый профиль для его возврата лежит рядом — panel/profile.v4.json,
# стратегия ema_wide — в panel/presets.json (mode=main).
SHARED = ("server.py", "index.html", "presets.json", "proc.py", "coin_guard.py", "tuned/ema_anchor.json")


def payload(app):
    """Файлы, которые ставим в бота: общие из panel/ плюс его профиль (в panel/ он лежит как profile.<app>.json)."""
    files = list(SHARED) + ["profile.json"]
    src_of = {rel: (SRC / f"profile.{app}.json" if rel == "profile.json" else SRC / rel) for rel in files}
    return files, src_of


def install(app, dry=False):
    dst = TARGETS[app]
    files, src_of = payload(app)
    prof = json.loads(src_of["profile.json"].read_text(encoding="utf-8"))
    print(f"=== {app} -> {dst} ===")
    print(f"  профиль: mode={prof['mode']} port={prof['port']} user={prof['user']} "
          f"preset={prof['default_preset']} label={prof['label']}")

    diffs = []
    for rel in files:
        target = dst / rel
        same = target.exists() and target.read_bytes() == src_of[rel].read_bytes()
        if not same:
            diffs.append(rel)
        print(f"  {'одинаково' if same else 'ОТЛИЧАЕТСЯ':<12} {rel}")

    if not diffs:
        print("  совпадает, ничего делать не нужно")
        return 0
    if dry:
        print(f"  (пробный запуск: записать {len(diffs)} файл(ов))")
        return len(diffs)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = dst.parent / f"panel_backup_{stamp}"
    bak.mkdir(parents=True)
    for rel in files:
        target = dst / rel
        if target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            (bak / rel).parent.mkdir(parents=True, exist_ok=True)   # tuned/ — вложенный путь
            shutil.copy2(target, bak / rel)
    print(f"  прежние копии: {bak}")

    for rel in files:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_of[rel], target)
    print(f"  записано файлов: {len(files)}, data/ не тронута")

    # контроль: панель обязана грузиться с новым профилем
    import ast
    ast.parse((dst / "server.py").read_text(encoding="utf-8"))
    print("  синтаксис server.py: ок")
    return len(diffs)


argv = sys.argv[1:] or ["check"]
app_arg = argv[0]
if app_arg not in ("turbo", "both", "check"):
    raise SystemExit("Укажите: turbo | both | check. Основной бот удалён 15.10.2026; "
                     "профиль для его возврата — panel/profile.v4.json")

apps = list(TARGETS) if app_arg in ("both", "check") else [app_arg]
dry = app_arg == "check"

total = 0
for a in apps:
    total += install(a, dry)
    print()

print(f"итого затронуто файлов: {total}" + ("  (пробный запуск, записи не было)" if dry else ""))