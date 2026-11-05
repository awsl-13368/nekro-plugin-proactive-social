"""A4 补完两块 + A8 固定扫描 + reset 计数清理 的用例"""

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

    def user(self, **kw):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u.update(kw)
        return st, u


class TestA4Deltas(Base):
    """tired/mood 要用"磁盘值 + 本轮增量"合成，不能拿旧值覆盖"""

    def test_tick_delta_survives_hook_write(self):
        st, u = self.user(tired=50.0, mood=50.0)
        self.mod.save_state(st)

        # 模拟：tick 拿到 u 之后，钩子在 await 期间把 tired 改成 44（-6）
        disk = self.mod.load_state()
        du = disk["users"]["onebot_v11-private_1"]
        du["tired"] = 44.0
        du["mood"] = 58.0
        self.mod.save_state(disk)

        # tick 自己漂移 +0.6（10 分钟白天），mood 不变
        u["tired"] = 50.6
        u["mood"] = 50.0
        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.commit(u, deltas={"tired": 0.6, "mood": 0.0}, tick_start_ts=1e9)

        got = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertAlmostEqual(got["tired"], 44.6, places=3, msg="钩子的 -6 应该保住，再加 tick 的 +0.6")
        self.assertAlmostEqual(got["mood"], 58.0, places=3)

    def test_memories_from_hook_survive(self):
        st, u = self.user(memories=[{"text": "旧事A", "weight": 1.0, "ts": 1e9 - 100, "kind": "topic"}])
        self.mod.save_state(st)

        # 钩子在 tick 期间新增一条（ts 晚于 tick 开始）
        disk = self.mod.load_state()
        disk["users"]["onebot_v11-private_1"]["memories"].append(
            {"text": "新事B", "weight": 1.0, "ts": 1e9 + 5, "kind": "topic"})
        self.mod.save_state(disk)

        with patch.object(self.mod, "_now", return_value=1e9 + 10):
            self.mod.commit(u, tick_start_ts=1e9)

        texts = [m["text"] for m in self.mod.load_state()["users"]["onebot_v11-private_1"]["memories"]]
        self.assertIn("新事B", texts, "tick 期间钩子新加的记忆不能被盖掉")
        self.assertIn("旧事A", texts)

    def test_forgotten_memory_stays_forgotten(self):
        """方案②：memories 归钩子。磁盘上没有的，commit 后也不能被内存里的旧副本复活"""
        st, u = self.user(memories=[
            {"text": "旧事C", "weight": 1.0, "ts": 1e9 - 1000, "kind": "topic"}])
        # 磁盘上是空的（钩子已经把它忘了）
        st["users"]["onebot_v11-private_1"]["memories"] = []
        self.mod.save_state(st)

        with patch.object(self.mod, "_now", return_value=1e9):
            self.mod.commit(u, tick_start_ts=1e9)

        texts = [m["text"] for m in self.mod.load_state()["users"]["onebot_v11-private_1"]["memories"]]
        self.assertNotIn("旧事C", texts, "磁盘已忘掉的记忆不该被 tick 的旧列表写回来")


class TestA8Sweep(Base):
    """过期扫描不依赖"有没有到期待办" """

    def test_sweep_marks_expired_even_without_due(self):
        st, u = self.user()
        u["pending"] = [
            {"text": "很久以前的追问", "due_ts": 1e9 - 10 * 3600, "kind": "probe", "done": False},
            {"text": "还没到期", "due_ts": 1e9 + 3600, "kind": "care", "done": False},
        ]
        with patch.object(self.mod, "_now", return_value=1e9):
            n = self.mod.sweep_expired(u)
        self.assertEqual(n, 1)
        expired = [p for p in u["pending"] if p.get("expired")]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["text"], "很久以前的追问")

    def test_sweep_no_change_returns_zero(self):
        st, u = self.user()
        u["pending"] = [{"text": "刚到期", "due_ts": 1e9 - 60, "kind": "care", "done": False}]
        with patch.object(self.mod, "_now", return_value=1e9):
            self.assertEqual(self.mod.sweep_expired(u), 0)


class TestResetCounts(Base):
    """reset 要清掉 miss_today / reflect_count / suppress_count / restraint_until"""

    def test_reset_clears_extra_counters(self):
        st, u = self.user(
            miss_today=2,
            reflect_count=3,
            suppress_count={"probe": 4},
            restraint_until=1e9 + 3600,
        )
        self.mod.save_state(st)
        self.mod.config.CHAT_KEY = "onebot_v11-private_1"

        async def _run():
            return await self.mod.proactive_cmd(_Ctx(), "reset")

        out = asyncio.run(_run())
        self.assertEqual(out[0], "success", f"reset 应成功: {out}")

        disk = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertEqual(int(disk.get("miss_today", 0)), 0)
        self.assertEqual(int(disk.get("reflect_count", 0)), 0)
        self.assertEqual(int(disk.get("restraint_until", 0)), 0)
        self.assertEqual(disk.get("suppress_count") or {}, {})


class _Ctx:
    chat_key = "onebot_v11-private_1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
