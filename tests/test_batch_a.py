"""批 A（B3/B4/B5/B6）的用例 + R23 指出的两个缺口"""

from __future__ import annotations

import importlib.util
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

    def user(self, **kw):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u.update(kw)
        self.mod.save_state(st)
        return u


class TestB3Cues(Base):
    """一句话只记一个最具体的锚点；时段映射到小时；等会儿 +45 分钟"""

    NOW = datetime(2026, 11, 5, 21, 0, 0).timestamp()   # 周四 21:00

    def test_morning_hour_mapping(self):
        cues = self.mod.extract_cues("明天早上要考试", self.NOW)
        self.assertEqual(len(cues), 1, "一句话只该出一个锚点")
        due = datetime.fromtimestamp(cues[0]["due_ts"])
        self.assertEqual((due.day, due.hour), (6, 8), "明天早上 → 次日 08:00")

    def test_afternoon_mapping(self):
        cues = self.mod.extract_cues("后天下午过来", self.NOW)
        due = datetime.fromtimestamp(cues[0]["due_ts"])
        self.assertEqual((due.day, due.hour), (7, 15))

    def test_soon_is_45_minutes(self):
        cues = self.mod.extract_cues("我一会儿回来", self.NOW)
        due = datetime.fromtimestamp(cues[0]["due_ts"])
        self.assertAlmostEqual((due.timestamp() - self.NOW) / 60, 45, delta=1)

    def test_next_month_ignored(self):
        self.assertEqual(self.mod.extract_cues("下个月再说吧", self.NOW), [])

    def test_next_next_week_ignored(self):
        """修复 R24-1：'下下周' 不能被当成 '下周'"""
        self.assertEqual(self.mod.extract_cues("下下周再说", self.NOW), [])


class TestB4ReplyRate(Base):
    def test_zero_rate_not_treated_as_default(self):
        u = self.user(msg_count=10, reply_rate=0.0)
        score = self.mod.affection_score(u)
        # 若 0.0 被当成 0.7，会多加 4 分
        u2 = self.user(msg_count=10, reply_rate=0.7)
        self.assertLess(score, self.mod.affection_score(u2))

    def test_urge_scale_reflects_reply_rate(self):
        """调真实函数：pings>=3 且 reply_rate=0 时，系数压到一半以下"""
        self.mod.config.ACTIVITY_LEVEL = 75
        self.mod.config.ADAPTIVE_REPLY_RATE = True

        u_low = self.user(pings=4, reply_rate=0.0)
        u_high = self.user(pings=4, reply_rate=1.0)
        low = self.mod.urge_scale(u_low, weekend=False)
        high = self.mod.urge_scale(u_high, weekend=False)
        self.assertAlmostEqual(low, 0.5, places=3)
        self.assertAlmostEqual(high, 1.0, places=3)
        self.assertLess(low, high)

    def test_urge_scale_ignores_rate_before_three_pings(self):
        self.mod.config.ACTIVITY_LEVEL = 75
        self.mod.config.ADAPTIVE_REPLY_RATE = True
        u = self.user(pings=2, reply_rate=0.0)
        self.assertAlmostEqual(self.mod.urge_scale(u, weekend=False), 1.0, places=3)


class TestB5EnsureToday(Base):
    def test_reset_counts_on_new_day(self):
        u = self.user(sent_today=9, sent_date="2026-11-04", miss_today=2, miss_date="2026-11-04")
        self.mod.ensure_today(u)
        self.assertEqual(u["sent_today"], 0)
        self.assertEqual(u["miss_today"], 0)

    def test_same_day_keeps_counts(self):
        today = datetime.now().strftime("%Y-%m-%d")
        u = self.user(sent_today=3, sent_date=today)
        self.mod.ensure_today(u)
        self.assertEqual(u["sent_today"], 3)


class TestB6QuietSleep(Base):
    def test_wait_truncated_before_quiet_end(self):
        """调真实函数：06:50 时把 30 分钟的心跳截短到 07:00 + 60 秒"""
        self.mod.config.QUIET_END_WAKE = True
        now_dt = datetime(2026, 11, 5, 6, 50, 0)
        got = self.mod.heartbeat_wait(now_dt, 30 * 60)
        self.assertAlmostEqual(got, 10 * 60 + 60, delta=2)

    def test_wait_untouched_outside_quiet(self):
        self.mod.config.QUIET_END_WAKE = True
        now_dt = datetime(2026, 11, 5, 14, 0, 0)
        self.assertEqual(self.mod.heartbeat_wait(now_dt, 30 * 60), 30 * 60)


class TestR23Gaps(Base):
    """R23 指出的两个缺口"""

    def test_extract_unresolved_passes_low_temperature(self):
        src = (REPO / "__init__.py").read_text(encoding="utf-8")
        idx = src.index("async def extract_unresolved")
        seg = src[idx: idx + 3000]
        self.assertIn("temperature=0.3", seg, "extract_unresolved 应传 0.3")

    def test_no_reply_count_increments_once_per_tick(self):
        st = {"users": {}, "last_chat_key": "onebot_v11-private_1"}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["reflect_at"] = 1e9 - 3600
        u["no_reply_count"] = 0
        self.mod.save_state(st)
        self.mod.config.REFLECT_ENABLED = True
        self.mod.config.REFLECT_AFTER_MINUTES = 1

        for i in range(3):
            fresh = self.mod.load_state()
            nu = self.mod.get_user(fresh, "onebot_v11-private_1")
            base_t, base_m = float(nu["tired"]), float(nu["mood"])
            with patch.object(self.mod, "_now", return_value=1e9 + i):
                self.mod.run_reflection(nu)
                self.mod.commit(
                    nu,
                    deltas={"tired": float(nu["tired"]) - base_t, "mood": float(nu["mood"]) - base_m},
                    tick_start_ts=1e9 + i,
                )

        got = self.mod.load_state()["users"]["onebot_v11-private_1"]
        self.assertEqual(int(got.get("no_reply_count", 0)), 1, "只反省一次，计数只该 +1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
