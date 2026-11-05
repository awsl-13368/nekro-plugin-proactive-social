"""R27（4.1.1）：闸门/回访用的模型分组要跟随主程序的 USE_MODEL_GROUP"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from nekro_stub import load_plugin  # noqa: E402


def _group(model, api_key="k", model_type="chat"):
    return types.SimpleNamespace(
        MODEL_TYPE=model_type,
        API_KEY=api_key,
        CHAT_MODEL=model,
        BASE_URL="http://stub",
    )


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.mod = load_plugin(REPO, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def cfg(self, groups, prefer):
        return types.SimpleNamespace(MODEL_GROUPS=groups, USE_MODEL_GROUP=prefer)


class TestModelGroup(Base):
    def test_prefers_use_model_group(self):
        groups = {
            "default": _group("model-default"),
            "main": _group("model-main"),
        }
        with patch.object(self.mod, "core_config", self.cfg(groups, "main")):
            args = self.mod._model_args()
        self.assertEqual(args.get("group"), "main", "应优先用 USE_MODEL_GROUP 指定的分组")
        self.assertEqual(args.get("model"), "model-main")

    def test_falls_back_when_preferred_missing(self):
        groups = {"default": _group("model-default")}
        with patch.object(self.mod, "core_config", self.cfg(groups, "gone")):
            args = self.mod._model_args()
        self.assertEqual(args.get("group"), "default", "指定分组不存在时要回退，而不是返回空")

    def test_skips_non_chat_group(self):
        groups = {
            "default": _group("embed", model_type="embedding"),
            "chatty": _group("model-chatty"),
        }
        with patch.object(self.mod, "core_config", self.cfg(groups, "default")):
            args = self.mod._model_args()
        self.assertEqual(args.get("group"), "chatty", "非 chat 分组要跳过")

    def test_empty_group_returns_empty(self):
        with patch.object(self.mod, "core_config", self.cfg({}, "default")):
            self.assertEqual(self.mod._model_args(), {})
