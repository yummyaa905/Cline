# Telegram ↔ Cline 桥

在 Telegram 里给 bot 发消息，由本机 Cline Hub 拉起 / 续接一个 agent 会话干活，再把结果发回 Telegram。
也就是说：**Telegram 成了操作这个沙箱里 agent 的入口**（能读写文件、跑命令、提交代码）。

## 目录

```
tools/tg_bridge.mjs         桥本体（Node，复用 @cline/core 的 HubSessionClient）
tools/tg_bridge_service.sh  桥的守护脚本（start|stop|restart|status|logs，崩溃自动重启+退避）
tools/tg_watchdog.sh        健康检查自愈（给 cron 每 5 分钟调用）
tools/tg.py                 Telegram Bot API 基础客户端（me / send / updates / listen）
tools/tg_bot.py             【可选】独立 FAQ 客服机器人（不需要 agent，和桥二选一）
tools/tg_service.sh         【可选】FAQ 机器人的守护脚本
data/faq.json               【可选】FAQ 知识库
tests/test_tg_bot.py        【可选】FAQ 机器人的测试（15 个用例）
```

## 快速开始

```bash
cp .env.example .env    # 填 TG_BOT_TOKEN（BotFather 获取）
tools/tg_bridge_service.sh start     # 后台常驻
tools/tg_bridge_service.sh status    # 状态 + 最近日志
tools/tg_bridge_service.sh logs      # 跟踪日志
tools/tg_bridge_service.sh stop      # 优雅停止（0.5s 内）
```

自检与调试：

```bash
node tools/tg_bridge.mjs --self-test              # 建会话 + 问一句，验证 Hub 链路
node tools/tg_bridge.mjs --simulate "你好"         # 只跑一轮，打印「会发给 TG 的内容」，不发送
node tools/tg_bridge.mjs --replay <chatId> "指令"  # 走真实链路：agent 回答 → 真的发到 Telegram
```

## 它怎么工作

1. `GET http://127.0.0.1:8081/hub-token` 拿 Hub 的 `url` + `authToken`（沙箱内已就绪）。
2. `HubSessionClient` 连 Hub，`startRuntimeSession()` 为每个 chat 建一个会话。
3. 每条 Telegram 消息 → `sendRuntimeSession(sessionId, { config, prompt })`，取 `result.text` 回发。
4. `offset` 存 `logs/tg_bridge_state.json`：重启不重复处理；`/new` 可开新会话。

## 投喂给 agent 的能力

`TG_BRIDGE_TOOLS=1`（默认开）时 agent 可用工具；`TG_BRIDGE_AUTO_APPROVE=1` 表示无人值守自动批准，
否则带工具调用会卡在等待批准。只想纯聊天就设 `TG_BRIDGE_TOOLS=0`。

## 指令

- `/help` `/start` — 使用说明
- `/status` — 当前会话 ID、轮次、最近时间
- `/new` — 丢弃上下文，开新会话

## 注意事项（踩过的坑）

- **同一个 bot token 只能有一个 `getUpdates` 消费者**：桥在跑时不要再 `python3 tools/tg.py updates`，
  否则互相 `409 Conflict` 并可能抢消息。桥和 FAQ 机器人（`tg_service.sh`）也只能二选一，
  `tg_bridge_service.sh start` 会自动停掉 FAQ 机器人。
- 长任务请用 `setsid nohup ... &` 起，前台跑容易被会话超时打断。
- 并发别乱开：多个 `--replay` 同时跑会各自建会话、互相拖慢（实测会卡住），一次一轮。

## 已知限制

- 进程存活取决于沙箱是否被回收；被回收后 `tools/tg_watchdog.sh` 可由 cron 重新拉起。
- 桥用的是当前 `cline-cloud` 模型（`TG_BRIDGE_MODEL` 可改），单个回合默认 300 秒超时。
- `TG_BOT_TOKEN` 泄露请去 @BotFather `/revoke` 换新，更新 `.env` 后 `restart`。
