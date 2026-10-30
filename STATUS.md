# 会话状态 / 恢复说明（STATUS）

> 本文件用于「常驻」：沙箱可能随时被销毁、对话上下文会丢失，但本文件随 git 分支
> `cline/krawce2s` 保存在远端。新会话开始时先读这个文件即可续上进度。

## 当前状态

- 日期：2026-10-30
- 工作目录：`/workspace`（仓库 `github.com/yummyaa905/Cline`，初始为空仓库、无提交、无 issue）
- 已完成：无代码变更；仅建立分支 `cline/krawce2s` 与状态记录
- 待办：等待用户给出具体任务（修 bug / 搭项目 / 拉取代码）

## 新会话恢复协议

1. `git fetch origin && git checkout cline/krawce2s && git pull`
2. 阅读本文件与最近的提交信息，确认进度
3. 继续未完成事项，并在此文件更新状态后提交、推送

## 环境要点（已核实）

- 无 `cline` CLI、无 `tmux`/`crontab`，`systemctl` 为 offline（PID 1 是宿主进程）→
  靠 OS 守护进程做常驻不可行，容器随时会被回收。
- Cline 运行时数据在 `/workspace/.cline-home/data/db/`：
  - `cron.db`（`cron_specs` / `cron_runs` / `cron_event_log`）：定时与事件触发的常驻入口
  - `tasks.db`（`agenda_tasks` / `agenda_task_automation`）：任务自动化，
    `mode` 可为 `manual` / `auto_start` / `unattended`（当前全局为 `manual`）
  - `teams.db`、`hub-runs-*.db`：团队与 hub 会话运行记录
- 规则文件注入目录：`/workspace/.cline-home/rules/`（每次会话都会注入，但只在当前沙箱内有效）

## 结论：可用的「常驻」方式

| 方式 | 持久性 | 说明 |
| --- | --- | --- |
| Cron 定时/事件规格 | ✅ 跨会话 | 由 hub 生成 run，无需人工发起 |
| Agenda 任务 + `unattended` 自动化 | ✅ 跨会话 | 任务排队后自动开跑，可链式续跑 |
| git 分支 + 本文件（记忆常驻） | ✅ 跨沙箱 | 状态与代码可恢复 |
| tmux / nohup / systemd 常驻进程 | ❌ | 容器无 systemd/crontab，沙箱销毁即终止 |
