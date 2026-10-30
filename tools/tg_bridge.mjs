#!/usr/bin/env node
/**
 * Telegram ↔ Cline 桥：你在 Telegram 发消息，由本地 Cline Hub 拉起/续接一个 agent 会话回答，
 * 再把 agent 的回复发回 Telegram。
 *
 * 用法：
 *   node tools/tg_bridge.mjs --self-test        # 自检：建会话 + 问一句，验证 Hub 链路
 *   node tools/tg_bridge.mjs --simulate "你好"   # 只跑一轮并打印“会发给 TG 的内容”（不发送）
 *   node tools/tg_bridge.mjs [--once]           # 常驻 / 处理完积压后退出
 *
 * 环境变量（.env 或 shell）：
 *   TG_BOT_TOKEN / TG_ALLOWED_CHATS / TG_BRIDGE_PROVIDER / TG_BRIDGE_MODEL
 *   TG_BRIDGE_TOOLS=1 允许 agent 用工具（默认 0）；TG_BRIDGE_MODE 默认 act
 *   TG_HUB_TOKEN_URL 默认 http://127.0.0.1:8081/hub-token
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { HubSessionClient } from "/app/node_modules/@cline/core/dist/hub/index.js";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const STATE_PATH_DEFAULT = path.join(ROOT, "logs", "tg_bridge_state.json");
let STATE_PATH = process.env.TG_BRIDGE_STATE || STATE_PATH_DEFAULT;
const LOG_PATH = process.env.TG_BRIDGE_LOG || path.join(ROOT, "logs", "tg-bridge.log");
const TELEGRAM_API = process.env.TG_API_BASE || "https://api.telegram.org";
const MAX_LEN = 4000;
const TURN_TIMEOUT_MS = Number(process.env.TG_BRIDGE_TURN_TIMEOUT_MS || 300000);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 给 agent 回合加超时，避免一次卡死拖住整条消息循环 */
async function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

function stdoutIsLogFile() {
  try {
    const out = fs.fstatSync(1);
    const target = fs.statSync(LOG_PATH);
    return out.dev === target.dev && out.ino === target.ino;
  } catch {
    return false;
  }
}

function log(msg) {
  const stamp = new Date().toISOString().slice(0, 19).replace("T", " ");
  const line = `[${stamp}] ${msg}`;
  try {
    fs.writeSync(1, line + "\n"); // 同步写 stdout，避免退出时丢日志
  } catch {}
  if (stdoutIsLogFile()) return; // 守护脚本已把 stdout 重定向到日志文件，避免重复行
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    fs.appendFileSync(LOG_PATH, line + "\n");
  } catch {}
}

function loadDotenv() {
  const file = path.join(ROOT, ".env");
  if (!fs.existsSync(file)) return;
  const lines = fs.readFileSync(file, "utf8").split("\n");
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const idx = line.indexOf("=");
    const key = line.slice(0, idx).replace(/^export\s+/, "").trim();
    const value = line.slice(idx + 1).trim().replace(/^['"]|['"]$/g, "");
    if (key.startsWith("TG_") && !process.env[key]) process.env[key] = value;
  }
}

function loadState() {
  try {
    const data = JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
    return { offset: null, chats: {}, ...data };
  } catch {
    return { offset: null, chats: {} };
  }
}

function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
  fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 1));
}

function chunk(text) {
  const parts = [];
  let rest = text ?? "";
  while (rest.length > MAX_LEN) {
    let cut = rest.lastIndexOf("\n", MAX_LEN);
    if (cut < MAX_LEN / 2) cut = MAX_LEN;
    parts.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest) parts.push(rest);
  return parts.length ? parts : ["（空回复）"];
}

