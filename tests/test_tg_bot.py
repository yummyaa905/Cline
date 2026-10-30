#!/usr/bin/env python3
"""tg_bot.py 的自动化测试：用本地假 Telegram API 跑通端到端流程。

运行: python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

TMP = tempfile.mkdtemp(prefix="tgbot-test-")
os.environ["TG_BOT_TOKEN"] = "123456:TEST"
os.environ["TG_STATE_PATH"] = os.path.join(TMP, "state.json")
os.environ["TG_LOG_PATH"] = os.path.join(TMP, "bot.log")
os.environ["TG_FAQ_PATH"] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "faq.json")
os.environ.pop("AI_API_KEY", None)
os.environ.pop("AI_BASE_URL", None)
os.environ.pop("TG_ALLOWED_CHATS", None)
os.environ.pop("TG_RATE_LIMIT", None)


class FakeTelegram(BaseHTTPRequestHandler):
    updates = []
    sent = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        params = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        method = self.path.rsplit("/", 1)[-1]

        if method == "getUpdates":
            result = list(FakeTelegram.updates)
        elif method == "sendMessage":
            FakeTelegram.sent.append(params)
            result = {"message_id": len(FakeTelegram.sent), "chat": {"id": params.get("chat_id")}}
        elif method == "getMe":
            result = {"id": 123456, "username": "test_bot", "is_bot": True}
        else:
            result = {}

        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def setUpModule():
    global SERVER, THREAD
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
    THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
    THREAD.start()
    os.environ["TG_API_BASE"] = f"http://127.0.0.1:{SERVER.server_address[1]}"


def tearDownModule():
    SERVER.shutdown()
    SERVER.server_close()
    shutil.rmtree(TMP, ignore_errors=True)


def make_update(update_id, chat_id, text, message_id=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "username": "tester"},
            "text": text,
        },
    }


class TGBotTest(unittest.TestCase):
    def setUp(self):
        import tg_bot

        self.bot = tg_bot
        FakeTelegram.updates = []
        FakeTelegram.sent = []
        if os.path.exists(os.environ["TG_STATE_PATH"]):
            os.remove(os.environ["TG_STATE_PATH"])
        for key in ("TG_ALLOWED_CHATS", "TG_RATE_LIMIT"):
            os.environ.pop(key, None)
        self.faq = tg_bot.load_faq()

    def run_once(self):
        self.assertEqual(self.bot.main(["--once", "--poll-timeout", "0"]), 0)
        return FakeTelegram.sent

    def test_faq_loaded(self):
        self.assertEqual(len(self.faq), 6)
        self.assertTrue(self.faq[0]["keywords"])

    def test_start_command_lists_questions(self):
        reply = self.bot.compose_reply("/start", 1, self.bot.default_state(), self.faq)
        self.assertIn("常见问题", reply)
        self.assertIn(self.faq[0]["question"], reply)

    def test_match_faq_shipping(self):
        hit = self.bot.match_faq("你们多久发货呀？", self.faq)
        self.assertIsNotNone(hit)
        self.assertIn("24 小时", hit[0])

    def test_fallback_when_no_match(self):
        reply = self.bot.compose_reply("完全无关的内容 zzz", 1, self.bot.default_state(), self.faq)
        self.assertIn("/faq", reply)

    def test_end_to_end_reply_and_offset(self):
        FakeTelegram.updates = [make_update(1001, 555, "什么时候发货")]
        sent = self.run_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "555")
        self.assertIn("发货", sent[0]["text"])

        with open(os.environ["TG_STATE_PATH"], encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["offset"], 1002)
        self.assertEqual(len(state["history"]["555"]), 2)

    def test_duplicate_message_not_replied_twice(self):
        FakeTelegram.updates = [make_update(2001, 556, "怎么退货")]
        self.assertEqual(len(self.run_once()), 1)
        # 抹掉 offset 模拟服务端重发同一条 update
        with open(os.environ["TG_STATE_PATH"], encoding="utf-8") as fh:
            state = json.load(fh)
        state["offset"] = None
        with open(os.environ["TG_STATE_PATH"], "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        before = len(FakeTelegram.sent)
        self.run_once()
        self.assertEqual(len(FakeTelegram.sent), before)

    def test_whitelist_blocks_unknown_chat(self):
        os.environ["TG_ALLOWED_CHATS"] = "999"
        FakeTelegram.updates = [make_update(3001, 111, "优惠券")]
        self.assertEqual(len(self.run_once()), 0)

    def test_rate_limit(self):
        os.environ["TG_RATE_LIMIT"] = "2"
        FakeTelegram.updates = [make_update(4000 + i, 777, "有优惠吗") for i in range(4)]
        self.assertEqual(len(self.run_once()), 2)

    def test_no_token_exits_with_message(self):
        os.environ["TG_BOT_TOKEN"] = ""
        try:
            with self.assertRaises(SystemExit) as ctx:
                self.bot.main(["--once"])
            self.assertIn("缺少 token", str(ctx.exception))
        finally:
            os.environ["TG_BOT_TOKEN"] = "123456:TEST"

    def test_dry_run_reports_config(self):
        self.assertEqual(self.bot.main(["--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
