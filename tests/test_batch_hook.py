"""钩子相关项的用例：A7 / A8 / B1 / B2 / B7 / C6"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from nekro_stub import load_plugin  # noqa: E402


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.mod = load_plugin(REPO, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        d = Path(self._tmp.name)
        for p in d.iterdir():
            if p.is_file():
                p.unlink()


class TestB1Negation(Base):
    def test_busy_negation(self):
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["tired"] = 20.0
        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.note_busy(u, "今天不累")
            self.assertAlmostEqual(u["tired"], 20.0)
            self.assertEqual(float(u.get("user_busy_until", 0) or 0), 0.0)
            self.mod.note_busy(u, "好累啊")
            self.assertGreater(u["tired"], 20.0)
            self.assertGreater(float(u.get("user_busy_until", 0) or 0), 0.0)

    def test_care_negation(self):
        self.assertIsNone(self.mod.detect_care("不头疼了"))
        self.assertIsNone(self.mod.detect_care("没发烧"))
        self.assertEqual(self.mod.detect_care("头疼"), "头疼")


class TestB2Perfunctory(Base):
    def test_short_not_always_perfunctory(self):
        self.assertFalse(self.mod.is_perfunctory("救命"))
        self.assertFalse(self.mod.is_perfunctory("别走"))
        self.assertFalse(self.mod.is_perfunctory(""))
        self.assertTrue(self.mod.is_perfunctory("嗯"))
        self.assertTrue(self.mod.is_perfunctory("好的"))
        self.assertTrue(self.mod.is_perfunctory("..."))

    def test_digits_not_filler(self):
        self.assertFalse(self.mod.is_perfunctory("123"))


class TestB7MemoryWords(Base):
    def test_important_words_narrowed(self):
        self.assertAlmostEqual(self.mod.memory_weight("明天面试"), 1.8)
        self.assertAlmostEqual(self.mod.memory_weight("我真的很喜欢吃面条啊"), 1.0)

    def test_good_words_cooldown(self):
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["tired"] = 50.0
        u["mood"] = 50.0
        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.note_busy(u, "睡饱了")
            t1, m1 = u["tired"], u["mood"]
            self.mod.note_busy(u, "睡饱了")
            self.assertEqual((u["tired"], u["mood"]), (t1, m1), "30 分钟内只该生效一次")


class TestA8Pending(Base):
    def test_queue_dedupes_same_kind(self):
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.queue_pending(u, {"text": "A", "due_ts": 1e9 + 60, "kind": "probe", "done": False})
            self.mod.queue_pending(u, {"text": "B", "due_ts": 1e9 + 120, "kind": "probe", "done": False})
        pend = [p for p in u["pending"] if p.get("kind") == "probe"]
        self.assertEqual(len(pend), 1, "同 kind 未完成只该留一条")
        self.assertEqual(pend[0]["text"], "B", "新的应刷新文本")
        self.assertEqual(pend[0]["due_ts"], 1e9 + 120)

    def test_priority_and_expiry(self):
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["pending"] = [
            {"text": "过期的追问", "due_ts": 1e9 - 10 * 3600, "kind": "probe", "done": False},
            {"text": "关怀", "due_ts": 1e9 - 60, "kind": "care", "done": False},
        ]
        u["last_user_msg_ts"] = 1e9 - 60
        u["last_followup_ts"] = 0
        with patch.object(self.mod, "_now", return_value=1e9), \
             patch.object(self.mod, "greeting_window", return_value=None):
            got = asyncio.run(self.mod.derive_reason(u, []))
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "care", "过期的 probe 应被丢弃，选出 care")
        expired = [p for p in u["pending"] if p.get("kind") == "probe" and p.get("expired")]
        self.assertEqual(len(expired), 1)


class TestC6Topics(Base):
    def test_blocked_topic_not_stored(self):
        self.assertFalse(self.mod.memory_allowed("约炮 赌博"))
        self.assertTrue(self.mod.memory_allowed("今天吃了拉面"))


class _Msg:
    def __init__(self, text):
        self.content_text = text


class _Ctx:
    def __init__(self, chat_key):
        self.chat_key = chat_key


class TestA7TimeBase(Base):
    """A7：接话的时间基准应该是"她最近一条发言"，不是最后一次主动消息"""

    def _run_hook(self, now, bot_ts, text):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["last_bot_msg_ts"] = 1e9 - 3 * 86400      # 三天前的主动消息
        self.mod.save_state(st)

        async def _fake_recent(chat_key, limit=16):
            return [
                {"is_bot": True, "text": "她在说话", "ts": bot_ts},
                {"is_bot": False, "text": text, "ts": now},
            ]

        with patch.object(self.mod, "_now", return_value=now), \
             patch.object(self.mod, "fetch_recent", _fake_recent):
            asyncio.run(self.mod.on_user_message(_Ctx("onebot_v11-private_1"), _Msg(text)))
        return self.mod.load_state()["users"]["onebot_v11-private_1"]

    def test_no_followup_after_long_absence(self):
        now = 1e9
        u = self._run_hook(now, bot_ts=now - 2 * 86400, text="早")
        kinds = [p.get("kind") for p in (u.get("pending") or [])]
        self.assertNotIn("followup", kinds, "两天没说话，不该排'接着话头'")

    def test_followup_within_window(self):
        now = 1e9
        u = self._run_hook(now, bot_ts=now - 40 * 60, text="我回来了，今天挺累的")
        kinds = [p.get("kind") for p in (u.get("pending") or [])]
        self.assertIn("followup", kinds, "她 40 分钟前说过话，应排接话")

    def test_probe_requires_recent_speech(self):
        now = 1e9
        u = self._run_hook(now, bot_ts=now - 5 * 60, text="嗯")
        kinds = [p.get("kind") for p in (u.get("pending") or [])]
        self.assertIn("probe", kinds, "她 5 分钟前说话 + 对方敷衍，应排追问")


if __name__ == "__main__":
    unittest.main(verbosity=2)
