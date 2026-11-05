"""R22 的修复：reflect_at 不能被 max 顶回去（连续多 tick 只反省一次）"""

from __future__ import annotations

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


class TestReflectNotStuck(Base):
    def test_reflect_cleared_stays_cleared_over_ticks(self):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["reflect_at"] = 1e9 - 3600      # 一小时前发过，待反省
        u["tired"] = 40.0
        u["mood"] = 60.0
        self.mod.save_state(st)
        self.mod.config.REFLECT_ENABLED = True
        self.mod.config.REFLECT_AFTER_MINUTES = 1

        mood_after = []
        for i in range(3):
            # 每个 tick：重新读盘 → 反省 → 按"内存值 - 本轮基准"算增量提交
            fresh = self.mod.load_state()
            nu = self.mod.get_user(fresh, "onebot_v11-private_1")
            base_t = float(nu.get("tired", 20.0))
            base_m = float(nu.get("mood", 60.0))
            with patch.object(self.mod, "_now", return_value=1e9 + i):
                self.mod.run_reflection(nu)
                self.mod.commit(
                    nu,
                    deltas={"tired": float(nu["tired"]) - base_t, "mood": float(nu["mood"]) - base_m},
                    tick_start_ts=1e9 + i,
                )
            mood_after.append(self.mod.load_state()["users"]["onebot_v11-private_1"]["mood"])
            self.assertEqual(
                float(self.mod.load_state()["users"]["onebot_v11-private_1"].get("reflect_at", 0) or 0),
                0.0,
                f"第 {i + 1} 轮后 reflect_at 应该保持 0，不能被磁盘旧值顶回去",
            )

        # 只该反省一次：mood 从 60 → 48，后面两轮不该再掉
        self.assertAlmostEqual(mood_after[0], 48.0, places=3)
        self.assertEqual(mood_after[1], mood_after[0], "第二轮不该再扣 mood")
        self.assertEqual(mood_after[2], mood_after[0])


class TestStatusGateLine(Base):
    def test_status_shows_no_model_state(self):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        self.mod.save_state(st)
        self.mod.config.LLM_GATE = True
        self.mod.config.CHAT_KEY = "onebot_v11-private_1"

        import asyncio

        async def _run():
            return await self.mod.proactive_cmd(_Ctx(), "status")

        with patch.object(self.mod, "_model_args", return_value={}):
            out = asyncio.run(_run())
        text = str(out)
        self.assertIn("无可用模型", text, "没有模型时 status 的闸门行要看得出来")
        self.assertIn("当前时间", text)


class _Ctx:
    chat_key = "onebot_v11-private_1"


if __name__ == "__main__":
    unittest.main(verbosity=2)
