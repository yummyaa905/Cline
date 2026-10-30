#!/usr/bin/env python3
"""Telegram Bot API 轻量客户端（零依赖，仅用标准库）。

用法:
  export TG_BOT_TOKEN=123456:ABC...
  python3 tools/tg.py me                       # 校验 token
  python3 tools/tg.py send --chat <chat_id> --text "hello"
  python3 tools/tg.py updates                  # 拉取最近消息（一次性）
  python3 tools/tg.py listen --chat <chat_id>  # 常驻轮询，收到消息就打印

说明:
  - token 只从环境变量 TG_BOT_TOKEN 或 --token 读取，不要写进仓库。
  - 需要网络可达 https://api.telegram.org（本沙箱直连已验证可达）。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("TG_API_BASE", "https://api.telegram.org")


def call(token, method, params=None, timeout=30):
    url = f"{API_BASE}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise SystemExit(f"HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络不可达: {exc.reason}") from exc

    if not payload.get("ok"):
        raise SystemExit(
            f"Telegram API 返回错误 {payload.get('error_code')}: {payload.get('description')}"
        )
    return payload["result"]


def resolve_token(args):
    token = args.token or os.environ.get("TG_BOT_TOKEN")
    if not token:
        raise SystemExit("缺少 token：请设置环境变量 TG_BOT_TOKEN，或传入 --token")
    return token


def cmd_me(args):
    me = call(resolve_token(args), "getMe")
    print(json.dumps(me, ensure_ascii=False, indent=2))


def cmd_send(args):
    result = call(
        resolve_token(args),
        "sendMessage",
        {
            "chat_id": args.chat,
            "text": args.text,
            "disable_web_page_preview": "true" if args.no_preview else "false",
        },
    )
    print(f"已发送 message_id={result.get('message_id')} -> chat={result.get('chat', {}).get('id')}")


def cmd_updates(args):
    params = {"timeout": 0, "limit": args.limit}
    if args.offset is not None:
        params["offset"] = args.offset
    updates = call(resolve_token(args), "getUpdates", params)
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = msg.get("text", "")
        chat_id = (msg.get("chat") or {}).get("id")
        sender = (msg.get("from") or {}).get("username")
        print(f"update_id={upd['update_id']} chat={chat_id} from={sender} text={text!r}")
    print(f"-- 共 {len(updates)} 条 --")


def cmd_listen(args):
    token = resolve_token(args)
    offset = None
    print(f"开始常驻轮询（chat={args.chat or '任意'}），Ctrl-C 退出 ...", flush=True)
    while True:
        params = {"timeout": args.poll_timeout, "limit": 50}
        if offset is not None:
            params["offset"] = offset
        try:
            updates = call(token, "getUpdates", params, timeout=args.poll_timeout + 15)
        except SystemExit as exc:
            print(f"[warn] {exc}，5 秒后重试", flush=True)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text", "")
            if args.chat and str(chat_id) != str(args.chat):
                continue
            print(f"[{time.strftime('%H:%M:%S')}] chat={chat_id} text={text!r}", flush=True)
            if args.echo and chat_id is not None:
                call(token, "sendMessage", {"chat_id": chat_id, "text": f"echo: {text}"})


def build_parser():
    p = argparse.ArgumentParser(description="Telegram Bot API 轻量客户端")
    p.add_argument("--token", help="bot token（默认读环境变量 TG_BOT_TOKEN）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("me", help="校验 token 并打印 bot 信息").set_defaults(func=cmd_me)

    s = sub.add_parser("send", help="发送文本消息")
    s.add_argument("--chat", required=True, help="chat_id 或 @channel")
    s.add_argument("--text", required=True)
    s.add_argument("--no-preview", action="store_true", help="关闭链接预览")
    s.set_defaults(func=cmd_send)

    u = sub.add_parser("updates", help="一次性拉取消息")
    u.add_argument("--limit", type=int, default=20)
    u.add_argument("--offset", type=int)
    u.set_defaults(func=cmd_updates)

    l = sub.add_parser("listen", help="常驻长轮询")
    l.add_argument("--chat", help="只关心这个 chat_id")
    l.add_argument("--poll-timeout", type=int, default=30)
    l.add_argument("--echo", action="store_true", help="把收到的消息回显给发送者")
    l.set_defaults(func=cmd_listen)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
