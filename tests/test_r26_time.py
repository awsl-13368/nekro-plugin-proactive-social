"""R26（4.1.1）：由头 / 闸门 / 生成 prompt 的时间标记与时效约束"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

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


class TestTranscriptTime(Base):
    NOW = datetime(2026, 11, 5, 22, 0, 0).timestamp()   # 周四 22:00

    def msgs(self):
        return [
            {"is_bot": False, "text": "晚上吃啥", "ts": self.NOW - 3 * 3600},
            {"is_bot": True, "text": "随便吃点", "ts": self.NOW - 2 * 3600},
        ]

    def test_clock_mode_same_day(self):
        self.mod.config.PROMPT_TIME_MODE = "clock"
        out = self.mod.transcript(self.msgs(), now_ts=self.NOW)
        self.assertIn("[19:00] TA: 晚上吃啥", out)
        self.assertIn("[20:00] 我: 随便吃点", out)

    def test_clock_mode_cross_day(self):
        self.mod.config.PROMPT_TIME_MODE = "clock"
        old = [{"is_bot": False, "text": "昨天说的", "ts": self.NOW - 30 * 3600}]
        out = self.mod.transcript(old, now_ts=self.NOW)
        self.assertIn("11-04", out, "跨天要带日期")

    def test_relative_mode(self):
        self.mod.config.PROMPT_TIME_MODE = "relative"
        out = self.mod.transcript(self.msgs(), now_ts=self.NOW)
        self.assertIn("3.0 小时前", out)

    def test_off_mode(self):
        self.mod.config.PROMPT_TIME_MODE = "off"
        out = self.mod.transcript(self.msgs(), now_ts=self.NOW)
        self.assertEqual(out.splitlines()[0], "TA: 晚上吃啥", "关掉时间标记后应与旧格式一致")

    def test_memory_age_label(self):
        self.assertEqual(self.mod._age_label(self.NOW - 3600, self.NOW), "今天")
        self.assertEqual(self.mod._age_label(self.NOW - int(1.5 * 86400), self.NOW), "昨天")
        self.assertEqual(self.mod._age_label(self.NOW - 3 * 86400, self.NOW), "3 天前")


class TestPromptTimeHeader(Base):
    def test_now_label_shape(self):
        label = self.mod.now_label()
        self.assertRegex(
            label,
            r"^现在是 \d{4}-\d{2}-\d{2} \d{2}:\d{2}（周[一二三四五六日]）$",
        )
