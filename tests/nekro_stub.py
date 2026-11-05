"""最小 nekro_agent 替身，让插件能在没有 NekroAgent 的环境里被 import。

用法：
    from nekro_stub import load_plugin
    mod = load_plugin(REPO_DIR, TEMP_STATE_DIR)

设计原则：
  · 不依赖 pydantic（G:\\NekroAgent\\python 里没装），自己实现一个够用的 Field/ConfigBase
  · 所有会被插件 import 的模块都注册进 sys.modules
  · 状态目录通过 NEKRO_STUB_DATA 环境变量传给插件里的 plugin.get_plugin_data_dir()
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

_MARK = object()


class _Field:
    def __init__(self, default=None, **kw):
        self.default = default
        self.meta = kw


def Field(default=None, **kw):
    return _Field(default, **kw)


class ConfigBase:
    """够用的配置基类：把类属性里的 _Field 变成实例属性"""

    def __init__(self, **overrides):
        for klass in reversed(type(self).__mro__):
            for name, val in vars(klass).items():
                if isinstance(val, _Field):
                    setattr(self, name, val.default)
        for k, v in overrides.items():
            setattr(self, k, v)

    def model_dump(self):
        return {k: v for k, v in vars(self).items() if not k.startswith("_")}


class _Logger:
    def info(self, msg, *a, **kw):
        print("[stub:info]", msg)

    def warning(self, msg, *a, **kw):
        print("[stub:warn]", msg)

    def error(self, msg, *a, **kw):
        print("[stub:error]", msg)

    def debug(self, msg, *a, **kw):
        pass


class NekroPlugin:
    def __init__(self, name="", module_name="", description="", version="", author="", url="", support_adapter=None):
        self.name = name
        self.module_name = module_name
        self.version = version
        self._config_cls = None
        self._config = None

    # ---- 装饰器 ----
    def mount_config(self):
        def deco(cls):
            self._config_cls = cls
            return cls
        return deco

    def get_config(self, cls=None):
        if self._config is None:
            target = cls or self._config_cls
            self._config = target() if target else None
        return self._config

    def mount_init_method(self):
        return lambda fn: fn

    def mount_cleanup_method(self):
        return lambda fn: fn

    def mount_on_user_message(self):
        return lambda fn: fn

    def mount_command(self, *a, **kw):
        return lambda fn: fn

    def mount_prompt_inject_method(self, *a, **kw):
        return lambda fn: fn

    def mount_sandbox_method(self, *a, **kw):
        return lambda fn: fn

    def get_plugin_data_dir(self):
        d = Path(os.environ.get("NEKRO_STUB_DATA", ""))
        d.mkdir(parents=True, exist_ok=True)
        return d


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _install_stubs():
    """注册 nekro_agent 相关模块（可重复调用）"""
    if "nekro_agent.api.plugin" in sys.modules:
        return

    pyd = _mod("pydantic", Field=Field, BaseModel=ConfigBase)

    na = _mod("nekro_agent")
    api = _mod("nekro_agent.api")
    api_core = _mod("nekro_agent.api.core", logger=_Logger())
    api_plugin = _mod("nekro_agent.api.plugin", ConfigBase=ConfigBase, NekroPlugin=NekroPlugin)
    api_schemas = _mod("nekro_agent.api.schemas", AgentCtx=object)

    schemas = _mod("nekro_agent.schemas")
    chat_message = _mod("nekro_agent.schemas.chat_message", ChatMessage=object)

    services = _mod("nekro_agent.services")
    cmd = _mod("nekro_agent.services.command")

    class _CmdPerm:
        SUPER_USER = "super_user"
        ADMIN = "admin"
        USER = "user"

    cmd_base = _mod("nekro_agent.services.command.base", CommandPermission=_CmdPerm)
    cmd_ctl = _mod("nekro_agent.services.command.ctl", CmdCtl=types.SimpleNamespace(
        success=lambda *a, **k: ("success", a, k),
        failed=lambda *a, **k: ("failed", a, k),
    ))
    cmd_schemas = _mod("nekro_agent.services.command.schemas",
                       Arg=lambda *a, **k: None,
                       CommandExecutionContext=object,
                       CommandResponse=object)
    msg_service = _mod("nekro_agent.services.message_service", message_service=types.SimpleNamespace())

    for m in (na, api, schemas, services, cmd, pyd):
        pass
    na.api = api
    api.core = api_core
    api.plugin = api_plugin
    api.schemas = api_schemas
    na.schemas = schemas
    na.services = services


def load_plugin(repo_dir, state_dir):
    """加载插件文件，并把状态目录指向 state_dir"""
    _install_stubs()
    repo = Path(repo_dir)
    target = repo / "__init__.py"
    if not target.exists():
        raise FileNotFoundError(str(target))

    os.environ["NEKRO_STUB_DATA"] = str(state_dir)
    Path(state_dir).mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location("proactive_social_under_test", str(target))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["proactive_social_under_test"] = mod
    spec.loader.exec_module(mod)

    # 状态文件三件套一起指过去，避免读到真实 .bak
    sd = Path(state_dir)
    mod.STATE_DIR = sd
    mod.STATE_FILE = sd / "state.json"
    mod.STATE_BAK = sd / "state.json.bak"
    return mod
