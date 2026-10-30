#!/usr/bin/env python3
"""Telegram 24/7 客服 / AI 自动回复机器人（零依赖，仅标准库）。

特性:
  - 长轮询 + offset 持久化：重启不重复回复
  - FAQ 知识库匹配（data/faq.json），可选用 OpenAI 兼容接口做 AI 回复
  - /start /help /faq /id /reset 指令
  - 按 chat 限流、消息去重、白名单（TG_ALLOWED_CHATS）
  - 日志落盘（logs/tg-bot.log），SIGTERM/SIGINT 优雅退出

配置（.env 或环境变量）:
  TG_BOT_TOKEN      必填，BotFather 的 token
  TG_ALLOWED_CHATS  可选，逗号分隔的 chat_id 白名单；留空=所有人都能聊
  TG_RATE_LIMIT     可选，每个 chat 每分钟最多回复条数（默认 20）
  AI_BASE_URL       可选，OpenAI 兼容地址（如 https://api.openai.com/v1）
  AI_API_KEY        可选，AI 密钥；未配置则只用 FAQ
  AI_MODEL          可选，模型名（默认 gpt-4o-mini）
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tg import call, load_dotenv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.environ.get("TG_STATE_PATH", ROOT / "logs" / "tg_state.json"))
FAQ_PATH = Path(os.environ.get("TG_FAQ_PATH", ROOT / "data" / "faq.json"))
LOG_PATH = Path(os.environ.get("TG_LOG_PATH", ROOT / "logs" / "tg-bot.log"))

MAX_SEEN_IDS = 300
HISTORY_TURNS = 6
_running = True


def _stdout_is_log_file():
    """stdout 已被重定向到日志文件时（守护脚本），避免重复写入。"""
    try:
        out = os.fstat(sys.stdout.fileno())
        target = os.stat(LOG_PATH)
    except (OSError, ValueError, AttributeError):
        return False
    return (out.st_dev, out.st_ino) == (target.st_dev, target.st_ino)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _stdout_is_log_file():
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class _StopNow(Exception):
    """信号处理里抛出，用来立即打断阻塞中的长轮询。"""


def stop(*_args):
    global _running
    _running = False
    log("收到退出信号，准备结束 ...")
    raise _StopNow


# ---------------------------------------------------------------- 状态持久化
def default_state():
    return {"offset": None, "seen": [], "history": {}, "rate": {}}


def load_state():
    state = default_state()
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            state.update({k: data.get(k, state[k]) for k in state})
    except (OSError, json.JSONDecodeError):
        pass
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- 知识库
def load_faq():
    try:
        with open(FAQ_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[warn] 读取 FAQ 失败: {exc}")
        return []
    if isinstance(data, dict):
        data = data.get("items", [])
    items = []
    for item in data:
        if isinstance(item, dict) and item.get("answer"):
            items.append(
                {
                    "question": str(item.get("question", "")),
                    "keywords": [str(k).lower() for k in item.get("keywords", [])],
                    "answer": str(item["answer"]),
                }
            )
    return items


def match_faq(text, faq):
    """关键词打分；题目原文包含也算命中。返回 (answer, question) 或 None。"""
    if not text:
        return None
    lowered = text.lower()
    best = None
    best_score = 0
    for item in faq:
        score = 0
        for kw in item["keywords"]:
            if kw and kw in lowered:
                score += 2 if len(kw) > 1 else 1
        if item["question"] and item["question"].lower() in lowered:
            score += 3
        if score > best_score:
            best, best_score = item, score
    if not best:
        return None
    return best["answer"], best["question"]


# ---------------------------------------------------------------- 可选 AI
def ai_enabled():
    return bool(os.environ.get("AI_API_KEY") and os.environ.get("AI_BASE_URL"))


def ai_reply(text, history):
    base = os.environ["AI_BASE_URL"].rstrip("/")
    model = os.environ.get("AI_MODEL", "gpt-4o-mini")
    payload = {
        "model": model,
        "temperature": 0.3,
        "messages": [
            {
                "role": "system",
                "content": "你是一名简洁、礼貌的中文客服。回答控制在 3 句话内，"
                "不确定时请让用户联系人工客服，不要编造订单或价格信息。",
            }
        ]
        + history[-HISTORY_TURNS:]
        + [{"role": "user", "content": text}],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['AI_API_KEY']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        log(f"[warn] AI 回复失败，回退 FAQ: {exc}")
        return None


# ---------------------------------------------------------------- 限流 / 白名单
def allowed_by_rate(chat_id, state, limit, now=None):
    now = now or time.time()
    bucket = state["rate"].setdefault(str(chat_id), [])
    bucket[:] = [t for t in bucket if now - t < 60]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def whitelisted(chat_id):
    raw = os.environ.get("TG_ALLOWED_CHATS", "").strip()
    if not raw:
        return True
    allowed = {c.strip() for c in raw.split(",") if c.strip()}
    return str(chat_id) in allowed


# ---------------------------------------------------------------- 回复逻辑
GREETINGS = ("你好", "您好", "在吗", "在么", "hi", "hello", "嗨", "哈喽", "早上好", "晚上好", "有人吗")


def welcome_text(faq):
    lines = ["你好，我是自动客服机器人 🤖 24 小时在线。", "可以直接描述你的问题，也可以看下面这些常见问题："]
    lines += [f"· {item['question']}" for item in faq[:6]]
    lines.append("指令：/faq 常见问题 · /id 查看会话 ID · /reset 清空上下文")
    return "\n".join(lines)


def is_greeting(text):
    """中文问候用子串匹配，英文问候按单词边界匹配，避免 'this' 命中 'hi'。"""
    lowered = text.strip().lower()
    if not lowered:
        return False
    for word in ("hi", "hello", "hey"):
        if re.search(rf"\b{word}\b", lowered):
            return True
    return any(g in lowered for g in GREETINGS if not g.isascii())


def compose_reply(text, chat_id, state, faq):
    lowered = text.strip().lower()
    if lowered in ("/start", "/help") or is_greeting(lowered):
        return welcome_text(faq)
    if lowered == "/faq":
        if not faq:
            return "知识库暂时为空，请稍后再试或联系人工客服。"
        return "\n".join(
            f"{i}. {item['question']}\n   → {item['answer']}" for i, item in enumerate(faq, 1)
        )
    if lowered == "/id":
        return f"chat_id: {chat_id}"
    if lowered == "/reset":
        state["history"][str(chat_id)] = []
        return "上下文已清空，我们可以重新开始。"
    if "人工" in lowered:
        return "已记录你的需求，人工客服会尽快联系你。也可以留下联系方式加速处理。"

    if ai_enabled():
        answer = ai_reply(text, state["history"].get(str(chat_id), []))
        if answer:
            return answer

    hit = match_faq(text, faq)
    if hit:
        answer, question = hit
        return f"关于「{question}」：\n{answer}"
    return (
        "抱歉，这个问题我暂时没有答案。你可以换个说法，或输入 /faq 查看常见问题；"
        "需要人工客服请回复「人工」。"
    )


def remember(state, chat_id, user_text, bot_text):
    history = state["history"].setdefault(str(chat_id), [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": bot_text})
    del history[:-HISTORY_TURNS * 2]


def process_update(token, update, state, faq):
    """处理单条 update；返回是否真的发送了消息。"""
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    message_id = message.get("message_id")
    if chat_id is None or not text:
        return False

    seen_key = f"{update.get('update_id')}:{message_id}"
    if seen_key in state["seen"]:
        log(f"跳过重复消息 {seen_key}")
        return False
    state["seen"].append(seen_key)
    del state["seen"][:-MAX_SEEN_IDS]

    if not whitelisted(chat_id):
        log(f"忽略非白名单会话 chat={chat_id}")
        return False

    limit = int(os.environ.get("TG_RATE_LIMIT", "20") or 20)
    if not allowed_by_rate(chat_id, state, limit):
        log(f"限流命中 chat={chat_id}")
        return False

    reply = compose_reply(text, chat_id, state, faq)
    call(token, "sendMessage", {"chat_id": chat_id, "text": reply, "disable_web_page_preview": "true"})
    remember(state, chat_id, text, reply)
    log(f"已回复 chat={chat_id} <- {text[:40]!r}")
    return True


def poll_once(token, state, faq, poll_timeout=30):
    params = {"timeout": poll_timeout, "limit": 50}
    if state["offset"] is not None:
        params["offset"] = state["offset"]
    updates = call(token, "getUpdates", params, timeout=poll_timeout + 15)
    handled = 0
    for update in updates:
        state["offset"] = int(update["update_id"]) + 1
        if process_update(token, update, state, faq):
            handled += 1
    save_state(state)
    return handled


def run_forever(token, faq, poll_timeout):
    state = load_state()
    log(f"机器人启动，已处理消息 {len(state['seen'])} 条，FAQ={len(faq)} 条，AI={ai_enabled()}")
    while _running:
        try:
            handled = poll_once(token, state, faq, poll_timeout)
            if handled:
                log(f"本轮处理 {handled} 条")
        except _StopNow:
            log("长轮询被中断，立即退出")
            break
        except SystemExit as exc:  # tg.call 的错误
            log(f"[warn] 轮询失败: {exc}，5 秒后重试")
            save_state(state)
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001 - 常驻进程必须兜住所有异常
            log(f"[error] 未预期异常: {exc!r}，5 秒后重试")
            save_state(state)
            time.sleep(5)
    save_state(state)
    log("机器人已退出")


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description="Telegram 24/7 客服机器人")
    parser.add_argument("--token", help="bot token（默认读 TG_BOT_TOKEN / .env）")
    parser.add_argument("--poll-timeout", type=int, default=30)
    parser.add_argument("--once", action="store_true", help="只轮询一轮后退出（便于测试/定时任务）")
    parser.add_argument("--dry-run", action="store_true", help="不启动，仅自检配置与知识库")
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("TG_BOT_TOKEN")
    faq = load_faq()

    if args.dry_run:
        print(f"AI 启用: {ai_enabled()}")
        print(f"FAQ 条目: {len(faq)}")
        print(f"限流: {os.environ.get('TG_RATE_LIMIT', '20')}/分钟/chat")
        print(f"白名单: {os.environ.get('TG_ALLOWED_CHATS') or '（空=所有人）'}")
        print(f"token: {'已配置' if token else '缺失'}")
        return 0 if token else 1

    if not token:
        raise SystemExit("缺少 token：请在 .env 写入 TG_BOT_TOKEN（见 .env.example）")

    if args.once:
        state = load_state()
        handled = poll_once(token, state, faq, args.poll_timeout)
        log(f"单轮结束，处理 {handled} 条")
        return 0

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_forever(token, faq, args.poll_timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
