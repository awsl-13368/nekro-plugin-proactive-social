"""R28（4.2.0）：群聊心流已整体移除，只做私聊"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from nekro_stub import load_plugin  # noqa: E402


class _Ctx:
    def __init__(self, chat_key):
        self.chat_key = chat_key


class _Msg:
    def __init__(self, text):
        self.content_text = text


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.mod = load_plugin(REPO, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class TestGroupRemoved(Base):
    def test_no_group_config_fields(self):
        leftovers = [n for n in dir(self.mod.ProactiveConfig) if n.startswith("GROUP_")]
        self.assertEqual(leftovers, [], "GROUP_* 配置应已全部删除")

    def test_no_group_helpers(self):
        for fn in (
            "group_flow_tick",
            "send_group_flow",
            "group_gate",
            "get_group",
            "group_allowed",
            "flush_groups",
            "commit_groups",
        ):
            self.assertFalse(hasattr(self.mod, fn), f"{fn} 应已删除")

    def test_legacy_groups_key_dropped(self):
        self.mod.save_state({"users": {}, "groups": {"onebot_v11-group_1": {"last_msg_ts": 1}}})
        st = self.mod.load_state()
        self.assertNotIn("groups", st, "旧状态里的 groups 残留应在加载时清掉")

    def test_group_message_writes_nothing(self):
        self.mod.save_state({"users": {}, "last_chat_key": ""})
        before = self.mod.load_state()
        asyncio.run(self.mod.on_user_message(_Ctx("onebot_v11-group_12345"), _Msg("大家好")))
        after = self.mod.load_state()
        self.assertEqual(before, after, "群消息不应触发任何状态写入")