async function tg(method, params) {
  const res = await fetch(`${TELEGRAM_API}/bot${process.env.TG_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(`Telegram ${method} 失败 ${data.error_code}: ${data.description}`);
  return data.result;
}

async function sendText(chatId, text) {
  for (const part of chunk(text)) {
    await tg("sendMessage", { chat_id: String(chatId), text: part, disable_web_page_preview: "true" });
  }
}

function allowed(chatId) {
  const raw = (process.env.TG_ALLOWED_CHATS || "").trim();
  if (!raw) return true;
  return new Set(raw.split(",").map((s) => s.trim())).has(String(chatId));
}

function sessionConfig() {
  return {
    workspaceRoot: "/workspace",
    cwd: "/workspace",
    provider: process.env.TG_BRIDGE_PROVIDER || "cline",
    model: process.env.TG_BRIDGE_MODEL || "cline-cloud/deepseek-v4.1-flash",
    mode: process.env.TG_BRIDGE_MODE || "act",
    enableTools: process.env.TG_BRIDGE_TOOLS === "1",
    autoApproveTools: process.env.TG_BRIDGE_AUTO_APPROVE === "1",
    interactive: false,
    source: "telegram-bridge",
  };
}


async function connectHub() {
  const tokenUrl = process.env.TG_HUB_TOKEN_URL || "http://127.0.0.1:8081/hub-token";
  const res = await fetch(tokenUrl);
  if (!res.ok) throw new Error(`读取 Hub token 失败: HTTP ${res.status}`);
  const meta = await res.json();
  const address = String(meta.url).replace("0.0.0.0", "127.0.0.1");
  const client = new HubSessionClient({
    address,
    authToken: meta.authToken,
    workspaceRoot: "/workspace",
    cwd: "/workspace",
    clientId: "telegram-bridge",
    clientType: "telegram-bridge",
    displayName: "Telegram Bridge",
  });
  await client.connect();
  log(`已连接 Cline Hub: ${address}`);
  return client;
}

async function ensureSession(client, state, chatId) {
  const entry = state.chats[String(chatId)] || {};
  const config = sessionConfig();
  if (entry.sessionId) {
    try {
      const row = await client.getSession(entry.sessionId);
      if (row) return { sessionId: entry.sessionId, config };
    } catch (err) {
      log(`[warn] 旧会话不可用，将新建: ${err?.message || err}`);
    }
  }
  const started = await client.startRuntimeSession(config);
  state.chats[String(chatId)] = {
    sessionId: started.sessionId,
    turns: 0,
    createdAt: new Date().toISOString(),
  };
  saveState(state);
  log(`新建会话 ${started.sessionId} (chat=${chatId})`);
  return { sessionId: started.sessionId, config };
}

/** 标记「正在处理」的回合：进程中途被重启时，回复会丢，下个实例据此提示用户重发 */
function markPending(state, chatId, prompt) {
  const entry = state.chats[String(chatId)];
  if (!entry) return;
  entry.pending = { at: new Date().toISOString(), prompt: String(prompt).slice(0, 60) };
  saveState(state);
}

function clearPending(state, chatId) {
  const entry = state.chats[String(chatId)];
  if (!entry || !entry.pending) return;
  delete entry.pending;
  saveState(state);
}

/** 启动时补发提示：上次被打断的回合没有回复，明确告诉用户重发 */
async function recoverPending(state) {
  for (const [chatId, entry] of Object.entries(state.chats)) {
    if (!entry?.pending) continue;
    const at = entry.pending.at || "未知时间";
    delete entry.pending;
    saveState(state);
    log(`[warn] 发现被中断的回合 chat=${chatId} at=${at}，已提示用户重发`);
    await sendText(
      chatId,
      `⚠️ 上一次请求（${at}）处理到一半被重启打断了，那条回复没发出来。\n请重发一次；想清空上下文可以 /new。`,
    ).catch((err) => log(`[warn] 补发提示失败 chat=${chatId}: ${err?.message || err}`));
  }
}

async function ask(client, state, chatId, prompt) {
  const { sessionId, config } = await ensureSession(client, state, chatId);
  markPending(state, chatId, prompt);
  const turn = client.sendRuntimeSession(sessionId, { config, prompt }, { timeoutMs: TURN_TIMEOUT_MS });
  const out = await withTimeout(turn, TURN_TIMEOUT_MS, `agent 超过 ${TURN_TIMEOUT_MS / 1000}s 未返回`);
  const entry = state.chats[String(chatId)];
  if (entry) {
    entry.turns = (entry.turns || 0) + 1;
    entry.lastAt = new Date().toISOString();
  }
  clearPending(state, chatId);
  return out?.result?.text ?? "（agent 没有返回文本）";
}

const HELP_TEXT = () =>
  "我在。直接把需求发给我，我会在这台机器上干活（读写文件、跑命令、提交代码）。\n\n" +
  "指令：/new 开新会话 · /status 查看会话 · /help 帮助\n" +
  `当前模型：${sessionConfig().model}`;

async function handleMessage(client, state, message) {
  const chatId = message?.chat?.id;
  const text = (message?.text || "").trim();
  if (chatId == null || !text) return;
  if (!allowed(chatId)) {
    log(`忽略非白名单 chat=${chatId}`);
    return;
  }
  log(`收到 chat=${chatId} text=${JSON.stringify(text.slice(0, 60))}`);

  if (text === "/new") {
    delete state.chats[String(chatId)];
    saveState(state);
    await sendText(chatId, "已开启新会话，之前的上下文清空了。");
    return;
  }
  if (text === "/help" || text === "/start") {
    await sendText(chatId, HELP_TEXT());
    return;
  }
  if (text === "/status") {
    const entry = state.chats[String(chatId)];
    await sendText(
      chatId,
      entry
        ? `会话 ${entry.sessionId}\n轮次 ${entry.turns || 0}\n最近 ${entry.lastAt || "-"}`
        : "还没有会话，发一条消息就会创建。",
    );
    return;
  }

  await tg("sendChatAction", { chat_id: String(chatId), action: "typing" }).catch(() => {});
  const started = Date.now();
  const reply = await ask(client, state, chatId, text);
  await sendText(chatId, reply);
  log(`已回复 chat=${chatId} 用时 ${((Date.now() - started) / 1000).toFixed(1)}s len=${reply.length}`);
}

async function pollLoop(client, { once = false, pollTimeout = 30 } = {}) {
  const state = loadState();
  const cfg = sessionConfig();
  log(`桥接启动：provider=${cfg.provider} model=${cfg.model} tools=${cfg.enableTools}`);
  await recoverPending(state);
  for (;;) {
    const params = { timeout: pollTimeout, limit: 20 };
    if (state.offset != null) params.offset = state.offset;
    let updates;
    try {
      updates = await tg("getUpdates", params);
    } catch (err) {
      log(`[warn] getUpdates 失败: ${err.message}，5 秒后重试`);
      await sleep(5000);
      continue;
    }
    for (const update of updates) {
      state.offset = Number(update.update_id) + 1;
      saveState(state);
      try {
        await handleMessage(client, state, update.message || update.edited_message);
      } catch (err) {
        log(`[error] 处理消息失败: ${err?.stack || err}`);
        const chatId = (update.message || {}).chat?.id;
        if (chatId != null) await sendText(chatId, `出错了：${err?.message || err}`).catch(() => {});
      }
    }
    if (once && updates.length === 0) break;
  }
}

async function main(argv) {
  loadDotenv();
  if (!process.env.TG_BOT_TOKEN) {
    log("缺少 TG_BOT_TOKEN：请在 .env 里填写（见 .env.example）");
    return 1;
  }
  const client = await connectHub();

  if (argv.includes("--self-test")) {
    const reply = await ask(client, { offset: null, chats: {} }, "self-test", "只回复两个字：pong");
    log(`self-test 回复: ${JSON.stringify(reply)}`);
    client.close();
    return reply.toLowerCase().includes("pong") ? 0 : 1;
  }

  const simIdx = argv.indexOf("--simulate");
  if (simIdx !== -1) {
    const prompt = argv[simIdx + 1] || "你好";
    const reply = await ask(client, { offset: null, chats: {} }, "simulate", prompt);
    fs.writeSync(1, `---- 会发给 Telegram 的内容 ----\n${reply}\n`);
    client.close();
    return 0;
  }

  const replayIdx = argv.indexOf("--replay");
  if (replayIdx !== -1) {
    // node tools/tg_bridge.mjs --replay <chatId> <text>：走真实链路（Hub → agent → Telegram 发送）
    // 用独立状态文件，避免污染常驻桥的 offset / 会话映射
    STATE_PATH = path.join(ROOT, "logs", "tg_bridge_replay_state.json");
    const chatId = argv[replayIdx + 1];
    const text = argv.slice(replayIdx + 2).join(" ") || "你好";
    const state = { offset: null, chats: {} };
    await handleMessage(client, state, { chat: { id: chatId }, text });
    client.close();
    return 0;
  }

  await pollLoop(client, { once: argv.includes("--once") });
  client.close();
  return 0;
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code ?? 0;
  })
  .catch((err) => {
    log(`[fatal] ${err?.stack || err}`);
    process.exitCode = 1;
  });

  `当前模型：${sessionConfig().model}`;
