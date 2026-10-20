"""Генерация ai/AI_SYMBOLS.txt по engine/src.

Запуск: python ai/gen_symbols.py  (из корня C:\bot, любой python 3.10+)
Формат строки: путь:строка\tвид\tимя  — путь от корня C:\bot.
Заодно проверяет синтаксис каждого файла (ast.parse): битый файл упадёт с ошибкой.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "engine", "src")

rows = []
errors = []

for dirpath, dirnames, filenames in os.walk(SRC):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for fn in sorted(filenames):
        if not fn.endswith(".py"):
            continue
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, ROOT).replace("\\", "/")
        try:
            tree = ast.parse(open(full, encoding="utf-8").read(), filename=rel)
        except SyntaxError as e:
            errors.append(f"{rel}: {e}")
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
                rows.append((rel, node.lineno, "def", name))
            elif isinstance(node, ast.ClassDef):
                rows.append((rel, node.lineno, "class", node.name))
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        rows.append((rel, sub.lineno, "  def", f"{node.name}.{sub.name}"))

rows.sort(key=lambda r: (r[0], r[1]))
out = os.path.join(ROOT, "ai", "AI_SYMBOLS.txt")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write("# ИНДЕКС СИМВОЛОВ — движок один, engine/src (панели ищи grep-ом по turbo/ и v4/)\n")
    f.write("# формат: путь:строка\tвид\tимя; перегенерация: python ai/gen_symbols.py\n")
    f.write(f"# файлов: {len({r[0] for r in rows})}, символов: {len(rows)}\n")
    for rel, line, kind, name in rows:
        f.write(f"{rel}:{line}\t{kind}\t{name}\n")

print(f"OK: {len({r[0] for r in rows})} files, {len(rows)} symbols -> ai/AI_SYMBOLS.txt")
if errors:
    print("SYNTAX ERRORS:")
    for e in errors:
        print(" ", e)
    sys.exit(1)
