"""Запуск и остановка TB Core одинаково на Windows и Linux."""
import os
import signal
import subprocess
import time

IS_WIN = os.name == "nt"


def venv_bin(root, name):
    """Путь к программе из venv проекта: venv\\Scripts\\name.exe на Windows, venv/bin/name на Linux."""
    if IS_WIN:
        return os.path.join(root, "venv", "Scripts", name + ".exe")
    return os.path.join(root, "venv", "bin", name)


def env_path(root):
    cargo = os.path.join(os.path.expanduser("~"), ".cargo", "bin")
    vbin = os.path.join(root, "venv", "Scripts" if IS_WIN else "bin")
    return os.pathsep.join([cargo, vbin, os.environ.get("PATH", "")])


def detached_kwargs():
    """Отдельная группа процессов без консоли: сигналы одного процесса не задевают панель и соседей."""
    if IS_WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def hidden_kwargs():
    """Без окна консоли (Windows); на Linux ничего не нужно."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}


def set_oom_score(pid, value):
    """Linux: очередь на закрытие при нехватке памяти (больше — закроют раньше; None — этот процесс).
    Повышать можно всегда, понижать — не ниже значения, с которым запущена служба (у нас 0)."""
    if IS_WIN:
        return False
    try:
        with open(f"/proc/{'self' if pid is None else int(pid)}/oom_score_adj", "w") as f:
            f.write(str(int(value)))
        return True
    except (OSError, ValueError):
        return False


def create_time(pid):
    """Время старта процесса — вместе с PID однозначно определяет процесс (PID после перезагрузки переиспользуется)."""
    try:
        import psutil
        return round(psutil.Process(int(pid)).create_time(), 1)
    except Exception:
        return None


def pid_alive(pid, ctime=None):
    """Процесс жив; если задан ctime — это тот самый процесс, а не чужой с тем же PID."""
    if not pid:
        return False
    try:
        import psutil
        p = psutil.Process(int(pid))
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return False
        return ctime is None or abs(p.create_time() - float(ctime)) < 2
    except Exception:
        return False


def stop_tree(pid, grace_s=20.0, ctime=None):
    """Мягкая остановка (как Ctrl+C: бот сохраняет состояние), через grace_s — принудительно вместе с дочерними."""
    if not pid_alive(pid, ctime):
        return
    pid = int(pid)
    try:
        if IS_WIN:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(pid), signal.SIGINT)
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < grace_s:
        if not pid_alive(pid):
            return
        time.sleep(0.5)
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass
