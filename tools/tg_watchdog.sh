#!/usr/bin/env bash
# 健康检查 + 自愈：进程不在就拉起来。适合挂到 Cline cron / 系统 crontab（每 5 分钟一次）。
# 用法: tools/tg_watchdog.sh   （可用后看 logs/tg-bot.log）
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if "$ROOT/tools/tg_service.sh" status >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] ok"
  exit 0
fi

echo "[$(date '+%F %T')] 检测到机器人未运行，尝试拉起"
"$ROOT/tools/tg_service.sh" start
