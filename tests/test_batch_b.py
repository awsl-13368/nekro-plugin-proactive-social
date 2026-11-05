"""批 B（C1/C2/C3/C4/C5/C7）的用例 + B6 接线守卫"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
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


class TestC1NormalizeKey(Base):
    def test_plain_qq(self):
        self.assertEqual(self.mod.normalize_chat_key("2293270861"),
                         "onebot_v11-private_2293270861")

    def test_already_full(self):
        full = "onebot_v11-private_2293270861"
        self.assertEqual(self.mod.normalize_chat_key(full), full)

    def test_other_protocol_untouched(self):
        key = "qq_official-private_123"
        self.assertEqual(self.mod.normalize_chat_key(key), key)

    def test_empty(self):
        self.assertEqual(self.mod.normalize_chat_key(""), "")


class TestC2ParseHM(Base):
    def test_valid(self):
        self.assertEqual(self.mod._parse_hm("23:30"), (23, 30))

    def test_out_of_range_falls_back(self):
        self.assertEqual(self.mod._parse_hm("25:00", "07:00"), (7, 0))

    def test_garbage_falls_back(self):
        self.assertEqual(self.mod._parse_hm("abc", "06:00"), (6, 0))

    def test_empty_uses_default(self):
        self.assertEqual(self.mod._parse_hm("", "00:30"), (0, 30))

    def test_in_quiet_does_not_raise(self):
        # 非法配置进 in_quiet 也不该抛异常
        self.assertIn(self.mod.in_quiet(datetime(2026, 11, 5, 12, 0)), (True, False))


class TestC3Cleanup(Base):
    def test_seeded_user_kept(self):
        self.mod.config.USER_RETENTION_DAYS = 7
        self.mod.config.CHAT_KEY = "onebot_v11-private_1"
        st = {"users": {}}
        seeded = self.mod.get_user(st, "onebot_v11-private_999")   # last_user_msg_ts = 0
        old = self.mod.get_user(st, "onebot_v11-private_888")
        old["last_user_msg_ts"] = 1e9 - 30 * 86400
        with patch.object(self.mod, "_now", return_value=1e9), \
             patch.object(self.mod, "days_since", lambda ts: (1e9 - ts) / 86400 if ts else 0.0):
            removed = self.mod.cleanup_users(st)
        self.assertEqual(removed, 1)
        self.assertIn("onebot_v11-private_999", st["users"], "从没说过话的播种用户不该被清")
        self.assertNotIn("onebot_v11-private_888", st["users"])

    def test_target_kept_via_normalized_key(self):
        self.mod.config.USER_RETENTION_DAYS = 7
        self.mod.config.CHAT_KEY = "1"          # 纯数字，归一化后是 onebot_v11-private_1
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["last_user_msg_ts"] = 1e9 - 30 * 86400
        with patch.object(self.mod, "_now", return_value=1e9), \
             patch.object(self.mod, "days_since", lambda ts: (1e9 - ts) / 86400 if ts else 0.0):
            self.mod.cleanup_users(st)
        self.assertIn("onebot_v11-private_1", st["users"], "目标私聊不该被清")


class TestC4OnOff(Base):
    def test_off_sets_flag(self):
        self.mod.config.ENABLE = True

        async def _run():
            return await self.mod.proactive_cmd(_Ctx(), "off")

        with patch.object(self.mod, "load_state", return_value={"users": {}}):
            out = asyncio.run(_run())
        self.assertFalse(self.mod.config.ENABLE)
        self.assertEqual(out[0], "success")

    def test_on_starts_scheduler_when_missing(self):
        self.mod.config.ENABLE = False
        created = []

        async def _fake_loop():
            return None

        with patch.object(self.mod, "load_state", return_value={"users": {}}), \
             patch.object(self.mod, "scheduler_loop", _fake_loop):
            self.mod._scheduler_task = None
            asyncio.run(self.mod.proactive_cmd(_Ctx(), "on"))
            created.append(self.mod._scheduler_task)
        self.assertTrue(self.mod.config.ENABLE)
        self.assertIsNotNone(created[0], "on 时调度器没在跑就该创建")


class TestC5Logs(Base):
    def test_diagnostic_logs_gated_by_debug(self):
        src = (REPO / "__init__.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if "诊断·" in line:
                # 诊断行必须在 if config.DEBUG 的下一行（缩进更深）
                idx = src.splitlines().index(line)
                prev = src.splitlines()[idx - 1].strip()
                self.assertEqual(prev, "if config.DEBUG:", f"诊断日志没被 DEBUG 包住: {line.strip()}")


class TestC7TypingTasks(Base):
    def test_show_typing_registers_task(self):
        self.mod.config.TYPING_ENABLED = True

        async def _run():
            self.mod.show_typing("onebot_v11-private_1", 0)
            await asyncio.sleep(0)

        asyncio.run(_run())
        self.assertIsInstance(self.mod._typing_tasks, set)


class TestB6Wiring(Base):
    def test_scheduler_calls_heartbeat_wait(self):
        src = (REPO / "__init__.py").read_text(encoding="utf-8")
        idx = src.index("async def scheduler_loop")
        seg = src[idx: idx + 4000]
        self.assertIn("heartbeat_wait(", seg, "scheduler 必须真的调用 heartbeat_wait（R25 抓到的漏接）")


class _Ctx:
    chat_key = "onebot_v11-private_1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
