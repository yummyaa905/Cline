#!/usr/bin/env bash
# 健康检查 + 自愈：桥不在就拉起来。适合挂到 Cline cron / 系统 crontab（每 5 分钟一次）。
# 注意：同一个 bot token 只能有一个 getUpdates 消费者，所以只拉 Telegram ↔ Cline 桥。
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if "$ROOT/tools/tg_bridge_service.sh" status >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] ok"
  exit 0
fi

echo "[$(date '+%F %T')] 检测到桥未运行，尝试拉起"
"$ROOT/tools/tg_bridge_service.sh" start
