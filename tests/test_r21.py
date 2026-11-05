"""R21 要求的 4 个新用例：钩子侧字段不被覆盖、二次 commit 不重复计增量、
dry 不泄漏、_tick_start 之前写入的记忆不丢"""

from __future__ import annotations

import asyncio
import json
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

    def seed(self, **kw):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u.update(kw)
        self.mod.save_state(st)
        return u


class TestHookFieldsNotOverwritten(Base):
    """R21-1：钩子在 tick 期间写的字段，commit 不能被 tick 的旧值盖掉"""

    def test_hook_fields_survive(self):
        u = self.seed(tired=50.0, mood=50.0)
        disk = self.mod.load_state()
        du = disk["users"]["onebot_v11-private_1"]
        du["user_busy_until"] = 1e9 + 7200
        du["pings"] = 4
        du["replies"] = 3
        du["reply_rate"] = 0.75
        self.mod.save_state(disk)

        # tick 手里的旧 u 里这些字段还是 0/None
        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.commit(u, deltas={"tired": 1.0, "mood": 0.0}, tick_start_ts=1e9)

        got = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertEqual(got["user_busy_until"], 1e9 + 7200)
        self.assertEqual(got["pings"], 4)
        self.assertEqual(got["replies"], 3)
        self.assertAlmostEqual(got["reply_rate"], 0.75)

    def test_hook_cleared_counters_survive(self):
        u = self.seed(closer_count=2, no_reply_count=3, last_user_msg_ts=1e9 - 100)
        # 模拟：对方在 tick 期间说过话，钩子把计数清零
        disk = self.mod.load_state()
        du = disk["users"]["onebot_v11-private_1"]
        du["closer_count"] = 0
        du["no_reply_count"] = 0
        du["last_user_msg_ts"] = 1e9 + 5
        self.mod.save_state(disk)

        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.commit(u, deltas={"tired": 0.0, "mood": 0.0}, tick_start_ts=1e9)

        got = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertEqual(got["closer_count"], 0, "钩子刚清零，不能被 tick 旧值写回")
        self.assertEqual(got["no_reply_count"], 0)


class TestDeltaOncePerCommit(Base):
    """R21-2：同一 tick 里 commit 两次，增量只能算一次"""

    def test_double_commit_uses_reset_base(self):
        u = self.seed(tired=50.0, mood=50.0)
        base = {"t": 50.0, "m": 50.0}

        def commit_tick():
            self.mod.commit(
                u,
                deltas={"tired": float(u["tired"]) - base["t"], "mood": float(u["mood"]) - base["m"]},
                tick_start_ts=1e9,
            )
            base["t"] = float(u["tired"])
            base["m"] = float(u["mood"])

        with patch.object(self.mod, "_now", return_value=1e9):
            u["tired"] = 52.0        # 漂移 +2
            commit_tick()
            u["tired"] = 55.0        # 又 +3
            commit_tick()

        got = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertAlmostEqual(got["tired"], 55.0, places=3, msg="两次 commit 不该把增量算两遍")


class TestDryNoLeak(Base):
    """R21-4：dry 之后不应留下 _swept_changed，也不该改 pending"""

    def test_dry_keeps_pending_and_no_flag(self):
        self.seed()
        st = self.mod.load_state()
        u = st["users"]["onebot_v11-private_1"]
        u["pending"] = [{"text": "过期条子", "due_ts": 1e9 - 10 * 3600, "kind": "probe", "done": False}]
        u["last_user_msg_ts"] = 1e9 - 60
        self.mod.save_state(st)
        self.mod.config.CHAT_KEY = "onebot_v11-private_1"

        async def _run():
            return await self.mod.proactive_cmd(_Ctx(), "dry")

        with patch.object(self.mod, "_now", return_value=1e9), \
             patch.object(self.mod, "greeting_window", return_value=None):
            asyncio.run(_run())

        raw = json.loads(Path(self.mod.STATE_FILE).read_text(encoding="utf-8"))
        self.assertNotIn("_swept_changed", raw.get("users", {}).get("onebot_v11-private_1", {}))
        pend = raw["users"]["onebot_v11-private_1"].get("pending") or []
        self.assertEqual(len(pend), 1, "dry 不该把 pending 写没")
        self.assertFalse(pend[0].get("expired"), "dry 不该动 pending 的状态")


class TestMemoriesBeforeTickStart(Base):
    """R21-3：_tick_start 之前写入的记忆也不能丢（方案②：memories 归钩子）"""

    def test_memories_taken_from_disk(self):
        u = self.seed(memories=[])
        disk = self.mod.load_state()
        disk["users"]["onebot_v11-private_1"]["memories"] = [
            {"text": "早一点的记忆", "weight": 1.0, "ts": 1e9 - 300, "kind": "topic"}]
        self.mod.save_state(disk)

        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.commit(u, deltas={"tired": 0.0, "mood": 0.0}, tick_start_ts=1e9)

        texts = [m["text"] for m in self.mod.load_state()["users"]["onebot_v11-private_1"]["memories"]]
        self.assertIn("早一点的记忆", texts, "ts 早于 tick 开始的记忆也不该丢")


class _Ctx:
    chat_key = "onebot_v11-private_1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
