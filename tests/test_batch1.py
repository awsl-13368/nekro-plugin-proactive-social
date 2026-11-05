"""批 1 已改项的单元测试（A1 / A2 / A3 / A5 / A6）。

跑法（Claude 那边）：
    cd F:\\GitHub-Backup\\nekro-plugin-proactive-social
    G:\\NekroAgent\\python\\python.exe -m unittest discover -s tests -v

注意：这里只用 unittest，不引入第三方依赖；pydantic 由 nekro_stub 提供替身。
"""

from __future__ import annotations

import asyncio
import json
import random
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
        # 每个用例前清空状态目录
        d = Path(self._tmp.name)
        for p in d.iterdir():
            if p.is_file():
                p.unlink()
        self.mod.config.ENABLE = True
        self.mod.config.SUPPRESS_ENABLED = True

    def fresh_user(self):
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        return st, u


class TestA1Tired(Base):
    """A1：疲惫值不能只涨不落；单次推进要封顶

    按 R20 要求：不替换 in_quiet，只替换 datetime；心跳用 random.Random(1)；
    所有替换走 mock.patch.object，测完自动还原。
    """

    def test_minutes_capped_at_60(self):
        st, u = self.fresh_user()
        u["tired"] = 20.0
        u["last_user_msg_ts"] = 1e9
        with patch.object(self.mod, "datetime", _fake_dt(14)), \
             patch.object(self.mod, "_now", return_value=1e9):
            self.mod.drift_mood(u, 10_000)
        self.assertLessEqual(u["tired"], 20.0 + 0.01 * 60 + 1e-6)

    def test_quiet_recovers(self):
        st, u = self.fresh_user()
        u["tired"] = 80.0
        u["last_user_msg_ts"] = 1e9
        with patch.object(self.mod, "datetime", _fake_dt(3)), \
             patch.object(self.mod, "_now", return_value=1e9):
            self.mod.drift_mood(u, 60)          # 03:00 在安静时段内
        self.assertAlmostEqual(u["tired"], 80.0 - 0.05 * 60, places=6)

    def test_seven_days_idle_peaks(self):
        """7 天、每次心跳 8~30 分钟（seed=1），对方全程不说话"""
        st, u = self.fresh_user()
        u["tired"] = 20.0
        u["last_user_msg_ts"] = 1e9

        rng = random.Random(1)
        clock = {"ts": 1e9}
        day_start = datetime(2026, 11, 5, 0, 0, 0)

        def _now():
            return clock["ts"]

        class _DT:
            @staticmethod
            def now():
                return datetime.fromtimestamp(clock["ts"])

        peaks = []
        end_of_day = []
        with patch.object(self.mod, "_now", _now), patch.object(self.mod, "datetime", _DT):
            for day in range(7):
                peak = 0.0
                while True:
                    clock["ts"] += rng.randint(8, 30) * 60
                    elapsed_day = (clock["ts"] - day_start.timestamp()) / 86400.0
                    if elapsed_day > day + 1:
                        break
                    self.mod.drift_mood(u, rng.randint(8, 30))
                    peak = max(peak, u["tired"])
                peaks.append(round(peak, 1))
                end_of_day.append(round(u["tired"], 1))

        self.assertLess(max(peaks[-2:]), 35.0, f"最后两天峰值过高: {peaks}")
        self.assertLess(end_of_day[-1], 25.0, f"第 7 天末值过高: {end_of_day}")


class TestA2Greeting(Base):
    """A2：问候不能"派发即消费"，要发送成功才记账"""

    def test_greeting_not_consumed_on_derive(self):
        st, u = self.fresh_user()
        u["last_user_msg_ts"] = 1e9 - 13 * 3600
        self.mod.config.GREETING_ENABLED = True
        self.mod.config.GREETING_GAP_HOURS = 6
        self.mod.greeting_window = lambda now=None: "morning"
        self.mod._now = lambda: 1e9
        got = asyncio.run(self.mod.derive_reason(u, [], dry=True))
        self.assertIsNotNone(got)
        reason, kind, ref = got
        self.assertEqual(kind, "greeting")
        self.assertEqual(ref, {"win": "morning"})
        self.assertEqual(u.get("greeted") or {}, {}, "派由头时不该写 greeted")

    def test_dry_does_not_touch_loop_check(self):
        st, u = self.fresh_user()
        u["last_user_msg_ts"] = 1e9 - 3600
        u["last_loop_check_ts"] = 0
        self.mod.config.LOOP_ENABLED = True
        self.mod._now = lambda: 1e9

        async def _no_topic(*a, **k):
            return None

        self.mod.extract_unresolved = _no_topic
        asyncio.run(self.mod.derive_reason(u, [], dry=True))
        self.assertEqual(float(u.get("last_loop_check_ts", 0) or 0), 0.0)


