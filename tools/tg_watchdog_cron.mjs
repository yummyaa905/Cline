#!/usr/bin/env node
/**
 * 把 tools/tg_watchdog.sh 注册成 Cline Hub 的 cron 计划（默认每 5 分钟）。
 * Hub 到点会起一个 agent 会话，由它执行 watchdog 脚本；桥挂了就会被重新拉起。
 *
 * 用法：
 *   node tools/tg_watchdog_cron.mjs --install            # 创建（同名则更新）
 *   node tools/tg_watchdog_cron.mjs --list               # 列出所有计划
 *   node tools/tg_watchdog_cron.mjs --run-now [id]       # 立即触发一次（默认本脚本创建的那个）
 *   node tools/tg_watchdog_cron.mjs --executions [id]    # 最近的执行记录
 *   node tools/tg_watchdog_cron.mjs --delete [id]        # 删除
 *
 * 可选环境变量：TG_WATCHDOG_CRON（默认 *\/5 * * * *）、TG_WATCHDOG_TZ（默认 Asia/Shanghai）
 */

import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { HubSessionClient } from "/app/node_modules/@cline/core/dist/hub/index.js";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const NAME = "tg-bridge-watchdog";
const CRON = process.env.TG_WATCHDOG_CRON || "*/5 * * * *";
const TZ = process.env.TG_WATCHDOG_TZ || "Asia/Shanghai";
const SCRIPT = path.join(ROOT, "tools", "tg_watchdog.sh");
const PROMPT =
  `只做一件事：在 shell 里执行 \`bash ${SCRIPT}\`，然后用一行回复它的输出。` +
  "不要修改任何文件，不要提交代码，不要做额外探索。";

async function connect() {
  const res = await fetch(process.env.TG_HUB_TOKEN_URL || "http://127.0.0.1:8081/hub-token");
  if (!res.ok) throw new Error(`读取 Hub token 失败: HTTP ${res.status}`);
  const meta = await res.json();
  const client = new HubSessionClient({
    address: String(meta.url).replace("0.0.0.0", "127.0.0.1"),
    authToken: meta.authToken,
    workspaceRoot: "/workspace",
    cwd: "/workspace",
    clientId: "tg-watchdog-cron",
    clientType: "tg-watchdog-cron",
    displayName: "TG Watchdog Cron",
  });
  await client.connect();
  return client;
}

async function findByName(client) {
  const list = (await client.listSchedules({})) || [];
  return list.find((s) => s.name === NAME || s.scheduleId === NAME);
}

async function install(client) {
  const payload = {
    name: NAME,
    cronPattern: CRON,
    timezone: TZ,
    prompt: PROMPT,
    workspaceRoot: "/workspace",
    cwd: "/workspace",
    modelSelection: {
      providerId: process.env.TG_BRIDGE_PROVIDER || "cline",
      modelId: process.env.TG_BRIDGE_MODEL || "cline-cloud/deepseek-v4.1-flash",
    },
    enabled: true,
    mode: "yolo",
    maxIterations: 8,
    timeoutSeconds: 180,
    maxParallel: 1,
    createdBy: "telegram-bridge",
    tags: ["telegram", "watchdog"],
    runtimeOptions: { mode: "yolo", enableTools: true, autoApproveTools: true, maxIterations: 8, timeoutSeconds: 180 },
  };
  const existing = await findByName(client);
  if (existing) {
    const updated = await client.updateSchedule(existing.scheduleId, payload);
    console.log(`已更新计划 ${existing.scheduleId}（${CRON} ${TZ}）`);
    console.log(JSON.stringify(updated ?? existing, null, 1));
    return existing.scheduleId;
  }
  const created = await client.createSchedule(payload);
  console.log(`已创建计划 ${created?.scheduleId}（${CRON} ${TZ}）`);
  console.log(JSON.stringify(created, null, 1));
  return created?.scheduleId;
}

async function main(argv) {
  const client = await connect();
  const idArg = argv.find((a) => a.startsWith("--id="));
  const idFromArg = idArg ? idArg.slice("--id=".length) : undefined;

  try {
    if (argv.includes("--install")) {
      await install(client);
    } else if (argv.includes("--list")) {
      const list = (await client.listSchedules({})) || [];
      if (!list.length) console.log("（没有任何计划）");
      for (const s of list) {
        console.log(`${s.scheduleId}\t${s.enabled ? "启用" : "暂停"}\t${s.cronPattern}\t${s.timezone || "-"}\t${s.name}`);
      }
    } else if (argv.includes("--run-now")) {
      const id = idFromArg || (await findByName(client))?.scheduleId;
      if (!id) throw new Error("找不到计划，先跑 --install");
      const exec = await client.triggerScheduleNow(id);
      console.log(`已触发 ${id}`);
      console.log(JSON.stringify(exec, null, 1));
    } else if (argv.includes("--executions")) {
      const id = idFromArg || (await findByName(client))?.scheduleId;
      if (!id) throw new Error("找不到计划");
      const runs = (await client.listScheduleExecutions(id, 5)) || [];
      if (!runs.length) console.log("（还没有执行记录）");
      for (const r of runs) console.log(JSON.stringify(r));
    } else if (argv.includes("--delete")) {
      const id = idFromArg || (await findByName(client))?.scheduleId;
      if (!id) throw new Error("找不到计划");
      console.log((await client.deleteSchedule(id)) ? `已删除 ${id}` : `删除失败 ${id}`);
    } else {
      console.log("用法: --install | --list | --run-now [--id=<id>] | --executions | --delete");
    }
  } finally {
    client.close();
  }
  return 0;
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code ?? 0;
  })
  .catch((err) => {
    console.error(`[fatal] ${err?.stack || err}`);
    process.exitCode = 1;
  });
