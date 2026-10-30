#!/usr/bin/env bash
# Telegram 客服机器人守护脚本：崩溃自动重启 + 退避 + 日志 + pid 文件。
# 用法: tools/tg_service.sh {start|stop|restart|status|logs}
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/logs/tg-bot.pid"
LOG_FILE="$ROOT/logs/tg-bot.log"
PY="${PYTHON:-python3}"
BACKOFF_MAX=60

mkdir -p "$ROOT/logs"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null
}

start() {
  if is_running; then echo "已在运行 (pid $(cat "$PID_FILE"))"; return 0; fi
  if [[ ! -f "$ROOT/.env" ]] && [[ -z "${TG_BOT_TOKEN:-}" ]]; then
    echo "缺少凭据：请先创建 $ROOT/.env 并填入 TG_BOT_TOKEN（参考 .env.example）" >&2
    return 1
  fi
  echo "启动中 ... 日志: $LOG_FILE"
  nohup bash -c '
    export LC_ALL=C.UTF-8
    root="$1"; py="$2"; max="$3"
    backoff=2; child=""
    trap "kill -TERM $child 2>/dev/null; wait $child 2>/dev/null; echo \"[supervisor] 收到退出信号\"; exit 0" TERM INT
    while true; do
      "$py" "$root/tools/tg_bot.py" &
      child=$!
      wait $child; code=$?
      if [[ $code -eq 0 ]]; then echo "[supervisor] 正常退出，停止守护"; exit 0; fi
      echo "[supervisor] 机器人退出(code=$code)，${backoff}s 后重启"
      sleep "$backoff"
      backoff=$(( backoff * 2 )); [[ $backoff -gt $max ]] && backoff=$max
    done
  ' _ "$ROOT" "$PY" "$BACKOFF_MAX" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 1
  if is_running; then echo "已启动 (pid $(cat "$PID_FILE"))"; else echo "启动失败，请查看 $LOG_FILE" >&2; return 1; fi
}

stop() {
  local pid children c alive
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  children=""
  [[ -n "${pid:-}" ]] && children="$(pgrep -P "$pid" 2>/dev/null || true)"

  # 只针对本服务自己的进程，不用 pkill -f，避免误伤命令行里带路径的其他 shell
  [[ -n "${pid:-}" ]] && kill -TERM "$pid" 2>/dev/null || true
  for c in $children; do kill -TERM "$c" 2>/dev/null || true; done

  # 机器人自己处理 SIGTERM（会打断长轮询立即退出），最多等 15 秒
  for _ in $(seq 1 30); do
    alive=0
    for c in $children; do kill -0 "$c" 2>/dev/null && alive=1; done
    [[ $alive -eq 0 ]] && break
    sleep 0.5
  done
  for c in $children; do
    if kill -0 "$c" 2>/dev/null; then
      echo "子进程 $c 未在 15 秒内退出，强制结束"
      kill -KILL "$c" 2>/dev/null || true
    fi
  done
  [[ -n "${pid:-}" ]] && kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "已停止"
}

status() {
  if is_running; then
    echo "运行中 (pid $(cat "$PID_FILE"))"
    echo "最近日志:"; tail -n 5 "$LOG_FILE" 2>/dev/null || true
  else
    echo "未在运行"
    return 3
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) tail -f "$LOG_FILE" ;;
  *) echo "用法: $0 {start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
