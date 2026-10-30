# Cline × Telegram 客服机器人

零依赖（仅 Python 标准库）的 Telegram 客服 / AI 自动回复机器人，附带守护脚本与自动化测试。

## 目录

```
tools/tg.py            Telegram Bot API 基础客户端（me / send / updates / listen）
tools/tg_bot.py        24/7 客服机器人（长轮询、FAQ、可选 AI、限流、去重、白名单）
tools/tg_service.sh    守护脚本（start|stop|restart|status|logs，崩溃自动重启+退避）
tools/tg_watchdog.sh   健康检查自愈（适合挂 cron，每 5 分钟一次）
data/faq.json          客服知识库（关键词 → 标准答案）
tests/test_tg_bot.py   端到端测试（本地假 Telegram API，无需真 token）
```

## 快速开始

```bash
cp .env.example .env          # 填入 TG_BOT_TOKEN（BotFather 获取）
python3 tools/tg_bot.py --dry-run     # 自检：token/FAQ/限流/白名单
python3 -m unittest discover -s tests -v   # 10 个用例，全部通过
tools/tg_service.sh start             # 后台常驻（崩溃自动重启）
tools/tg_service.sh status            # 查看状态 + 最近日志
tools/tg_service.sh logs              # 跟踪日志
tools/tg_service.sh stop              # 优雅停止
```

## 行为说明

- **回复优先级**：内置指令（/start /help /faq /id /reset）→「人工」转人工提示 → AI（若配置）→ FAQ 关键词匹配 → 兜底话术。
- **不重复回复**：`update_id` 持久化在 `logs/tg_state.json`，重启后继续；同一消息重复投递会被去重。
- **限流**：默认每个 chat 每分钟 20 条（`TG_RATE_LIMIT`）。
- **白名单**：`TG_ALLOWED_CHATS=123,456`，留空表示对所有人开放。
- **AI（可选）**：设置 `AI_BASE_URL` + `AI_API_KEY` + `AI_MODEL`（OpenAI 兼容 `/chat/completions`）；AI 调用失败自动回退 FAQ。
- **隐私**：`TG_BOT_TOKEN` 只从环境变量或 `.env` 读取，`.env` 已被 `.gitignore` 忽略。

## 常驻策略

容器里没有 systemd/crontab，所以用两层保障：

1. `tools/tg_service.sh` 的 supervisor 循环：进程退出非 0 时按 2→4→…→60 秒退避重启。
2. `tools/tg_watchdog.sh` 挂到 Cline 的 cron 规格（每 5 分钟）：宿主进程被回收后把机器人重新拉起。

## 已知限制

- 机器人的存活上限取决于沙箱/宿主是否被回收；生产环境建议部署到常驻 VPS（本仓库脚本可直接复用）。
- 知识库只有 6 条示例，请按业务补充 `data/faq.json`。