class TestA3Counters(Base):
    """A3：反省不能动 closer_count"""

    def test_reflection_keeps_closer_count(self):
        st, u = self.fresh_user()
        u["reflect_at"] = 1e9 - 3600
        u["tired"] = 40.0
        u["mood"] = 50.0
        self.mod.config.REFLECT_ENABLED = True
        self.mod.config.REFLECT_AFTER_MINUTES = 1
        self.mod._now = lambda: 1e9
        self.mod.run_reflection(u)
        self.assertEqual(int(u.get("closer_count", 0)), 0)
        self.assertEqual(int(u.get("no_reply_count", 0)), 1)

    def test_reply_clears_both(self):
        st, u = self.fresh_user()
        u["reflect_at"] = 1e9 - 3600
        u["no_reply_count"] = 3
        u["closer_count"] = 2
        u["last_user_msg_ts"] = 1e9              # 回过了
        self.mod.config.REFLECT_ENABLED = True
        self.mod.config.REFLECT_AFTER_MINUTES = 1
        self.mod._now = lambda: 1e9
        self.mod.run_reflection(u)
        self.assertEqual(int(u.get("no_reply_count", 0)), 0)
        self.assertEqual(int(u.get("closer_count", 0)), 2, "回话不该把已发的收场次数抹掉")


class TestA5AtomicState(Base):
    """A5：原子写 + 坏文件隔离 + 回退 .bak"""

    def test_save_creates_file_and_bak(self):
        st1 = {"users": {"k": {"chat_key": "k", "n": 1}}}
        self.mod.save_state(st1)
        self.assertTrue(Path(self.mod.STATE_FILE).exists())
        st2 = {"users": {"k": {"chat_key": "k", "n": 2}}}
        self.mod.save_state(st2)
        self.assertTrue(Path(self.mod.STATE_BAK).exists(), "第二次保存前应留下 .bak")
        bak = json.loads(Path(self.mod.STATE_BAK).read_text(encoding="utf-8"))
        self.assertEqual(bak["users"]["k"]["n"], 1, ".bak 应该是上一次的完整内容")
        cur = json.loads(Path(self.mod.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(cur["users"]["k"]["n"], 2)

    def test_no_tmp_left(self):
        self.mod.save_state({"users": {}})
        leftovers = list(Path(self.mod.STATE_DIR).glob("*.tmp"))
        self.assertEqual(leftovers, [], "原子写不该留下 .tmp")

    def test_corrupt_falls_back_to_bak(self):
        self.mod.save_state({"users": {"a": {"chat_key": "a", "v": 1}}})   # 第一份
        self.mod.save_state({"users": {"a": {"chat_key": "a", "v": 2}}})   # 生成 .bak（内容 = 第一份）
        Path(self.mod.STATE_FILE).write_text("{ 这不是 json", encoding="utf-8")
        st = self.mod.load_state()
        self.assertEqual(st.get("users", {}).get("a", {}).get("v"), 1, "应回退到 .bak")
        quarantined = list(Path(self.mod.STATE_DIR).glob("state.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1, "坏文件应被改名保留")


class TestA6ResetGuard(Base):
    """A6：没有目标私聊时 reset 不崩；能真正清掉计数与 pending"""

    def _run_reset(self):
        async def _run():
            return await self.mod.proactive_cmd(_Ctx(), "reset")
        return asyncio.run(_run())

    def test_reset_without_target(self):
        self.mod.config.CHAT_KEY = ""
        st = {"users": {}}
        with open(self.mod.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
        out = self._run_reset()
        self.assertEqual(out[0], "failed", f"应返回失败提示而不是抛异常: {out}")

    def test_reset_clears_counters(self):
        self.mod.config.CHAT_KEY = "onebot_v11-private_1"
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["no_reply_count"] = 3
        u["closer_count"] = 2
        u["sent_today"] = 5
        self.mod.save_state(st)

        out = self._run_reset()
        self.assertEqual(out[0], "success", f"reset 应成功: {out}")
        disk = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertEqual(int(disk.get("no_reply_count", 0)), 0)
        self.assertEqual(int(disk.get("closer_count", 0)), 0)
        self.assertEqual(int(disk.get("sent_today", 0)), 0)

    def test_reset_clears_pending(self):
        self.mod.config.CHAT_KEY = "onebot_v11-private_1"
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["pending"] = [{"text": "旧条子", "due_ts": 1e9 + 600, "kind": "care", "done": False}]
        self.mod.save_state(st)

        self._run_reset()
        disk = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertEqual(disk.get("pending") or [], [], "reset 后 pending 必须真的清空")


class _Ctx:
    chat_key = "onebot_v11-private_1"


def _fake_dt(hour):
    class _DT:
        @staticmethod
        def now():
            return datetime(2026, 11, 5, hour, 0, 0)

        @staticmethod
        def fromtimestamp(ts):
            return datetime.fromtimestamp(ts)
    return _DT


def _fake_dt_from_box(box):
    class _DT:
        @staticmethod
        def now():
            return datetime(2026, 11, 5, box["h"], 0, 0)

        @staticmethod
        def fromtimestamp(ts):
            return datetime.fromtimestamp(ts)
    return _DT


if __name__ == "__main__":
    unittest.main(verbosity=2)
