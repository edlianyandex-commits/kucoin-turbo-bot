#!/usr/bin/env bash
# Сторожевой таймер: бот не должен оставаться торговым без панели.
#
# Зачем: панель TradeBot сама управляет ботом (в том числе в паузе при обвале BTC).
# Юнит панели намеренно идёт с KillMode=process, чтобы перезапуск панели не убивал
# бота. Обратная сторона: если панель упала или остановлена, бот продолжает торговать
# уже без защиты — выход по обвалу BTC живёт в панели.
#
# Что делает скрипт: если юнит панели не active, а бот этого приложения ещё жив —
# отправляет ему SIGINT (бот ловит его и закрывается сам, закрывая позиции) и
# пишет в лог. Если панель работает — ничего не делает.
#
# Использование: server_watchdog.sh turbo   (по одному приложению)
set -uo pipefail

APP="${1:-}"
PANEL_UNIT="tradebot-${APP}.service"
LOG_TAG="tradebot-watchdog-${APP}"
WATCHDOG_DIR="/opt/tradebot/deploy/out"
GRACE_SECONDS="${WATCHDOG_GRACE_SECONDS:-120}"

log() {
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    logger -t "${LOG_TAG}" -- "${ts} $*" 2>/dev/null || true
    mkdir -p "${WATCHDOG_DIR}" 2>/dev/null || true
    printf '%s %s\n' "${ts}" "$*" >> "${WATCHDOG_DIR}/watchdog.log" 2>/dev/null || true
}

[ -n "${APP}" ] || { log "ошибка: не указан приложение (turbo)"; exit 2; }

# Боты этого приложения: passivbot live из его рабочего каталога.
bot_pids() {
    pgrep -f "passivbot.*live" 2>/dev/null | while read -r pid; do
        exe_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
        case "${exe_cwd}" in
            "/opt/tradebot/${APP}"*) printf '%s\n' "${pid}" ;;
        esac
    done
}

panel_active() {
    # «activating» тоже считаем живым: панель может быть в середине рестарта.
    local st
    st="$(systemctl is-active "${PANEL_UNIT}" 2>/dev/null || echo unknown)"
    case "${st}" in
        active|activating|reloading) return 0 ;;
        *) return 1 ;;
    esac
}

pids="$(bot_pids)"
[ -n "${pids}" ] || exit 0          # бота нет — делать нечего

if panel_active; then
    exit 0                          # панель жива, защита работает
fi

# Панель не активна. Даём ей время подняться: если systemctl stop/restart длится
# дольше GRACE_SECONDS, бот всё равно остановится, но ложное срабатывание при
# быстром рестарте не случится.
state="$(systemctl is-active "${PANEL_UNIT}" 2>/dev/null || echo unknown)"
log "панель ${PANEL_UNIT} в состоянии '${state}', бот(ы) живы: ${pids//$'\n'/, }"

stop_marker="${WATCHDOG_DIR}/.last_stop_${APP}"
mkdir -p "${WATCHDOG_DIR}" 2>/dev/null || true
now="$(date +%s)"
last=0
if [ -f "${stop_marker}" ]; then
    last="$(head -n1 "${stop_marker}" 2>/dev/null | tr -cd '0-9')"
fi
case "${last}" in
    ''|*[!0-9]*) last=0 ;;
esac
if [ "$(( now - last ))" -lt "${GRACE_SECONDS}" ]; then
    log "недавно останавливали (${GRACE_SECONDS}s назад), жду"
    exit 0
fi

for pid in ${pids}; do
    if kill -0 "${pid}" 2>/dev/null; then
        log "останавливаю бота pid=${pid} (SIGINT, ждём graceful shutdown)"
        kill -INT "${pid}" 2>/dev/null || true
    fi
done

sleep 20
left=""
for pid in ${pids}; do
    if kill -0 "${pid}" 2>/dev/null; then
        left="${left} ${pid}"
    fi
done
if [ -n "${left}" ]; then
    log "бот не завершился за 20 c (pid:${left}), повторю SIGINT"
    for pid in ${left}; do kill -INT "${pid}" 2>/dev/null || true; done
fi

date +%s > "${stop_marker}" 2>/dev/null || true
log "готово"