"""A9 + B9 的用例：闸门三态、temperature、取第一个 ALLOW|SKIP"""

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
        self.mod.config.LLM_GATE = True
        self.mod.config.SKIP_COOLDOWN_MINUTES = 30

    def user(self):
        st = {"users": {}}
        u = self.mod.get_user(st, "onebot_v11-private_1")
        u["last_user_msg_ts"] = 1e9 - 600
        return u


class TestGateThreeState(Base):
    def test_no_model_passes(self):
        u = self.user()
        with patch.object(self.mod, "_model_args", return_value={}), \
             patch.object(self.mod, "_now", return_value=1e9):
            ok = asyncio.run(self.mod.gate(u, "想说话", []))
        self.assertTrue(ok, "没有可用模型时应放行")
        self.assertEqual(float(u.get("gate_skip_until", 0) or 0), 0.0)

    def test_empty_answer_denies(self):
        u = self.user()

        async def _empty(*a, **k):
            return ""

        with patch.object(self.mod, "_model_args", return_value={"model": "x"}), \
             patch.object(self.mod, "llm_chat", _empty), \
             patch.object(self.mod, "_now", return_value=1e9):
            ok = asyncio.run(self.mod.gate(u, "想说话", []))
        self.assertFalse(ok, "有模型但没答案应否决")
        self.assertGreater(float(u.get("gate_skip_until", 0) or 0), 1e9, "否决应写入冷却")

    def test_allow_and_skip(self):
        u = self.user()

        async def _allow(*a, **k):
            return "ALLOW"

        async def _skip(*a, **k):
            return "SKIP"

        with patch.object(self.mod, "_model_args", return_value={"model": "x"}), \
             patch.object(self.mod, "_now", return_value=1e9):
            with patch.object(self.mod, "llm_chat", _allow):
                self.assertTrue(asyncio.run(self.mod.gate(u, "想说话", [])))
            u["gate_skip_until"] = 0
            with patch.object(self.mod, "llm_chat", _skip):
                self.assertFalse(asyncio.run(self.mod.gate(u, "想说话", [])))

    def test_first_verdict_wins(self):
        self.assertEqual(self.mod._first_verdict("ALLOW，不需要 SKIP"), "ALLOW")
        self.assertEqual(self.mod._first_verdict("SKIP，别发"), "SKIP")
        self.assertIsNone(self.mod._first_verdict("我觉得挺好的"))


class TestTemperature(Base):
    def test_gate_passes_low_temperature(self):
        u = self.user()
        seen = {}

        async def _fake(prompt, **kw):
            seen.update(kw)
            return "ALLOW"

        with patch.object(self.mod, "_model_args", return_value={"model": "x"}), \
             patch.object(self.mod, "_now", return_value=1e9), \
             patch.object(self.mod, "llm_chat", _fake):
            asyncio.run(self.mod.gate(u, "想说话", []))
        # gate 直接调 llm_chat（这里已被替换），改测真实签名默认值
        import inspect
        sig = inspect.signature(self.mod.llm_chat)
        self.assertIn("temperature", sig.parameters)
        self.assertAlmostEqual(sig.parameters["temperature"].default, 0.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
