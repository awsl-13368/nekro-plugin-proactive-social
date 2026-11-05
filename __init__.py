"""
# 主动消息 v3.0 · 念头驱动社交引擎

移植自 `astrbot_plugin_autonomous_social` 的核心设计（作者 娜莉灵），
改为 NekroAgent 插件实现，**只做私聊**。

## 核心思想

不是定时器。**念头攒够了也要有「由头」才开口**：

```
心跳 → 念头累积 → 攒满 → 找由头 → 模型闸门 → 开口 → 记账
                      ↓派不出      ↓否决
                    不发（念头留着）  不发
```

三条不发：**派不出由头、模型否决、硬闸拦截** —— 都不发。

## 由头（开口的理由）优先级

1. **到期未完话题** —— 追问那件事 / 问在不在 / 接话
2. **时间锚点** —— 对方说过"明天""晚上"，到点了
3. **回访** —— 从对话里找提过但没说结果的事
4. **问候** —— 半天以上没说话
5. **生活场景兜底** —— 40 个具体场景（做噩梦/嘴馋/看到像你的东西）

## 社交闭环

- **追问**：对方回得敷衍 → 12 分钟后追问那件事
- **接话**：她说完对方消失 → 90 分钟后补一句
- **回访**：事情没结果 → 2.5~14 小时后问后来
- **收场**：她发的没人接 → 8 小时后自己收尾（最多 3 次）
- **停发**：连续 3 天不理她 → 全面停发（含问候），等对方先开口

## 只做私聊

群消息一律不看、不记、不触发。
"""

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import Field

from nekro_agent.api import core
from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import Arg, CommandExecutionContext, CommandResponse
from nekro_agent.services.message_service import message_service

# ---- 可选依赖（缺失时自动降级）----
try:
    from nekro_agent.services.agent.openai import gen_openai_chat_response
except Exception:
    gen_openai_chat_response = None  # type: ignore

try:
    from nekro_agent.core.config import config as core_config
except Exception:
    core_config = None  # type: ignore

try:
    from nekro_agent.models.db_chat_message import DBChatMessage
except Exception:
    DBChatMessage = None  # type: ignore


# ======================================================================== 插件

plugin = NekroPlugin(
    name="主动消息",
    module_name="proactive_social",
    description="念头驱动的主动社交：有由头才开口，会追问/接话/回访/收场，被无视就停",
    version="4.1.2",
    author="awsl_13368",
    url="",
    support_adapter=["onebot_v11"],
)


# ======================================================================== 配置


@plugin.mount_config()
class ProactiveConfig(ConfigBase):
    """主动消息 v3 配置"""

    ENABLE: bool = Field(default=True, title="总开关", description="关闭后完全不主动发消息")

    CHAT_KEY: str = Field(
        default="",
        title="目标私聊",
        description="形如 onebot_v11-private_QQ号；留空则用最近记录的私聊",
    )

    ACTIVITY_LEVEL: int = Field(
        default=75, ge=0, le=100,
        title="活跃度",
        description="整体想说话的程度，缩放念头积累速度（不是每隔几分钟发一条）",
    )
    URGE_REFILL_HOURS: float = Field(
        default=4.0, ge=0.5, le=72,
        title="念头攒满时长(小时)",
        description="最在意的人攒满一次念头大约要多久",
    )
    RECENT_TALK_MINUTES: int = Field(
        default=18, ge=0, le=600,
        title="刚聊过不另起话题(分钟)",
    )
    HEARTBEAT_MIN_MINUTES: int = Field(
        default=8, ge=1, le=120,
        title="心跳下限(分钟)",
        description="决定所有分钟级配置的实际精度",
    )
    HEARTBEAT_MAX_MINUTES: int = Field(default=30, ge=1, le=240, title="心跳上限(分钟)")

    QUIET_START: str = Field(default="00:30", title="安静时段开始", description="格式 HH:MM；跨天也支持")
    QUIET_END: str = Field(default="07:00", title="安静时段结束")

    LLM_GATE: bool = Field(
        default=True,
        title="模型闸门",
        description="发前让模型判断该不该说，它可以说 NO",
    )
    SKIP_COOLDOWN_MINUTES: int = Field(
        default=10, ge=0, le=180,
        title="否决后冷却(分钟)",
    )

    FOLLOWUP_ENABLED: bool = Field(default=True, title="未完话题", description="话断了会接一句")
    PROBE_AFTER_MINUTES: int = Field(default=12, ge=1, le=600, title="敷衍后追问(分钟)")
    FOLLOWUP_AFTER_MINUTES: int = Field(default=30, ge=1, le=1440, title="沉默后接话(分钟)")
    FOLLOWUP_MAX_MINUTES: int = Field(default=70, ge=1, le=2880, title="接话上限(分钟)")
    FOLLOWUP_COOLDOWN_MINUTES: int = Field(default=30, ge=0, le=600, title="两次接话最短间隔(分钟)")

    CONTINUE_ENABLED: bool = Field(default=True, title="接话", description="她说完、对方就没声了：过一阵接一句")
    CONTINUE_AFTER_MINUTES: int = Field(default=90, ge=10, le=1440, title="说完后接话(分钟)")

    LOOP_ENABLED: bool = Field(default=True, title="回访", description="记住对方提过、没说结果的事")
    LOOP_MIN_HOURS: float = Field(default=2.5, ge=0.5, le=48, title="回访窗口下限(小时)")
    LOOP_MAX_HOURS: float = Field(
        default=14.0, ge=1, le=96, title="回访窗口上限(小时)",
        description="对话超过这么久就不再「回访那件事」（太旧了像翻旧账）",
    )

    CLOSER_ENABLED: bool = Field(default=True, title="收场", description="她发的话没人接时，自己冒一句收尾")
    CLOSER_AFTER_HOURS: float = Field(default=8.0, ge=1, le=48, title="收场等待(小时)")
    CLOSER_MAX_COUNT: int = Field(default=3, ge=0, le=10, title="最多收场几次")

    SILENT_GIVEAWAY_DAYS: float = Field(
        default=3.0, ge=0, le=60,
        title="被无视就停(天)",
        description="对方连续这么多天没说话、她发过也没人回，就全面停发，等对方先开口。0=关闭",
    )

    MAX_SENDS_PER_DAY: int = Field(default=8, ge=0, le=100, title="每日上限")
    USER_COOLDOWN_MINUTES: int = Field(default=30, ge=1, le=1440, title="最小间隔(分钟)")

    GREETING_ENABLED: bool = Field(default=True, title="问候")
    GREETING_GAP_HOURS: float = Field(default=12.0, ge=1, le=72, title="多久没说话才问候(小时)")

    SPECIAL_CARE_UIDS: List[str] = Field(
        default=[],
        title="特别关心",
        description="名单里的人不被安静时段、最小间隔和「没回就停」挡住",
    )
    SPECIAL_CARE_GAP_MINUTES: int = Field(default=20, ge=1, le=1440, title="特别关心最小间隔(分钟)")
    SPECIAL_CARE_IGNORE_QUIET: bool = Field(default=False, title="特别关心无视安静时段")

    MAX_MESSAGE_LENGTH: int = Field(default=60, ge=10, le=500, title="单条上限(字)")

    FALLBACK_EVENTS: bool = Field(
        default=True,
        title="生活场景兜底",
        description="没有具体由头时，允许从内置生活场景里挑一个",
    )

    # ---------------- 拟人细节 ----------------
    TYPING_ENABLED: bool = Field(
        default=True,
        title="正在输入",
        description="写消息的那几秒显示「正在输入」（需协议端支持 set_input_status，不支持会自动跳过）",
    )
    TYPING_SECONDS: float = Field(default=6.0, ge=1, le=60, title="正在输入持续(秒)")

    ALLOW_BURST: bool = Field(default=True, title="连发", description="允许拆成两条短句发（取决于 Agent 的分段设置）")

    # ---------------- 问候三窗口 ----------------
    GREETING_MORNING_START: str = Field(default="07:00", title="早安窗口开始")
    GREETING_MORNING_END: str = Field(default="11:00", title="早安窗口结束")
    GREETING_MIDDAY_START: str = Field(default="12:00", title="午间问候开始")
    GREETING_MIDDAY_END: str = Field(default="14:00", title="午间问候结束（早安错过了才补）")
    GREETING_NIGHT_START: str = Field(default="21:00", title="晚安窗口开始")
    GREETING_NIGHT_END: str = Field(default="23:59", title="晚安窗口结束")

    # ---------------- 思念通道 ----------------
    MISS_ENABLED: bool = Field(default=True, title="想念通道", description="关系够近才会说「想你了」")
    MISS_AFFECTION_MIN: int = Field(default=42, ge=0, le=100, title="想念门槛(好感)", description="对陌生人说想你了很怪，所以门槛要高")
    MISS_DAILY_CAP: int = Field(default=3, ge=0, le=20, title="每天最多几条想念")

    # ---------------- 特别关心自动认定 ----------------
    SPECIAL_CARE_AUTO: bool = Field(default=True, title="自动认定特别关心", description="关系够近自动放宽限制")
    SPECIAL_CARE_MIN_AFFECTION: int = Field(default=85, ge=0, le=100, title="自动认定·好感门槛")
    SPECIAL_CARE_MIN_MESSAGES: int = Field(default=60, ge=1, le=10000, title="自动认定·消息条数门槛")

    # ---------------- 生病关怀 ----------------
    CARE_ENABLED: bool = Field(default=True, title="生病关怀", description="对方说不舒服时，过一阵问问好点没")
    CARE_AFTER_MINUTES: int = Field(default=35, ge=5, le=600, title="关怀延迟(分钟)")

    # ---------------- 回复率自适应 ----------------
    ADAPTIVE_REPLY_RATE: bool = Field(default=True, title="按回复率调整", description="总是不回就慢慢收着说")
    REPLY_WINDOW_MINUTES: int = Field(default=120, ge=10, le=1440, title="多久内回算作「接住了」(分钟)")

    # ---------------- 数据清理 ----------------
    USER_RETENTION_DAYS: float = Field(default=30.0, ge=0, le=365, title="闲置清理(天)", description="多久没互动就清掉状态，0=不清理")

    # ---------------- 群聊心流 ----------------
    GROUP_FLOW_ENABLED: bool = Field(
        default=False,
        title="群聊心流",
        description="在群里自然接话/破冰/起话题（默认关；开了她才会在群里主动说话）",
    )
    GROUP_FLOW_WHITELIST: List[str] = Field(default=[], title="只在这些群生效", description="填群号；留空=所有已聊过的群")
    GROUP_FLOW_WINDOW_MINUTES: int = Field(default=8, ge=1, le=120, title="说过话后的关注窗口(分钟)")
    GROUP_FLOW_HOLD_SECONDS: int = Field(default=120, ge=10, le=600, title="被回完后先静多久(秒)", description="防自己跟自己连成一串")
    GROUP_FLOW_MIN_GAP_SECONDS: int = Field(default=120, ge=30, le=1800, title="两次插话最短间隔(秒)")
    GROUP_FLOW_MAX_PER_WINDOW: int = Field(default=3, ge=1, le=10, title="一个窗口最多接几条")
    GROUP_FLOW_HOURLY_CAP: int = Field(default=6, ge=0, le=60, title="每群每小时上限")
    GROUP_FLOW_IGNORED_EXIT: int = Field(default=2, ge=1, le=10, title="连插几条没人接就退场")
    GROUP_ICEBREAK_ENABLED: bool = Field(default=True, title="破冰", description="群里冷场时开个话头")
    GROUP_IDLE_HOURS: float = Field(default=6.0, ge=0.5, le=72, title="群冷场多久算冷场(小时)")
    GROUP_ICEBREAK_DAILY_CAP: int = Field(default=2, ge=0, le=20, title="每群每天破冰上限")
    GROUP_TOPIC_ENABLED: bool = Field(default=True, title="起话头", description="她在群里静了很久，主动说点自己的事")
    GROUP_TOPIC_GAP_HOURS: float = Field(default=8.0, ge=0.5, le=168, title="静多久才起话头(小时)")
    GROUP_TOPIC_DAILY_CAP: int = Field(default=1, ge=0, le=10, title="每群每天起话头上限")

    # ---------------- 播种与静音结束 ----------------
    SEED_USERS: List[str] = Field(
        default=[],
        title="播种名单",
        description="没聊过的人也能写进来（填 QQ 号或完整 chat_key），攒够念头会主动找上门",
    )
    QUIET_END_WAKE: bool = Field(
        default=True,
        title="静音结束先醒一次",
        description="安静时段一结束就先评估一次，不等下一个心跳",
    )

    # ---------------- 抑制层（v4：想发但忍住） ----------------
    SUPPRESS_ENABLED: bool = Field(
        default=True,
        title="抑制层",
        description="不是想发就发：累了、对方在忙、刚反省过 → 把轻社交的念头压住（不问模型，纯规则）",
    )
    SUPPRESS_TIRED_THRESHOLD: int = Field(
        default=70, ge=0, le=100,
        title="疲惫到这个值就压住",
        description="疲惫度超过它，就不发「想你/无聊/撒娇」这类轻由头",
    )
    BUSY_QUIET_HOURS: float = Field(
        default=12.0, ge=0, le=72,
        title="对方说忙/累后安静多久(小时)",
        description="对方提过累、忙、加班，这段时间内不推「想你」类",
    )
    OBSESSION_BREAK_THRESHOLD: int = Field(
        default=3, ge=1, le=10,
        title="压几次后忍不住",
        description="同一个由头被压这么多次，就允许一次「实在憋不住」说出来",
    )

    # ---------------- 记忆重量（v4：重要的记得住，废话会忘） ----------------
    MEMORY_HALF_LIFE_DAYS: float = Field(
        default=14.0, ge=1, le=365,
        title="记忆半衰期(天)",
        description="权重每过这么多天掉一半",
    )
    MEMORY_MIN_WEIGHT: float = Field(
        default=0.08, ge=0.0, le=1.0,
        title="遗忘阈值",
        description="权重衰减到这个值以下就忘掉",
    )
    MEMORY_MAX_ITEMS: int = Field(default=40, ge=5, le=300, title="最多记多少条")

    # ---------------- 事后反省（v4：说多了会收敛） ----------------
    REFLECT_ENABLED: bool = Field(
        default=True,
        title="事后反省",
        description="主动发完过一阵没人回 → 记一笔「是不是说多了」，接下来更收敛",
    )
    REFLECT_AFTER_MINUTES: int = Field(default=60, ge=5, le=1440, title="多久后反省(分钟)")
    RESTRAINT_HOURS: float = Field(default=24.0, ge=1, le=168, title="反省后收敛多久(小时)")

    # ---------------- 兴致参与决策（v4.1） ----------------
    MOOD_ENABLED: bool = Field(
        default=True,
        title="兴致参与决策",
        description="兴致低时不说琐事、不撒娇、语气变淡；兴致高时可以多说两句",
    )
    MOOD_LOW_THRESHOLD: int = Field(
        default=35, ge=0, le=100,
        title="兴致低的门槛",
        description="兴致低于它就进入「没力气」状态",
    )
    MOOD_HIGH_THRESHOLD: int = Field(
        default=75, ge=0, le=100,
        title="兴致高的门槛",
        description="兴致高于它就可以多说两句、带点小雀跃",
    )

    PROMPT_TIME_MODE: str = Field(
        default="clock",
        title="提示词时间标记",
        description="给由头/闸门里的对话加时间：clock=绝对时间（推荐）、relative=几小时前、off=不加",
    )

    DEBUG: bool = Field(default=False, title="调试日志", description="记录念头进度与拦截原因")


config = plugin.get_config(ProactiveConfig)


# ======================================================================== 状态

def _resolve_state_dir() -> Path:
    """状态目录：优先用 NA 分配的数据目录，保证换机器也能跑"""
    try:
        d = plugin.get_plugin_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        # 兜底：<插件目录>/../../plugin_data/<模块名>
        d = Path(__file__).resolve().parent.parent.parent / "plugin_data" / "proactive_social"
        d.mkdir(parents=True, exist_ok=True)
        return d


STATE_DIR = _resolve_state_dir()
STATE_FILE = STATE_DIR / "state.json"
STATE_BAK = STATE_DIR / "state.json.bak"


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写 .tmp，flush+fsync 后 os.replace 覆盖。
    写到一半被中断时，原文件不受影响。"""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp, path)


def _quarantine_state(path: Path) -> None:
    """把坏掉的状态文件改名保留，不删除"""
    try:
        bad = path.with_name(path.name + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.replace(path, bad)
        core.logger.warning(f"[proactive3] 坏状态文件已改名保留: {bad.name}")
    except Exception as e:
        core.logger.warning(f"[proactive3] 坏状态文件改名失败: {e}")


# 一次性迁移：早期版本把 state.json 写在 plugin_data/proactive_social/
_LEGACY_STATE = Path(__file__).resolve().parent.parent.parent / "plugin_data" / "proactive_social" / "state.json"
if (not STATE_FILE.exists()) and _LEGACY_STATE.exists() and _LEGACY_STATE != STATE_FILE:
    try:
        _atomic_write_text(STATE_FILE, _LEGACY_STATE.read_text(encoding="utf-8"))
        core.logger.info("[proactive3] 已从旧路径迁移状态")
    except Exception as e:
        core.logger.warning(f"[proactive3] 状态迁移失败: {e}")


def _now() -> float:
    return time.time()


def normalize_chat_key(raw: str) -> str:
    """修复 C1：纯数字补成 onebot_v11-private_<QQ>；已是完整格式或其它协议前缀的不动"""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return f"onebot_v11-private_{s}"
    return s


_BAD_KEY_WARNED = {"done": False}


def _warn_bad_chat_key(raw: str) -> None:
    """CHAT_KEY 既不是纯数字也不是完整 chat_key 时只提示一次，避免每轮心跳刷屏"""
    if _BAD_KEY_WARNED["done"]:
        return
    _BAD_KEY_WARNED["done"] = True
    core.logger.warning(
        f"[proactive3] CHAT_KEY 无效（当前={raw!r}），念头不会累积；"
        "请填 QQ 号或 onebot_v11-private_<QQ>"
    )


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_today(u: Dict[str, Any]) -> None:
    """修复 B5：跨天清零统一走这里（can_send / send_with_reason / status 共用）"""
    d = _today()
    if u.get("sent_date") != d:
        u["sent_date"] = d
        u["sent_today"] = 0
    if u.get("miss_date") != d:
        u["miss_date"] = d
        u["miss_today"] = 0


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            core.logger.warning(f"[proactive3] 状态文件解析失败: {e}")
            _quarantine_state(STATE_FILE)
    if STATE_BAK.exists():
        try:
            st = json.loads(STATE_BAK.read_text(encoding="utf-8"))
            core.logger.warning("[proactive3] 已回退到 state.json.bak（上一次的完好状态）")
            return st
        except Exception as e:
            core.logger.warning(f"[proactive3] 备份状态也读不了: {e}")
    return {"users": {}}


def save_state(st: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            try:
                _atomic_write_text(STATE_BAK, STATE_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                core.logger.warning(f"[proactive3] 备份状态失败（继续写入）: {e}")
        _atomic_write_text(STATE_FILE, json.dumps(st, ensure_ascii=False, indent=2))
    except Exception as e:
        core.logger.warning(f"[proactive3] 保存状态失败: {e}")


def commit_groups(gk: str, changes: Dict[str, Any]) -> None:
    """只改一个群的字段，不碰 users（修复 A4 问题 2）"""
    try:
        st = load_state()
        g = (st.get("groups") or {}).get(gk) or {"chat_key": gk}
        g.update(changes or {})
        st.setdefault("groups", {})[gk] = g
        save_state(st)
    except Exception as e:
        core.logger.warning(f"[proactive3] 保存群状态失败: {e}")


def flush_groups(st: Dict[str, Any]) -> None:
    """把内存里的 groups 写回磁盘，users 用磁盘上的最新值（A4 问题 2）"""
    try:
        disk = load_state()
        disk["groups"] = st.get("groups") or {}
        save_state(disk)
    except Exception as e:
        core.logger.warning(f"[proactive3] 保存群状态失败: {e}")


def commit(
    u: Dict[str, Any],
    force: Optional[set] = None,
    deltas: Optional[Dict[str, float]] = None,
    tick_start_ts: Optional[float] = None,
) -> None:
    """安全落盘：合并磁盘上可能更新的用户侧数据，避免 tick 覆盖刚到的消息

    force：本次以内存为准、不与磁盘合并的字段集合（reset 用）。
    deltas：tick 自己的增量（tired/mood 等），用"磁盘值 + 增量"合成，而不是拿旧值覆盖。
    tick_start_ts：本轮 tick 开始的时刻，用于判断哪些 memories 是 tick 期间钩子新加的。
    """
    force = set(force or ())
    st = load_state()
    disk = (st.get("users") or {}).get(u["chat_key"])
    if disk:
        # 修复 A4：tired/mood 按增量合并（磁盘值 + 本轮增量），钩子的 ±6/±8 不被盖掉
        for _f, _d in (deltas or {}).items():
            base = float(disk.get(_f, u.get(_f, 0.0)) or 0.0)
            u[_f] = max(0.0, min(100.0, base + float(_d)))

        # 修复 R21-3（选方案②）：memories 归钩子，tick 侧直接取磁盘，不合并、不会丢
        if "memories" not in force:
            u["memories"] = list(disk.get("memories", []) or [])

        # 修复 R21-1：pings 是只增计数，取两边较大的
        if "pings" not in force:
            u["pings"] = max(int(u.get("pings", 0) or 0), int(disk.get("pings", 0) or 0))

        # 修复 R22：reflect_at / last_proactive_ts 只有 tick 写，之前用 max() 会把
        # run_reflection 清的 0 又顶回旧值，导致每个 tick 重复反省。改成 tick 直接写。
        # reply_credit_ts 是钩子写的，取磁盘。
        for f in ("user_busy_until", "good_state_ts", "replies", "reply_rate", "reply_credit_ts"):
            if f in disk and f not in force:
                u[f] = disk[f]

        # 修复 R21-1：对方在 tick 期间说过话 → 钩子刚把这两个计数清零，取磁盘值
        _disk_lu = float(disk.get("last_user_msg_ts", 0) or 0)
        if (
            tick_start_ts is not None
            and _disk_lu > float(tick_start_ts)
            and "closer_count" not in force
        ):
            for f in ("closer_count", "no_reply_count"):
                if f in disk:
                    u[f] = disk[f]

        # 用户侧时间更新的，以磁盘为准
        if (
            "last_user_msg_ts" not in force
            and _disk_lu > float(u.get("last_user_msg_ts", 0) or 0)
        ):
            for f in ("last_user_msg_ts", "msg_count", "silent_until", "cues", "topics", "tier"):
                if f in disk:
                    u[f] = disk[f]
            u["bot_msgs_since_user"] = min(
                int(u.get("bot_msgs_since_user", 0) or 0),
                int(disk.get("bot_msgs_since_user", 0) or 0),
            )
        # pending 取并集（同一张条子只留一条），**完成态优先**；
        # force 里点名了 pending 就以内存为准（reset 靠这个把条子真正清掉）
        if "pending" not in force:
            merged: Dict[Any, Dict[str, Any]] = {}
            for p in list(u.get("pending", []) or []) + list(disk.get("pending", []) or []):
                key = (p.get("kind"), str(p.get("text"))[:40], int(float(p.get("due_ts", 0) or 0)))
                old = merged.get(key)
                if old is None or p.get("done") or p.get("used"):
                    merged[key] = p
            u["pending"] = list(merged.values())[-10:]

        # 时间锚点按 due_ts 合并，已用过的优先保留
        if "cues" not in force:
            cu: Dict[Any, Dict[str, Any]] = {}
            for c in list(u.get("cues", []) or []) + list(disk.get("cues", []) or []):
                key = int(float(c.get("due_ts", 0) or 0))
                oldc = cu.get(key)
                if oldc is None or c.get("used"):
                    cu[key] = c
            u["cues"] = list(cu.values())[-6:]

    st.setdefault("users", {})[u["chat_key"]] = u
    st["last_chat_key"] = u["chat_key"]
    save_state(st)


def get_user(st: Dict[str, Any], chat_key: str) -> Dict[str, Any]:
    users = st.setdefault("users", {})
    u = users.get(chat_key)
    if not u:
        u = {
            "chat_key": chat_key,
            "tier": "high",
            "urge": 0.0,
            "last_user_msg_ts": 0,
            "last_bot_msg_ts": 0,
            "last_proactive_ts": 0,
            "last_tick_ts": 0,
            "bot_msgs_since_user": 0,
            "silent_until": 0,
            "pending": [],
            "cues": [],
            "greeted": {},
            "sent_date": "",
            "sent_today": 0,
            "closer_count": 0,
            "gate_skip_until": 0,
            "msg_count": 0,
            "log": [],
            "topics": [],
            # v4：动机层
            "tired": 20.0,            # 疲惫 0-100
            "mood": 60.0,             # 兴致 0-100
            "user_busy_until": 0,     # 对方说忙/累，到这时刻前不推"想你"
            "restraint_until": 0,     # 反省后的收敛期
            "suppress_count": {},     # {kind: 被压次数}
            "suppress_log": [],       # 最近被压记录
            "memories": [],           # 带权重的记忆 [{text, weight, ts, kind}]
            "reflect_at": 0,          # 待反省的发送时间点
            "reflect_count": 0,       # 连续没人回的次数
            "no_reply_count": 0,      # 连续没人回的次数（反省用，和 closer_count 分开）
            "last_reflect_ts": 0,
        }
        users[chat_key] = u
    return u


# ======================================================================== 时间


_HM_WARNED: set = set()


def _parse_hm(s: str, default: str = "00:00") -> Tuple[int, int]:
    """修复 C2：非法时间（25:00 / abc / 空串）回退默认值，并只告警一次，不让 tick 抛异常"""
    raw = (s or "").strip()
    if raw:
        try:
            h_s, m_s = raw.split(":")
            h, m = int(h_s), int(m_s)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except Exception:
            pass
        if raw not in _HM_WARNED:
            _HM_WARNED.add(raw)
            core.logger.warning(f"[proactive3] 时间格式不对: {raw!r}，回退到 {default}")
    h, m = default.split(":")
    return int(h), int(m)


def in_range(now: datetime, start_s: str, end_s: str) -> bool:
    sh, sm = _parse_hm(start_s)
    eh, em = _parse_hm(end_s)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def in_quiet(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return in_range(now, config.QUIET_START, config.QUIET_END)


def _hm_plus(hm: str, minutes: int) -> str:
    """'07:00' + 10 分钟 → '07:10'（跨天自动绕回）"""
    h, m = _parse_hm(hm, "00:00")
    total = (h * 60 + m + int(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def minutes_since(ts: float) -> float:
    return 999999.0 if not ts else (_now() - ts) / 60.0


def hours_since(ts: float) -> float:
    return 9999.0 if not ts else (_now() - ts) / 3600.0


def days_since(ts: float) -> float:
    return 999.0 if not ts else (_now() - ts) / 86400.0


# ======================================================================== 历史


async def sync_from_history(u: Dict[str, Any], limit: int = 30) -> int:
    """从数据库补全状态（装插件前聊过的人也能接上）"""
    msgs = await fetch_recent(u["chat_key"], limit)
    if not msgs:
        return 0

    users = [m for m in msgs if not m["is_bot"]]
    bots = [m for m in msgs if m["is_bot"]]

    if users and not float(u.get("last_user_msg_ts", 0) or 0):
        u["last_user_msg_ts"] = max(m["ts"] for m in users)
    if bots and not float(u.get("last_bot_msg_ts", 0) or 0):
        u["last_bot_msg_ts"] = max(m["ts"] for m in bots)
    u["msg_count"] = max(int(u.get("msg_count", 0) or 0), len(users))
    u["tier"] = update_tier(u)

    # 对方最后一条如果还没被回复，记一次"未回"
    if users and bots:
        if max(m["ts"] for m in users) < max(m["ts"] for m in bots):
            u["bot_msgs_since_user"] = max(int(u.get("bot_msgs_since_user", 0) or 0), 1)
    return len(msgs)


async def fetch_recent(chat_key: str, limit: int = 16) -> List[Dict[str, Any]]:
    """取最近对话（含 bot 自己说的），用于推导由头"""
    if DBChatMessage is None:
        return []
    try:
        rows = (
            await DBChatMessage.filter(chat_key=chat_key)
            .order_by("-send_timestamp")
            .limit(limit)
        )
    except Exception as e:
        core.logger.warning(f"[proactive3] 查询历史失败: {e}")
        return []

    out: List[Dict[str, Any]] = []
    for r in reversed(rows):
        text = (r.content_text or "").strip()
        if not text:
            continue
        if text.startswith("≡NA≡") or text.startswith("[系统"):
            continue
        out.append(
            {
                "is_bot": str(r.sender_id) == "-1",
                "text": text,
                "ts": r.send_timestamp,
            }
        )
    return out


def now_label() -> str:
    """给 prompt 用的「现在」：现在是 2026-11-05 18:20（周四）"""
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"现在是 {now:%Y-%m-%d %H:%M}（周{week}）"


def _age_label(ts: float, now_ts: float) -> str:
    """『今天』『昨天』『3 天前』，给记忆条目标年龄用"""
    if not ts:
        return ""
    days = max(0.0, (now_ts - ts) / 86400.0)
    if days < 1:
        return "今天"
    if days < 2:
        return "昨天"
    return f"{days:.0f} 天前"


def _msg_time_prefix(ts: float, now_ts: float) -> str:
    """单条消息的时间前缀；PROMPT_TIME_MODE=off 时不加"""
    mode = str(getattr(config, "PROMPT_TIME_MODE", "clock") or "clock").lower()
    if mode == "off" or not ts:
        return ""
    if mode == "relative":
        mins = max(0.0, (now_ts - ts) / 60.0)
        if mins < 60:
            return f"[{mins:.0f} 分钟前] "
        if mins < 24 * 60:
            return f"[{mins / 60:.1f} 小时前] "
        return f"[{mins / 1440:.1f} 天前] "
    d = datetime.fromtimestamp(ts)
    if d.date() == datetime.fromtimestamp(now_ts).date():
        return f"[{d:%H:%M}] "
    return f"[{d:%m-%d %H:%M}] "


def transcript(msgs: List[Dict[str, Any]], limit: int = 12, now_ts: Optional[float] = None) -> str:
    """拼对话给模型看。带时间标记，模型才知道哪些话已经过时了。"""
    now_ts = float(now_ts) if now_ts else _now()
    lines = []
    for m in msgs[-limit:]:
        who = "我" if m["is_bot"] else "TA"
        t = (m["text"] or "").replace("\n", " ")[:120]
        if t:
            stamp = _msg_time_prefix(float(m.get("ts", 0) or 0), now_ts)
            lines.append(f"{stamp}{who}: {t}")
    return "\n".join(lines)


# ======================================================================== 模型


def _model_args() -> Dict[str, Any]:
    if core_config is None:
        return {}
    try:
        groups = core_config.MODEL_GROUPS or {}
        # v4.1.1：优先跟随主程序的 USE_MODEL_GROUP，和 NA 自己用同一个分组
        prefer = str(getattr(core_config, "USE_MODEL_GROUP", "") or "").strip()
        order: List[str] = []
        for key in (prefer, "default"):
            if key and key not in order:
                order.append(key)
        for key in groups.keys():
            if key not in order:
                order.append(key)
        for key in order:
            g = groups.get(key)
            if g and getattr(g, "MODEL_TYPE", "chat") == "chat" and getattr(g, "API_KEY", ""):
                if prefer and key != prefer and not _MODEL_PREFER_WARNED["done"]:
                    _MODEL_PREFER_WARNED["done"] = True
                    core.logger.warning(
                        f"[proactive3] 主模型分组 {prefer!r} 不可用（不存在/不是 chat/无 API Key），"
                        f"回退到 {key!r}"
                    )
                return {
                    "model": g.CHAT_MODEL,
                    "api_key": g.API_KEY,
                    "base_url": g.BASE_URL,
                    "group": key,
                }
    except Exception as e:
        core.logger.warning(f"[proactive3] 读取模型配置失败: {e}")
    return {}


_NO_MODEL_LOGGED = {"done": False}
_MODEL_PREFER_WARNED = {"done": False}
_GROUP_WARN_TS: Dict[str, float] = {}


def _warn_group_once(chat_key: str) -> None:
    """群闸门无答案的告警：同一群一小时内只写一次"""
    now = _now()
    last = float(_GROUP_WARN_TS.get(chat_key, 0) or 0)
    if now - last >= 3600:
        _GROUP_WARN_TS[chat_key] = now
        core.logger.warning(f"[proactive3] 群聊闸门无有效回复，按否决 {chat_key}")


def _log_no_model_once() -> None:
    """没有可用模型时，只在启动后提示一次（避免每轮刷日志）"""
    if not _NO_MODEL_LOGGED["done"]:
        _NO_MODEL_LOGGED["done"] = True
        core.logger.warning("[proactive3] 没有可用的对话模型，模型闸门按关闭处理（不会否决）")


def _first_verdict(ans: str) -> Optional[str]:
    """取回复里第一个 ALLOW / SKIP；找不到返回 None（= 无有效答案）"""
    m = re.search(r"\b(ALLOW|SKIP)\b", (ans or "").upper())
    return m.group(1) if m else None


async def llm_chat(prompt: str, max_tokens: int = 200, timeout: float = 25.0, temperature: float = 0.8) -> str:
    """直接调模型（闸门/抽取用）。带硬超时：超时当作"无意见"，绝不卡住主线。"""
    if gen_openai_chat_response is None:
        return ""
    args = _model_args()
    if not args:
        return ""
    try:
        resp = await asyncio.wait_for(
            gen_openai_chat_response(
                model=args["model"],
                messages=[{"role": "user", "content": prompt}],
                api_key=args.get("api_key") or None,
                base_url=args.get("base_url") or None,
                temperature=temperature,
                # 思考型模型（如 gemini-3.7-flash）会先花 token 思考，
                # 给得太少会"正文为空"，所以下限抬到 200
                max_tokens=max(200, max_tokens),
                max_wait_time=max(10, int(timeout)),
            ),
            timeout=timeout,
        )
        # NA 的响应对象字段是 response_content（不是 content）
        content = str(getattr(resp, "response_content", "") or "").strip()
        if not content:
            content = str(getattr(resp, "content", "") or "").strip()
        return content
    except asyncio.TimeoutError:
        core.logger.warning(f"[proactive3] 模型超时({timeout}s)，本轮跳过判定")
        return ""
    except asyncio.CancelledError:
        raise
    except Exception as e:
        core.logger.warning(f"[proactive3] 模型调用失败: {type(e).__name__}: {e}")
        return ""


# ======================================================================== 生活场景

LIFE_EVENTS: List[Dict[str, str]] = [
    {"hours": "0-6", "scene": "刚做噩梦惊醒，心里发慌，想找人说话"},
    {"hours": "0-6", "scene": "睡不着，翻来覆去，脑子停不下来"},
    {"hours": "0-6", "scene": "梦到你了，醒来有点恍惚"},
    {"hours": "0-6", "scene": "半夜饿了，翻冰箱只找到点零食"},
    {"hours": "0-6", "scene": "外面打雷，有点怕，睡不着"},
    {"hours": "6-11", "scene": "刚醒，还赖在床上不想起"},
    {"hours": "6-11", "scene": "起床气，被吵醒了很不爽"},
    {"hours": "6-11", "scene": "在吃早餐，想着今天要做什么"},
    {"hours": "6-11", "scene": "看窗外天气，想起你冷不冷"},
    {"hours": "11-15", "scene": "在吃午饭，纠结吃什么"},
    {"hours": "11-15", "scene": "午睡做了个短梦，醒来懵懵的"},
    {"hours": "11-15", "scene": "下午犯困，眼皮打架"},
    {"hours": "14-18", "scene": "有点无聊，不知道干什么好"},
    {"hours": "14-18", "scene": "看到个有意思的东西，想给你看"},
    {"hours": "14-18", "scene": "突然很想你，说不出为什么"},
    {"hours": "14-18", "scene": "有点累，想被人哄一下"},
    {"hours": "14-18", "scene": "做了件小事，想被夸"},
    {"hours": "17-20", "scene": "在吃晚饭，一个人吃有点冷清"},
    {"hours": "17-20", "scene": "看到夕阳很好看，想你也在就好了"},
    {"hours": "17-20", "scene": "一天结束了，有点空落落的"},
    {"hours": "19-24", "scene": "在追剧，看到个情节想跟你吐槽"},
    {"hours": "19-24", "scene": "听到一首歌，想到你"},
    {"hours": "19-24", "scene": "想找人聊天，第一个想到你"},
    {"hours": "19-24", "scene": "想撒个娇"},
    {"hours": "19-24", "scene": "今天有个小成就，想跟你分享"},
    {"hours": "19-24", "scene": "有点低落，不太想说为什么"},
    {"hours": "19-24", "scene": "想知道你今天过得怎么样"},
    {"hours": "19-24", "scene": "困了但还不想睡，想再聊会儿"},
    {"hours": "any", "scene": "让你猜猜我在干嘛"},
    {"hours": "any", "scene": "有个小秘密想告诉你"},
    {"hours": "any", "scene": "翻到以前的聊天记录，看着看着笑了"},
    {"hours": "any", "scene": "看到个东西像你，忍不住想发给你"},
    {"hours": "any", "scene": "嘴馋了，想吃点好的"},
    {"hours": "any", "scene": "想听听你的声音"},
    {"hours": "any", "scene": "想要摸摸头"},
    {"hours": "any", "scene": "有点担心你，最近是不是太累了"},
    {"hours": "any", "scene": "假装生气，等你来哄"},
    {"hours": "any", "scene": "知道了个冷知识，想讲给你听"},
]


def pick_life_event() -> Optional[str]:
    h = datetime.now().hour
    pool = [e["scene"] for e in LIFE_EVENTS if e["hours"] == "any"]
    for e in LIFE_EVENTS:
        if e["hours"] == "any":
            continue
        try:
            a, b = e["hours"].split("-")
            if int(a) <= h < int(b):
                pool.append(e["scene"])
        except Exception:
            continue
    return random.choice(pool) if pool else None


# ======================================================================== 关系与拟人


def affection_score(u: Dict[str, Any]) -> int:
    """好感度估算（0-100）：互动量 + 近期性 + 回复率"""
    n = int(u.get("msg_count", 0) or 0)
    score = min(70.0, n * 0.35)

    lu = float(u.get("last_user_msg_ts", 0) or 0)
    if lu > 0:
        d = days_since(lu)
        if d <= 1:
            score += 20
        elif d <= 3:
            score += 12
        elif d <= 7:
            score += 6
        elif d <= 30:
            score += 2

    # 修复 B4：回复率真的是 0.0 时不能被 `or` 当成 0.7
    _rr = u.get("reply_rate", None)
    rate = 0.7 if _rr is None else float(_rr)
    score += (rate - 0.5) * 20

    return int(max(0, min(100, score)))


def is_special_care(u: Dict[str, Any]) -> bool:
    """名单内 + 自动认定"""
    uid = str(u["chat_key"]).split("_")[-1]
    if uid in [str(x) for x in (config.SPECIAL_CARE_UIDS or [])]:
        return True
    if config.SPECIAL_CARE_AUTO:
        if u.get("auto_special"):
            return True
        if (
            affection_score(u) >= config.SPECIAL_CARE_MIN_AFFECTION
            and int(u.get("msg_count", 0) or 0) >= config.SPECIAL_CARE_MIN_MESSAGES
        ):
            u["auto_special"] = True
            return True
    return False


# 生病关键词（沿用旧版 v2 的经验）
CARE_KEYWORDS = (
    "头疼", "头痛", "发烧", "发热", "感冒", "难受", "不舒服", "胃疼", "胃痛",
    "牙疼", "牙痛", "嗓子疼", "喉咙疼", "咳嗽", "流鼻涕", "鼻塞", "拉肚子",
    "腹泻", "恶心", "想吐", "过敏", "受伤", "扭到", "疼死", "失眠", "姨妈",
    "痛经", "低烧", "高烧", "中暑", "犯困",
)


def detect_care(text: str) -> Optional[str]:
    if not text or len(text) > 300:
        return None
    # 修复 B1：命中否定词不算（"不头疼了""没发烧"）
    for k in CARE_KEYWORDS:
        start = 0
        while True:
            i = text.find(k, start)
            if i < 0:
                break
            head = text[max(0, i - 4):i]
            if not any(n in head for n in _NEGATIONS):
                return k
            start = i + len(k)
    return None


_typing_tasks: set = set()


async def _typing_once(chat_key: str) -> None:
    """内部：发一次「正在输入」（带超时，永不阻塞主流程）"""
    try:
        from nekro_agent.adapters.onebot_v11.core.bot import get_bot

        uid = int(str(chat_key).split("_")[-1])
        bot = get_bot()
        await asyncio.wait_for(
            bot.call_api("set_input_status", user_id=uid, event_type=1),
            timeout=3.0,
        )
    except Exception as e:
        if config.DEBUG:
            core.logger.info(f"[proactive3] 正在输入不可用: {type(e).__name__}")


async def show_typing(chat_key: str, seconds: float = 0.0) -> None:
    """显示「正在输入」——后台任务，绝不阻塞发送"""
    if not config.TYPING_ENABLED:
        return
    if "-private_" not in chat_key:
        return
    try:
        # 修复 C7：任务持引用，避免被 GC 提前回收
        _task = asyncio.create_task(_typing_once(chat_key))
        _typing_tasks.add(_task)
        _task.add_done_callback(_typing_tasks.discard)
    except Exception:
        pass


def greeting_window(now: datetime) -> Optional[str]:
    """当前落在哪个问候窗口"""
    if in_range(now, config.GREETING_MORNING_START, config.GREETING_MORNING_END):
        return "morning"
    if in_range(now, config.GREETING_MIDDAY_START, config.GREETING_MIDDAY_END):
        return "midday"
    if in_range(now, config.GREETING_NIGHT_START, config.GREETING_NIGHT_END):
        return "night"
    return None


# ======================================================================== 群聊状态


def get_group(st: Dict[str, Any], chat_key: str) -> Dict[str, Any]:
    groups = st.setdefault("groups", {})
    g = groups.get(chat_key)
    if not g:
        g = {
            "chat_key": chat_key,
            "last_msg_ts": 0,
            "last_bot_ts": 0,
            "flow_until": 0,
            "interjects": 0,
            "interject_ts": [],
            "ignored_streak": 0,
            "topic_date": "",
            "topic_count": 0,
            "ice_date": "",
            "ice_count": 0,
            "flow_date": "",
        }
        groups[chat_key] = g
    return g


def group_allowed(chat_key: str) -> bool:
    wl = [str(x) for x in (config.GROUP_FLOW_WHITELIST or [])]
    if not wl:
        return True
    gid = str(chat_key).split("_")[-1]
    return gid in wl



# ======================================================================== 动机层（v4）


# 什么算"重要的事"（权重高，记得久）
_IMPORTANT_WORDS = (
    "面试", "考试", "答辩", "体检", "手术", "住院", "分手", "吵架", "辞职",
    "入职", "搬家", "生日", "纪念日", "结婚", "毕业", "签证", "出差",
    "去世", "崩溃",
)

# 对方说这些 → 说明在忙/累（抑制"想你"类）
_BUSY_WORDS = (
    "好累", "累死", "累坏", "加班", "在忙", "忙死", "好忙", "开会", "开完会",
    "赶工", "deadline", "通宵", "熬夜", "没睡", "好困", "困死", "想睡", "撑不住",
)

# 敏感内容不进记忆（防污染）
_MEM_BLOCK_WORDS = (
    "色情", "约炮", "裸聊", "黄片", "自慰", "性交", "卖淫",
    "赌博", "博彩", "下注", "六合彩", "百家乐",
    "冰毒", "海洛因", "毒品", "枪支", "炸药",
    "诈骗", "洗钱", "代开发票", "办假证", "人肉搜索",
    "自杀", "割腕", "怎么死",
)


def memory_allowed(text: str) -> bool:
    """这句话能不能进记忆（敏感内容不记，免得以后翻旧账时翻出来）"""
    s = text or ""
    return not any(w in s for w in _MEM_BLOCK_WORDS)


# 对方说这些 → 说明状态不错（疲惫降、兴致升）
_GOOD_WORDS = (
    "睡饱", "睡好", "精神", "开心", "舒服", "放松", "顺利", "搞定", "完成了",
    "笑死", "好耶",
)


# 修复 B1：命中关键词前 1~3 字内出现否定词就不算
_NEGATIONS = ("不", "没", "别", "不太", "没那么", "一点也不", "从不")


def hit_without_negation(s: str, words) -> bool:
    """关键词命中且前面没有否定词"""
    text = s or ""
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            head = text[max(0, i - 4):i]
            if not any(n in head for n in _NEGATIONS):
                return True
            start = i + len(w)
    return False


def memory_weight(text: str) -> float:
    """给一句话打权重：大事 1.8，日常 0.6，其它 1.0"""
    s = text or ""
    if any(w in s for w in _IMPORTANT_WORDS):
        return 1.8
    if len(s) <= 6:
        return 0.6
    if any(w in s for w in ("？", "?", "吗", "怎么", "为什么", "干嘛", "啥", "咋", "多少", "哪")):
        return 0.9
    return 1.0


def decayed_weight(item: Dict[str, Any], now: float) -> float:
    """按半衰期衰减后的权重"""
    base = float(item.get("weight", 1.0))
    ts = float(item.get("ts", 0) or 0)
    if ts <= 0:
        return base
    days = max(0.0, (now - ts) / 86400.0)
    half = max(1.0, float(config.MEMORY_HALF_LIFE_DAYS))
    return base * (0.5 ** (days / half))


def add_memory(u: Dict[str, Any], text: str, kind: str = "topic") -> None:
    """记一条带权重的记忆，顺手清掉该忘的"""
    s = (text or "").strip()
    if not s:
        return
    if not memory_allowed(s):
        if config.DEBUG:
            core.logger.info("[proactive3] 敏感内容不进记忆")
        return
    now = _now()
    mems = u.setdefault("memories", [])
    w = memory_weight(s)
    mems.append({"text": s[:100], "weight": w, "ts": now, "kind": kind})

    # 衰减 + 遗忘
    keep = []
    for m in mems:
        if decayed_weight(m, now) >= config.MEMORY_MIN_WEIGHT:
            keep.append(m)
    keep.sort(key=lambda m: decayed_weight(m, now), reverse=True)
    u["memories"] = keep[: max(5, int(config.MEMORY_MAX_ITEMS))]


def top_memories(u: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    """按当前权重取最重要的几条（她"记得住"的东西）"""
    now = _now()
    mems = u.get("memories") or []
    scored = [(decayed_weight(m, now), m) for m in mems]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for w, m in scored[:limit] if w >= config.MEMORY_MIN_WEIGHT]


def note_busy(u: Dict[str, Any], text: str) -> None:
    """对方说忙/累 → 记一笔，一段时间内别推想你

    修复 B1：关键词前有否定词不算（"今天不累""不忙"）。
    修复 B7：好状态 30 分钟内只生效一次，避免连说几句"哈哈"把疲惫/兴致刷满。
    """
    s = text or ""
    now = _now()
    if hit_without_negation(s, _BUSY_WORDS):
        u["user_busy_until"] = now + config.BUSY_QUIET_HOURS * 3600
        u["tired"] = min(100.0, float(u.get("tired", 20.0)) + 8.0)
        u["mood"] = max(0.0, float(u.get("mood", 60.0)) - 5.0)
    if hit_without_negation(s, _GOOD_WORDS):
        last_good = float(u.get("good_state_ts", 0) or 0)
        if now - last_good >= 30 * 60:
            u["good_state_ts"] = now
            u["tired"] = max(0.0, float(u.get("tired", 20.0)) - 6.0)
            u["mood"] = min(100.0, float(u.get("mood", 60.0)) + 8.0)


def urge_scale(u: Dict[str, Any], weekend: Optional[bool] = None) -> float:
    """念头增速系数（修复 B4 + 可测试）"""
    scale = max(0.0, config.ACTIVITY_LEVEL / 75.0)
    is_weekend = datetime.now().weekday() >= 5 if weekend is None else weekend
    if is_weekend:
        scale *= 1.2
    if config.ADAPTIVE_REPLY_RATE and int(u.get("pings", 0) or 0) >= 3:
        _r = u.get("reply_rate", None)
        _r = 1.0 if _r is None else max(0.0, min(1.0, float(_r)))
        scale *= max(0.5, 0.5 + 0.5 * _r)
    return scale


def heartbeat_wait(now_dt: datetime, base_seconds: float) -> float:
    """修复 B6：安静时段里若 QUIET_END 落在本次睡眠区间内，截短到 QUIET_END + 60 秒"""
    try:
        if not config.QUIET_END_WAKE or not in_quiet(now_dt):
            return base_seconds
        eh, em = _parse_hm(config.QUIET_END)
        end = now_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end <= now_dt:
            end = end + timedelta(days=1)
        to_end = (end - now_dt).total_seconds() + 60
        if 0 < to_end < base_seconds:
            return max(60.0, to_end)
    except Exception:
        pass
    return base_seconds


def drift_mood(u: Dict[str, Any], minutes: float) -> None:
    """疲惫/兴致随时间自然漂移

    修复 A1：疲惫值原来只涨不落，第一晚之后轻由头几乎全被压住。
    现在：
      · 单次推进封顶 60 分钟，避免长时间停机后一次冲到 100
      · 安静时段（in_quiet）当成她休息的时间，疲惫按固定速率恢复，而不是继续涨
      · 其余时段保持原来的节奏
    """
    h = datetime.now().hour
    tired = float(u.get("tired", 20.0))
    mood = float(u.get("mood", 60.0))
    minutes = max(0.0, min(float(minutes), 60.0))

    quiet = False
    try:
        quiet = in_quiet()
    except Exception:
        quiet = False

    if quiet:
        # 安静时段 = 她在睡觉：疲劳按 0.05/分 恢复（最长的 7 小时约 -21）
        tired -= 0.05 * minutes
    elif h >= 23 or h < 6:
        tired += 0.15 * minutes
    elif 6 <= h < 10:
        tired -= 0.05 * minutes
    else:
        tired += 0.01 * minutes

    # 太久没人说话，兴致慢慢降
    lu = float(u.get("last_user_msg_ts", 0) or 0)
    if lu > 0 and (_now() - lu) > 6 * 3600:
        mood -= 0.02 * minutes

    u["tired"] = max(0.0, min(100.0, tired))
    u["mood"] = max(0.0, min(100.0, mood))


# 哪些由头算"轻社交"（累了/对方忙的时候该忍住）
_LIGHT_KINDS = ("miss", "life", "greeting", "topic")

# 哪些算"重"（再累也可以说）
_HEAVY_KINDS = ("care", "probe", "followup", "cue", "continue", "closer", "loop")


def check_suppression(u: Dict[str, Any], kind: str) -> Optional[str]:
    """要不要把这次开口压住（返回压制原因；None = 放行）"""
    if not config.SUPPRESS_ENABLED:
        return None

    now = _now()
    light = kind in _LIGHT_KINDS

    reason = None
    if light and float(u.get("tired", 0)) >= config.SUPPRESS_TIRED_THRESHOLD:
        reason = f"太累了({float(u.get('tired', 0)):.0f})"
    elif kind == "miss" and now < float(u.get("user_busy_until", 0) or 0):
        reason = "对方在忙/累"
    elif light and now < float(u.get("restraint_until", 0) or 0):
        reason = "刚反省过，收着点"
    elif light and config.MOOD_ENABLED and float(u.get("mood", 60)) <= config.MOOD_LOW_THRESHOLD:
        reason = f"没兴致({float(u.get('mood', 60)):.0f})"

    if reason is None:
        # 这次不压 → 清掉这个 kind 的计数
        sc = u.setdefault("suppress_count", {})
        if sc.get(kind):
            sc[kind] = 0
        return None

    # 执念：同一个由头被压太多次 → 允许一次"忍不住"
    sc = u.setdefault("suppress_count", {})
    cnt = int(sc.get(kind, 0) or 0) + 1
    if cnt >= config.OBSESSION_BREAK_THRESHOLD:
        sc[kind] = 0
        u.setdefault("suppress_log", []).append({"ts": now, "kind": kind, "reason": "忍不住了"})
        u["suppress_log"] = u["suppress_log"][-20:]
        core.logger.info(f"[proactive3] 压了 {cnt} 次，这次不忍了({kind})")
        return None

    sc[kind] = cnt
    u.setdefault("suppress_log", []).append({"ts": now, "kind": kind, "reason": reason})
    u["suppress_log"] = u["suppress_log"][-20:]
    if config.DEBUG:
        core.logger.info(f"[proactive3] 忍住({kind})：{reason} 第{cnt}次")
    return reason


def run_reflection(u: Dict[str, Any]) -> Optional[str]:
    """事后反省：主动发完一段时间没人回 → 情绪下降 + 收敛期"""
    if not config.REFLECT_ENABLED:
        return None
    at = float(u.get("reflect_at", 0) or 0)
    if at <= 0:
        return None

    now = _now()
    if (now - at) < config.REFLECT_AFTER_MINUTES * 60:
        return None

    replied = float(u.get("last_user_msg_ts", 0) or 0) > at
    u["reflect_at"] = 0

    if replied:
        u["reflect_count"] = 0
        u["no_reply_count"] = 0
        u["mood"] = min(100.0, float(u.get("mood", 60.0)) + 6.0)
        u["tired"] = max(0.0, float(u.get("tired", 20.0)) - 4.0)
        return None

    u["reflect_count"] = int(u.get("reflect_count", 0) or 0) + 1
    u["no_reply_count"] = int(u.get("no_reply_count", 0) or 0) + 1
    u["mood"] = max(0.0, float(u.get("mood", 60.0)) - 12.0)
    # 修复 A3：反省不再动 closer_count（那个只表示"收场发了几次"）
    if int(u["no_reply_count"]) >= 2:
        u["restraint_until"] = now + config.RESTRAINT_HOURS * 3600
        core.logger.info(
            f"[proactive3] 反省：连续 {u['reflect_count']} 次没人回，收敛 "
            f"{config.RESTRAINT_HOURS:.0f} 小时"
        )
        return "收敛"
    core.logger.info("[proactive3] 反省：上一条没人回，记一笔")
    return "记一笔"


# ======================================================================== 语义工具

# 敷衍回复的特征：很短、没有疑问、多是应答词
_PERFUNCTORY_WORDS = {
    "嗯", "嗯嗯", "哦", "哦哦", "好", "好的", "行", "行吧", "可以", "知道了",
    "收到", "ok", "OK", "Ok", "在", "在的", "来了", "对", "是的", "哈哈",
    "666", "1", "。。", "…", "...", "草", "好耶", "随便", "都行",
}


# 填充词字符集：整句只由这些字组成时才算敷衍
_FILLER_CHARS = set("嗯哦好行可以的在吗呀啊哈嘿嘻对是的没那这个了吧呢啦嘛嘞嘛~～.!！。…,，、 　")


def is_perfunctory(text: str) -> bool:
    """判断对方是不是敷衍了一句（只认纯应答，不误伤正常短句）

    修复 B2：
      · 空文本（可能是纯图片/表情）不再算敷衍
      · ≤2 字只在白名单里才算敷衍（"救命""别走""难受" 不算）
      · 填充字符集去掉数字和标点（"123"、". . ." 不算纯填充，"…" 仍在白名单里）
    """
    s = (text or "").strip()
    if not s:
        return False
    if s in _PERFUNCTORY_WORDS:
        return True
    if len(s) <= 2:
        return False
    if len(s) <= 8 and set(s) <= _FILLER_CHARS:
        return True
    return False


# 时间锚点：对方说了"明天/今晚/周末"之类，到点可以问结果
# （B3 之后具体正则写在 extract_cues 里，这里只留说明，不再有 _CUE_PATTERNS）

_CN_WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def extract_cues(text: str, now_ts: float) -> List[Dict[str, Any]]:
    """从对方的话里抽出『到点该问』的时间锚点（纯规则，不依赖模型）"""
    import re

    s = (text or "").strip()
    if not s or len(s) > 300:
        return []

    now = datetime.fromtimestamp(now_ts)
    out: List[Dict[str, Any]] = []

    def _at(days_ahead: int, hour: int) -> float:
        d = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return d.timestamp()

    # 修复 B3：一句话只记一个最具体的锚点；时段映射到小时；"等会儿"= +45 分钟
    _HOUR_OF_DAY = {"早上": 8, "上午": 10, "中午": 12, "下午": 15, "晚上": 20}
    _TOD = r"(早上|上午|中午|下午|晚上)"

    def _hour_from(m) -> int:
        if m and m.groups():
            for g in m.groups():
                if g and g in _HOUR_OF_DAY:
                    return _HOUR_OF_DAY[g]
        return 20

    def _plus_minutes(minutes: int) -> float:
        return (now + timedelta(minutes=minutes)).timestamp()

    cands = []   # (优先级, due_ts)  优先级越小越具体
    # 1) 带具体日期的
    m = re.search(r"明天" + _TOD + "?", s)
    if m:
        cands.append((1, _at(1, _hour_from(m))))
    m = re.search(r"后天" + _TOD + "?", s)
    if m:
        cands.append((1, _at(2, _hour_from(m))))
    m = re.search(r"今晚|今天晚上", s)
    if m:
        cands.append((1, _at(0, 20)))
    m = re.search(r"(?<!下)下周([一二三四五六日天])?", s)
    if m:
        ch = m.group(1) if m.groups() and m.group(1) else "一"
        target_wd = _CN_WEEK.get(ch, 0)
        ahead = (target_wd - now.weekday()) % 7 or 7
        cands.append((1, _at(ahead, 20)))
    m = re.search(r"周末", s)
    if m:
        wd = now.weekday()
        ahead = (5 - wd) % 7
        if ahead == 0 and now.hour >= 18:
            ahead = 7
        cands.append((1, _at(ahead, 20)))
    # 2) 事件类（没写具体哪天，默认明天对应时段）
    m = re.search(r"考试|面试|体检|复查|答辩", s)
    if m:
        tm = re.search(_TOD, s[max(0, m.start() - 6): m.end() + 6])
        cands.append((2, _at(1, _hour_from(tm) if tm else 20)))
    # 3) 等会儿 / 待会儿 / 稍后 → +45 分钟
    if re.search(r"等会(儿)?|待会(儿)?|一会儿|稍后", s):
        cands.append((3, _plus_minutes(45)))
    # 4) 下班后
    if re.search(r"下班后|下班", s):
        cands.append((4, _at(0, 18)))

    if not cands:
        return []
    cands.sort(key=lambda x: x[0])
    due = cands[0][1]
    if due <= now_ts:
        return []
    out.append({"text": s[:40], "due_ts": due, "used": False})
    return out


def update_tier(u: Dict[str, Any]) -> str:
    """按互动量估关系档（有天使记忆时可被外部覆盖）"""
    n = int(u.get("msg_count", 0))
    _lu = float(u.get("last_user_msg_ts", 0) or 0)
    gap_days = days_since(_lu) if _lu > 0 else 0.0
    if n >= 300 or (n >= 120 and gap_days < 2):
        return "high"
    if n >= 60 or gap_days < 7:
        return "mid"
    if gap_days > 30 and n < 20:
        return "low"
    return u.get("tier", "mid")


# ======================================================================== 硬闸


def can_send(u: Dict[str, Any], special: bool) -> Tuple[bool, str]:
    now = _now()

    if not special and now < float(u.get("silent_until", 0)):
        return False, "停发期（对方太久没回）"

    if in_quiet() and not (special and config.SPECIAL_CARE_IGNORE_QUIET):
        return False, "安静时段"

    ensure_today(u)

    if int(u.get("sent_today", 0)) >= config.MAX_SENDS_PER_DAY:
        return False, "达每日上限"

    gap = config.SPECIAL_CARE_GAP_MINUTES if special else max(1, config.USER_COOLDOWN_MINUTES)
    if minutes_since(u.get("last_proactive_ts", 0)) < gap:
        return False, f"间隔不足({gap}分)"

    if minutes_since(u.get("last_user_msg_ts", 0)) < config.RECENT_TALK_MINUTES:
        return False, "刚聊过"

    return True, ""


# ======================================================================== 由头


def due_pending(u: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = _now()
    out = []
    for p in u.get("pending", []):
        if p.get("done"):
            continue
        if float(p.get("due_ts", 0)) <= now:
            out.append(p)
    return out


# 修复 A8：同一 kind 未完成的只留一条；取用时按重要性挑，并丢弃过期的
_PENDING_PRIORITY = {"care": 0, "probe": 1, "followup": 2, "continue": 3}
_PENDING_MAX_AGE = {"care": 12 * 3600.0}


def queue_pending(u: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    """入队：同 kind 且未完成的只保留一条，新的刷新文本和到期时间"""
    kind = item.get("kind")
    items = u.setdefault("pending", [])
    for p in items:
        if (not p.get("done")) and p.get("kind") == kind:
            p["text"] = item.get("text", p.get("text"))
            p["due_ts"] = item.get("due_ts", p.get("due_ts"))
            p["refreshed_ts"] = _now()
            return p
    items.append(item)
    return item


def pending_max_age(kind: str) -> float:
    return float(_PENDING_MAX_AGE.get(kind, config.FOLLOWUP_MAX_MINUTES * 60 * 2))


def sweep_expired(u: Dict[str, Any]) -> int:
    """把过期的待办标掉（不管本轮能不能派上由头）。
    返回被标掉的条数；调用方有变化时才写盘。"""
    n = 0
    now_ts = _now()
    for p in u.get("pending", []) or []:
        if p.get("done"):
            continue
        kind_p = str(p.get("kind", "followup"))
        if now_ts - float(p.get("due_ts", 0) or 0) > pending_max_age(kind_p):
            p["done"] = True
            p["expired"] = True
            n += 1
    if n and config.DEBUG:
        core.logger.info(f"[proactive3] 过期清理 {n} 条待办")
    return n


async def derive_reason(
    u: Dict[str, Any],
    msgs: List[Dict[str, Any]],
    dry: bool = False,
) -> Optional[Tuple[str, str, Any]]:
    """找一个由头 → (描述, 类型, 附带引用)

    dry=True 时只预览：不写任何状态（不推进回访冷却、不记问候）。
    """


    # 1) 到期未完话题（追问/接话/回访）
    # 注意：这里不标记 done —— 万一被闸门否决，条子要留着下次再用
    pend = due_pending(u)
    if pend:
        # 修复 A8：先把过期的全部标掉（不管排不排得上），再按重要性挑
        _now_ts = _now()
        alive = []
        for p in pend:
            kind_p = str(p.get("kind", "followup"))
            _age = _now_ts - float(p.get("due_ts", 0) or 0)
            if _age > pending_max_age(kind_p):
                if not dry:
                    p["done"] = True
                    p["expired"] = True
                continue
            alive.append(p)

        pend = sorted(alive, key=lambda p: _PENDING_PRIORITY.get(str(p.get("kind")), 9))
        for p in pend:
            kind_p = str(p.get("kind", "followup"))
            # 跨段护栏：两次接话/追问之间至少隔 FOLLOWUP_COOLDOWN_MINUTES
            _cool = kind_p in ("probe", "followup", "continue") and (
                minutes_since(u.get("last_followup_ts", 0)) < config.FOLLOWUP_COOLDOWN_MINUTES
            )
            if _cool:
                if config.DEBUG:
                    core.logger.info("[proactive3] 接话冷却中，先找别的由头")
                continue
            return str(p["text"]), kind_p, p

    # 2) 时间锚点（同样：发送成功后才标记 used）
    now = _now()
    for c in u.get("cues", []):
        if not c.get("used") and float(c.get("due_ts", 0)) <= now <= float(c.get("due_ts", 0)) + 6 * 3600:
            return f"之前提过「{c.get('text', '那件事')}」，差不多到时候了，问问结果", "cue", c

    # 3) 回访：从对话里找没结果的事（带冷却；太旧的对话不翻）
    loop_cd = 30 * 60
    _lu2 = float(u.get("last_user_msg_ts", 0) or 0)
    if (
        config.LOOP_ENABLED
        and _lu2 > 0
        and hours_since(_lu2) <= config.LOOP_MAX_HOURS
        and now >= float(u.get("last_loop_check_ts", 0)) + loop_cd
        and minutes_since(u.get("last_proactive_ts", 0)) > config.LOOP_MIN_HOURS * 60
    ):
        if not dry:
            u["last_loop_check_ts"] = now
        topic = await extract_unresolved(msgs, top_memories(u, 5))
        if topic:
            return topic, "loop", None

    # 4) 问候（必须真的有过对话，否则新用户会被误判成"好久没说话"）
    last_user = float(u.get("last_user_msg_ts", 0) or 0)
    if config.GREETING_ENABLED and last_user > 0 and hours_since(last_user) > config.GREETING_GAP_HOURS:
        win = greeting_window(datetime.now())
        greeted = u.setdefault("greeted", {})
        if win and greeted.get(win) != _today():
            label = {"morning": "早安", "midday": "午间", "night": "晚安"}.get(win, "问候")
            # 修复 A2：这里只给由头，不写 greeted；发送成功后由 tick 记账
            return f"{label}问候（对方挺久没说话了，主动打个招呼）", "greeting", {"win": win}

    # 5) 想念通道（关系够近才允许说想你了）
    if (
        config.MISS_ENABLED
        and affection_score(u) >= config.MISS_AFFECTION_MIN
        and int(u.get("miss_today", 0) or 0) < config.MISS_DAILY_CAP
        and (not config.MOOD_ENABLED or float(u.get("mood", 60)) > config.MOOD_LOW_THRESHOLD)
        and random.random() < 0.3
    ):
        return "突然很想对方，说不清为什么，就是想让他知道你惦记着他", "miss", None

    # 6) 生活场景兜底（没兴致时不说琐事）
    if config.FALLBACK_EVENTS and (
        not config.MOOD_ENABLED or float(u.get("mood", 60)) > config.MOOD_LOW_THRESHOLD
    ):
        ev = pick_life_event()
        if ev:
            return ev, "life", None

    return None


async def extract_unresolved(msgs: List[Dict[str, Any]], memories: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    """从最近对话 / 权重最高的记忆里，找一件『提过但没说结果』的事"""
    now_ts = _now()
    convo = transcript(msgs, 14, now_ts) if msgs else ""
    mem_lines = ""
    if memories:
        mem_lines = "\n".join(
            f"- {m.get('text', '')[:60]}"
            + (f"（{_age_label(float(m.get('ts', 0) or 0), now_ts)}）" if m.get("ts") else "")
            for m in memories[:5]
        )

    if not convo and not mem_lines:
        return None
    if len(msgs) < 4 and not mem_lines:
        return None

    prompt = (
        f"{now_label()}\n\n"
        "从下面的材料里找出一件「对方提过、但还没说结果」的具体的事"
        "（考试/面试/吃饭/去哪/身体/心情等）。\n"
        "优先挑分量重的事（大事、情绪强的事），别挑鸡毛蒜皮。\n"
        "方括号里是消息的真实时间，用来判断新旧：\n"
        "- 一次性的小事（吃饭/天气/在干嘛）过了 2 小时就别再问，会显得脱节；\n"
        "- 有结果可追的重要事（面试/看病/考试/出差）不受时间限制，只要还没结果就能问。\n"
        "找到了就用一句话描述（20字内）；找不到只回 NONE。\n\n"
    )
    if mem_lines:
        prompt += f"【她记得比较重的事】\n{mem_lines}\n\n"
    if convo:
        prompt += f"【最近对话】\n{convo}\n"

    ans = (await llm_chat(prompt, max_tokens=60, timeout=25.0, temperature=0.3)).strip().replace("\n", " ")[:60]
    if not ans or "NONE" in ans.upper():
        return None
    return f"想起对方之前提过「{ans}」，不知道后来怎么样了，问问"


# ======================================================================== 发送


async def gate(u: Dict[str, Any], reason: str, msgs: List[Dict[str, Any]]) -> bool:
    """模型闸门：这一刻该不该说"""
    if not config.LLM_GATE:
        return True

    now = _now()
    if now < float(u.get("gate_skip_until", 0)):
        return False

    convo = transcript(msgs, 8)
    prompt = (
        f"{now_label()}\n\n"
        "判断现在要不要主动给对方发一条消息。\n\n"
        f"【由头】{reason}\n\n"
        f"【最近对话】\n{convo or '（最近没有对话）'}\n\n"
        f"【距对方上次说话】{minutes_since(u.get('last_user_msg_ts', 0)):.0f} 分钟\n\n"
        "标准：由头自然、现在说合适 → ALLOW；显得多余/打扰/没必要 → SKIP\n"
        "只回一个词：ALLOW 或 SKIP"
    )
    # 修复 A9：三态闸门
    #   没有可用模型 → 视为关闭（放行），只在启动时提示一次
    #   有模型但本次没答案（超时/异常/空）→ 否决 + 冷却
    #   有答案 → 取第一个 ALLOW|SKIP 判定
    if not _model_args():
        _log_no_model_once()
        return True

    ans = (await llm_chat(prompt, max_tokens=200, timeout=20.0, temperature=0.3)).upper()
    verdict = _first_verdict(ans)
    if verdict is None:
        u["gate_skip_until"] = now + config.SKIP_COOLDOWN_MINUTES * 60
        core.logger.warning(f"[proactive3] 闸门无有效回复，按否决处理: {reason[:40]}")
        return False
    if verdict == "SKIP":
        u["gate_skip_until"] = now + config.SKIP_COOLDOWN_MINUTES * 60
        core.logger.info(f"[proactive3] 闸门否决: {reason[:40]}")
        return False
    return True


async def send_with_reason(u: Dict[str, Any], reason: str, kind: str) -> bool:
    chat_key = u["chat_key"]
    prompt = (
        "【主动开口的时机到了】\n"
        f"{now_label()}\n"
        f"此刻你心里冒出的事：{reason}\n\n"
        "要求：\n"
        "1. 就着这件事自然开口，别解释这是主动消息、别提『由头』；\n"
        f"2. 不超过 {config.MAX_MESSAGE_LENGTH} 字；\n"
        "3. 不要只问『在吗』，直接说事；\n"
        "4. 用你平时的语气和人设，可以带括号动作；\n"
        "5. 别重复你上一句说过的内容。\n"
        "* 说话要贴合现在的时间点；别把几小时前、甚至更早的事说成刚刚发生。"
    )
    if config.ALLOW_BURST:
        prompt += "\n6. 想拆成两条短句发也可以（像真人习惯性分条发）。"

    # v4.1：按当前兴致调语气
    if config.MOOD_ENABLED:
        _mood = float(u.get("mood", 60) or 0)
        if _mood <= config.MOOD_LOW_THRESHOLD:
            prompt += "\n* 你现在没什么力气，话短一点、语气淡一点，别用力卖萌。"
        elif _mood >= config.MOOD_HIGH_THRESHOLD:
            prompt += "\n* 你现在心情不错，可以多说两句、带点小雀跃。"

    # 先闪一下「正在输入」
    await show_typing(chat_key, config.TYPING_SECONDS)

    try:
        await message_service.push_system_message(
            chat_key=chat_key,
            agent_messages=prompt,
            trigger_agent=True,
        )
    except Exception as e:
        core.logger.warning(f"[proactive3] 发送失败: {e}")
        return False

    now = _now()
    u["last_proactive_ts"] = now
    u["last_bot_msg_ts"] = now
    u["bot_msgs_since_user"] = int(u.get("bot_msgs_since_user", 0)) + 1
    u["urge"] = 0.0
    u["pings"] = int(u.get("pings", 0) or 0) + 1
    u["reply_credit_ts"] = u.get("reply_credit_ts", 0)
    u["reflect_at"] = now          # v4：一小时后回看有没有人接
    u["tired"] = min(100.0, float(u.get("tired", 20.0)) + 3.0)  # 主动说话也费神
    ensure_today(u)
    u["sent_today"] = int(u.get("sent_today", 0)) + 1
    if kind == "miss":
        u["miss_today"] = int(u.get("miss_today", 0)) + 1

    log = u.setdefault("log", [])
    log.append({"ts": now, "kind": kind, "reason": reason[:80]})
    u["log"] = log[-30:]

    core.logger.info(f"[proactive3] 开口({kind}) -> {chat_key}: {reason[:40]}")
    return True


# ======================================================================== 群聊心流

GROUP_INTENT = {
    "interject": "群里正在聊，你插一句自然的话（别点名、别抢话题，接得上才说）",
    "icebreak": "群里冷场很久了，抛一个轻松的话头（关于你自己的小事/天气/节日）",
    "topic": "你在群里静了很久，想说点自己的事开个话题（不要问别人问题，先说自己）",
}


async def group_gate(chat_key: str, g: Dict[str, Any], msgs: List[Dict[str, Any]], intent: str) -> bool:
    """群里该不该说话（模型判断）"""
    if not config.LLM_GATE:
        return True
    convo = transcript(msgs, 10)
    prompt = (
        f"你是群聊里的一个普通成员。现在考虑：{GROUP_INTENT.get(intent, intent)}\n\n"
        f"【最近群聊】\n{convo or '（群很安静）'}\n\n"
        f"【距群里最后一条消息】{minutes_since(g.get('last_msg_ts', 0)):.0f} 分钟\n"
        f"【距你自己上次说话】{minutes_since(g.get('last_bot_ts', 0)):.0f} 分钟\n\n"
        "判断：现在开口会不会显得多余/打断别人/硬凑？\n"
        "接得上、自然、不抢话 → ALLOW；没必要或会突兀 → SKIP\n"
        "只回一个词：ALLOW 或 SKIP"
    )
    if not _model_args():
        _log_no_model_once()
        return True

    ans = (await llm_chat(prompt, max_tokens=200, timeout=20.0, temperature=0.3)).upper()
    verdict = _first_verdict(ans)
    if verdict is None:
        # 修复 R22：不依赖 DEBUG；同一群 1 小时内只写一次
        _warn_group_once(chat_key)
        return False
    if verdict == "SKIP":
        if config.DEBUG:
            core.logger.info(f"[proactive3] 群聊闸门否决({intent}) {chat_key}")
        return False
    return True


async def send_group_flow(chat_key: str, g: Dict[str, Any], intent: str, msgs: List[Dict[str, Any]]) -> bool:
    convo = transcript(msgs, 8)
    prompt = (
        "【群聊里的自然发言】\n"
        f"你现在要做的：{GROUP_INTENT.get(intent, intent)}\n\n"
        f"【最近群聊】\n{convo or '（群很安静）'}\n\n"
        "要求：\n"
        f"1. 不超过 {config.MAX_MESSAGE_LENGTH} 字，口语化；\n"
        "2. 针对整个群说，不点名；\n"
        "3. 别像客服、别总结别人说的话；\n"
        "4. 接得上就接，接不上就顺着自己的话头说。"
    )
    try:
        await message_service.push_system_message(
            chat_key=chat_key,
            agent_messages=prompt,
            trigger_agent=True,
        )
    except Exception as e:
        core.logger.warning(f"[proactive3] 群发言失败: {e}")
        return False

    now = _now()
    g["last_bot_ts"] = now
    g["flow_until"] = now + config.GROUP_FLOW_WINDOW_MINUTES * 60
    g["interjects"] = 0
    core.logger.info(f"[proactive3] 群内开口({intent}) -> {chat_key}")
    return True


async def group_flow_tick(st: Dict[str, Any]) -> bool:
    if not config.GROUP_FLOW_ENABLED:
        return False

    now = _now()
    for gk, g in list((st.get("groups") or {}).items()):
        if not group_allowed(gk):
            continue

        if g.get("flow_date") != _today():
            g["flow_date"] = _today()
            g["ice_count"] = 0
            g["topic_count"] = 0

        g["interject_ts"] = [t for t in (g.get("interject_ts") or []) if now - t < 3600]

        # 安静时段不在群里说话
        if in_quiet():
            continue

        msgs = await fetch_recent(gk, 12)
        idle_h = hours_since(g.get("last_msg_ts", 0)) if g.get("last_msg_ts") else 999.0

        # 连插几条都没人接 → 退场安静下来
        fu = float(g.get("flow_until", 0) or 0)
        if fu and fu < now:
            spoke_after = float(g.get("last_msg_ts", 0) or 0) > float(g.get("last_bot_ts", 0) or 0)
            if not spoke_after:
                g["ignored_streak"] = int(g.get("ignored_streak", 0) or 0) + 1
                if int(g["ignored_streak"]) >= config.GROUP_FLOW_IGNORED_EXIT:
                    g["flow_until"] = 0
                    core.logger.info(
                        f"[proactive3] 群里连着 {g['ignored_streak']} 次没人接，退出心流: {gk}"
                    )
            else:
                g["ignored_streak"] = 0
            g["flow_until"] = 0

        # 1) 心流接话
        in_window = float(g.get("flow_until", 0) or 0) > now
        fresh = (now - float(g.get("last_msg_ts", 0) or 0)) < config.GROUP_FLOW_WINDOW_MINUTES * 60
        gap_ok = (now - float(g.get("last_bot_ts", 0) or 0)) >= max(
            config.GROUP_FLOW_MIN_GAP_SECONDS, config.GROUP_FLOW_HOLD_SECONDS
        )
        hourly_ok = len(g["interject_ts"]) < config.GROUP_FLOW_HOURLY_CAP
        if (
            in_window
            and fresh
            and gap_ok
            and hourly_ok
            and int(g.get("interjects", 0) or 0) < config.GROUP_FLOW_MAX_PER_WINDOW
            and msgs
        ):
            if await group_gate(gk, g, msgs, "interject"):
                if await send_group_flow(gk, g, "interject", msgs):
                    g["interject_ts"].append(now)
                    g["interjects"] = int(g.get("interjects", 0) or 0) + 1
                    return True

        # 2) 破冰
        if (
            config.GROUP_ICEBREAK_ENABLED
            and idle_h >= config.GROUP_IDLE_HOURS
            and int(g.get("ice_count", 0) or 0) < config.GROUP_ICEBREAK_DAILY_CAP
        ):
            if await group_gate(gk, g, msgs, "icebreak"):
                if await send_group_flow(gk, g, "icebreak", msgs):
                    g["ice_count"] = int(g.get("ice_count", 0) or 0) + 1
                    return True

        # 3) 起话头
        if (
            config.GROUP_TOPIC_ENABLED
            and hours_since(g.get("last_bot_ts", 0)) >= config.GROUP_TOPIC_GAP_HOURS
            and int(g.get("topic_count", 0) or 0) < config.GROUP_TOPIC_DAILY_CAP
        ):
            if await group_gate(gk, g, msgs, "topic"):
                if await send_group_flow(gk, g, "topic", msgs):
                    g["topic_count"] = int(g.get("topic_count", 0) or 0) + 1
                    return True

    return False


# ======================================================================== 清理


def cleanup_users(st: Dict[str, Any]) -> int:
    if config.USER_RETENTION_DAYS <= 0:
        return 0
    # 修复 C3：目标私聊用归一化后的 key；从没说过话的用户（播种进来的）不清
    keep_key = normalize_chat_key((config.CHAT_KEY or "").strip())
    removed = 0
    for k in list((st.get("users") or {}).keys()):
        if k == keep_key:
            continue
        u = st["users"][k]
        if float(u.get("last_user_msg_ts", 0) or 0) <= 0:
            continue
        if days_since(u.get("last_user_msg_ts", 0)) > config.USER_RETENTION_DAYS:
            st["users"].pop(k, None)
            removed += 1
    return removed


# ======================================================================== 循环

_scheduler_task: Optional[asyncio.Task] = None


async def scheduler_loop() -> None:
    core.logger.info("[proactive3] 调度器启动（念头驱动）")
    while True:
        try:
            # 整体超时保护：任何一步卡死都不应该让调度器停摆
            await asyncio.wait_for(tick(), timeout=240.0)
        except asyncio.CancelledError:
            break
        except asyncio.TimeoutError:
            core.logger.warning("[proactive3] tick 超时（240s），跳过本轮")
        except Exception as e:
            core.logger.warning(f"[proactive3] tick 异常: {e}")

        # 安静时段刚结束 → 立刻补一次评估（不等下一个心跳）
        try:
            if config.QUIET_END_WAKE:
                st = load_state()
                now_dt = datetime.now()
                if (
                    st.get("quiet_wake_date") != _today()
                    and in_range(now_dt, config.QUIET_END, _hm_plus(config.QUIET_END, 10))
                ):
                    st["quiet_wake_date"] = _today()
                    save_state(st)
                    core.logger.info("[proactive3] 安静时段结束，补一次评估")
                    await tick()
        except Exception as e:
            core.logger.warning(f"[proactive3] 静音结束调度异常: {e}")

        lo = max(1, int(config.HEARTBEAT_MIN_MINUTES))
        hi = max(lo, int(config.HEARTBEAT_MAX_MINUTES))
        _base_gap = float(random.randint(lo, hi) * 60)
        # 修复 B6：统一走 heartbeat_wait（安静时段截短到 QUIET_END + 60 秒）
        _wait = heartbeat_wait(datetime.now(), _base_gap)
        if _wait < _base_gap:
            core.logger.info(f"[proactive3] 安静时段，心跳截短到 {_wait / 60:.0f} 分钟")
        await asyncio.sleep(_wait)


async def tick() -> None:
    if not config.ENABLE:
        return

    core.logger.info("[proactive3] tick 开始")

    # 修复 R21-3：tick 起点必须在任何 await / load_state 之前
    _tick_start = _now()
    st = load_state()

    # 群聊心流（独立于私聊念头）
    try:
        if await group_flow_tick(st):
            flush_groups(st)
    except Exception as e:
        core.logger.warning(f"[proactive3] 群心流异常: {e}")

    # 闲置清理
    try:
        n = cleanup_users(st)
        if n:
            core.logger.info(f"[proactive3] 清理了 {n} 个闲置会话")
    except Exception:
        pass

    chat_key = normalize_chat_key(config.CHAT_KEY) or st.get("last_chat_key", "")
    if not chat_key or "-private_" not in chat_key:
        _warn_bad_chat_key(config.CHAT_KEY)
        save_state(st)
        return
    st["last_chat_key"] = chat_key

    u = get_user(st, chat_key)
    uid = chat_key.split("_")[-1]
    special = is_special_care(u)

    # 修复 R21-4：固定扫过期待办（这里没有 await；早退路径在上面已经返回，扫不到也没关系）
    try:
        sweep_expired(u)
    except Exception as e:
        core.logger.warning(f"[proactive3] 过期清理异常: {e}")

    # 修复 A4：记下疲惫/兴致的基准值，方便按增量合并
    _base = {"tired": float(u.get("tired", 20.0)), "mood": float(u.get("mood", 60.0))}

    def _commit_tick() -> None:
        commit(
            u,
            deltas={
                "tired": float(u.get("tired", 20.0)) - _base["tired"],
                "mood": float(u.get("mood", 60.0)) - _base["mood"],
            },
            tick_start_ts=_tick_start,
        )
        # 修复 R21-2：提交后重置基准，避免同一 tick 多次 commit 重复计增量
        _base["tired"] = float(u.get("tired", 20.0))
        _base["mood"] = float(u.get("mood", 60.0))

    # ---- 念头累积 ----
    now = _now()
    last_tick = float(u.get("last_tick_ts", 0)) or now
    u["last_tick_ts"] = now
    minutes = max(1.0, (now - last_tick) / 60.0)
    scale = urge_scale(u)
    gain = (minutes / max(1.0, config.URGE_REFILL_HOURS * 60.0)) * 100.0 * scale
    u["urge"] = min(100.0, float(u.get("urge", 0.0)) + gain)

    # ---- v4：疲惫/兴致漂移 + 事后反省 ----
    try:
        drift_mood(u, minutes)
        run_reflection(u)
    except Exception as e:
        core.logger.warning(f"[proactive3] 动机层异常: {e}")

    # ---- 被无视停发（必须真的有过对话，否则新用户会被误判）----
    last_user = float(u.get("last_user_msg_ts", 0) or 0)
    if (
        config.SILENT_GIVEAWAY_DAYS > 0
        and last_user > 0
        and int(u.get("bot_msgs_since_user", 0)) >= 1
        and days_since(last_user) > config.SILENT_GIVEAWAY_DAYS
        and now >= float(u.get("silent_until", 0))
    ):
        u["silent_until"] = now + 30 * 86400
        core.logger.info(
            f"[proactive3] 对方 {days_since(last_user):.0f} 天没回，进入停发期"
        )

    # ---- 收场 ----
    if (
        config.CLOSER_ENABLED
        and int(u.get("bot_msgs_since_user", 0)) >= 1
        and hours_since(u.get("last_bot_msg_ts", 0)) >= config.CLOSER_AFTER_HOURS
        and int(u.get("closer_count", 0)) < int(getattr(config, "CLOSER_MAX_COUNT", 3))
        and hours_since(u.get("closer_try_ts", 0)) >= 6.0
    ):
        u["closer_try_ts"] = now
        ok, _ = can_send(u, special)
        if ok and await gate(u, "自己上一条没人接，想自言自语收个尾", []):
            if await send_with_reason(u, "自己上一条没人接，想自言自语收个尾（别追问、别提没人回）", "closer"):
                u["closer_count"] = int(u.get("closer_count", 0)) + 1
                _commit_tick()
                return

    # ---- 念头未满 ----
    if float(u.get("urge", 0)) < 100.0:
        if config.DEBUG:
            core.logger.info(f"[proactive3] 念头 {float(u.get('urge', 0)):.0f}/100")
        _commit_tick()
        return

    # ---- 硬闸 ----
    ok, why = can_send(u, special)
    if config.DEBUG:
        core.logger.info(f"[proactive3] 诊断·硬闸 ok={ok} why={why or '-'} urge={float(u.get('urge', 0)):.0f}")
    if not ok:
        if config.DEBUG:
            core.logger.info(f"[proactive3] 攒满但被拦: {why}")
        _commit_tick()
        return

    # ---- 由头 ----
    msgs = await fetch_recent(chat_key, 16)
    if config.DEBUG:
        core.logger.info(f"[proactive3] 诊断·历史 {len(msgs)} 条")
    got = await derive_reason(u, msgs)

    if config.DEBUG:
        core.logger.info(f"[proactive3] 诊断·由头 {got[1] if got else 'None'}")
    if not got:
        if config.DEBUG:
            core.logger.info("[proactive3] 派不出由头，念头留着")
        _commit_tick()
        return
    reason, kind, pending_ref = got

    # ---- v4：抑制层（累了 / 对方在忙 / 刚反省过 → 忍住，不问模型）----
    sup = check_suppression(u, kind)
    if sup:
        _commit_tick()
        return

    # ---- 闸门 ----
    if not await gate(u, reason, msgs):
        _commit_tick()
        return

    # ---- 开口（成功后才消耗这条由头）----
    if await send_with_reason(u, reason, kind):
        if kind == "greeting" and isinstance(pending_ref, dict) and pending_ref.get("win"):
            # 修复 A2：问候发送成功后才记"今天这个窗口已经问候过"
            u.setdefault("greeted", {})[str(pending_ref["win"])] = _today()
            pending_ref = None
        if pending_ref is not None:
            if kind == "cue":
                pending_ref["used"] = True
            else:
                pending_ref["done"] = True
                pending_ref["done_ts"] = _now()
        if kind in ("probe", "followup", "continue"):
            u["last_followup_ts"] = _now()
        if config.CONTINUE_ENABLED:
            already = any(
                (not p.get("done")) and p.get("kind") == "continue"
                for p in u.get("pending", [])
            )
            if not already:
                u.setdefault("pending", []).append({
                    "text": "话说完了人走了，过一阵补一句（问对方干啥去了，或随口说一句）",
                    "due_ts": _now() + config.CONTINUE_AFTER_MINUTES * 60,
                    "kind": "continue",
                    "done": False,
                })
    _commit_tick()


# ======================================================================== 生命周期


@plugin.mount_init_method()
async def on_init():
    global _scheduler_task
    if not config.ENABLE:
        core.logger.info("[proactive3] 已禁用，不启动")
        return

    # 启动自检：确认三件事都能用（不调模型、不发消息）
    chat_key = normalize_chat_key(config.CHAT_KEY)
    if chat_key and "-private_" in chat_key:
        try:
            msgs = await fetch_recent(chat_key, 10)
            core.logger.info(f"[proactive3] 自检·历史读取: {len(msgs)} 条")
        except Exception as e:
            core.logger.warning(f"[proactive3] 自检·历史读取失败: {e}")
    else:
        core.logger.warning("[proactive3] 自检·CHAT_KEY 未配置或不是私聊")

    # 从历史补全状态
    if chat_key and "-private_" in chat_key:
        try:
            _st = load_state()
            _u = get_user(_st, chat_key)
            _n = await sync_from_history(_u)
            if _n:
                commit(_u)
                core.logger.info(f"[proactive3] 自检·历史补全: {_n} 条记录")
        except Exception as e:
            core.logger.warning(f"[proactive3] 自检·历史补全失败: {e}")

    # 播种名单：没聊过的人也能写进来
    if config.SEED_USERS:
        try:
            _st = load_state()
            added = 0
            for raw in config.SEED_USERS:
                key = str(raw).strip()
                if not key:
                    continue
                if "-private_" not in key:
                    if not key.isdigit():
                        continue
                    key = f"onebot_v11-private_{key}"
                if key not in (_st.get("users") or {}):
                    get_user(_st, key)
                    added += 1
            if added:
                save_state(_st)
                core.logger.info(f"[proactive3] 自检·播种 {added} 个用户")
        except Exception as e:
            core.logger.warning(f"[proactive3] 自检·播种失败: {e}")

    args = _model_args()
    if args:
        core.logger.info(
            f"[proactive3] 自检·模型: {args.get('model')}（分组 {args.get('group')}）"
        )
    else:
        core.logger.warning("[proactive3] 自检·模型不可用（闸门/回访会降级）")

    core.logger.info(
        f"[proactive3] 自检·配置: 安静 {config.QUIET_START}-{config.QUIET_END} "
        f"念头 {config.URGE_REFILL_HOURS}h 闸门 {'开' if config.LLM_GATE else '关'}"
    )

    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop())
    core.logger.info("[proactive3] 初始化完成（v4 念头驱动 · 抑制 · 记忆 · 反省）")


@plugin.mount_cleanup_method()
async def on_cleanup():
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
    core.logger.info("[proactive3] 调度器已停止")


# ======================================================================== 消息钩子


@plugin.mount_on_user_message()
async def on_user_message(ctx: AgentCtx, message: ChatMessage):
    """只关心私聊：更新状态、解除停发、安排追问"""
    try:
        chat_key = ctx.chat_key or ""
    except Exception:
        return None

    # ---- 群聊：只用于心流，不做"人"处理 ----
    if "-group_" in chat_key:
        if config.GROUP_FLOW_ENABLED and group_allowed(chat_key):
            try:
                _st = load_state()
                g = get_group(_st, chat_key)
                now = _now()
                g["last_msg_ts"] = now
                flush_groups(_st)
            except Exception as e:
                core.logger.warning(f"[proactive3] 群状态更新失败: {e}")
        return None

    # 本插件只关心私聊
    if "-private_" not in chat_key:
        return None

    st = load_state()
    u = get_user(st, chat_key)

    text = ""
    try:
        text = (getattr(message, "content_text", "") or "").strip()
    except Exception:
        text = ""

    # 修复 B8：命令消息不算"对方说话"，不做任何状态更新
    if text.startswith("/"):
        return None

    now = _now()

    # 修复 A7：prev_bot 用"本条之前她最近一条发言"，
    # 而不是 last_bot_msg_ts（那个只记录主动消息的时间）。
    prev_bot = 0.0
    try:
        _recent = await fetch_recent(chat_key, 8)
        for _m in reversed(_recent):
            if _m.get("is_bot") and float(_m.get("ts", 0) or 0) < now - 1:
                prev_bot = float(_m["ts"])
                break
    except Exception as e:
        core.logger.warning(f"[proactive3] 取她最近发言失败（按无处理）: {e}")
        prev_bot = 0.0

    silence_min = (now - prev_bot) / 60.0 if prev_bot else 0.0

    u["last_user_msg_ts"] = now
    u["bot_msgs_since_user"] = 0
    u["silent_until"] = 0
    u["closer_count"] = 0
    u["no_reply_count"] = 0
    u["msg_count"] = int(u.get("msg_count", 0)) + 1
    st["last_chat_key"] = chat_key

    # 关系分档
    u["tier"] = update_tier(u)

    # 话题记忆（v4：带权重）
    # 修复 C6：topics 是死数据（没有任何地方读它），但会明文存原文，
    # 所以写入前先过一遍敏感词过滤，和记忆走同一套规则。
    if text and len(text) > 2 and memory_allowed(text):
        topics = u.setdefault("topics", [])
        topics.append({"text": text[:80], "ts": now})
        u["topics"] = topics[-30:]
        add_memory(u, text, "topic")

    # v4：对方说忙/累 or 状态好 → 影响疲惫与兴致
    if text:
        note_busy(u, text)

    # ---- 时间锚点：对方提过"明天/今晚/周末"之类 ----
    if text:
        for cue in extract_cues(text, now):
            cues = u.setdefault("cues", [])
            if any(abs(float(c.get("due_ts", 0)) - cue["due_ts"]) < 3600 for c in cues):
                continue  # 同一时间点只记一次
            cues.append(cue)
        u["cues"] = u.get("cues", [])[-6:]

    # ---- 回复率：她上次开口后对方在窗口内回了 ----
    if config.ADAPTIVE_REPLY_RATE:
        lp = float(u.get("last_proactive_ts", 0) or 0)
        if lp > 0 and (now - lp) <= config.REPLY_WINDOW_MINUTES * 60:
            if float(u.get("reply_credit_ts", 0) or 0) < lp:
                u["replies"] = int(u.get("replies", 0) or 0) + 1
                u["reply_credit_ts"] = lp
        pings = int(u.get("pings", 0) or 0)
        reps = int(u.get("replies", 0) or 0)
        if pings >= 3:
            u["reply_rate"] = round(min(1.0, reps / pings), 3)

    # ---- 生病关怀：对方说不舒服 ----
    if config.CARE_ENABLED and text:
        sym = detect_care(text)
        if sym:
            queue_pending(u, {
                "text": f"对方刚才说「{sym}」不舒服，过一阵问问好点没、有没有吃药",
                "due_ts": now + config.CARE_AFTER_MINUTES * 60,
                "kind": "care",
                "done": False,
            })
            core.logger.info(f"[proactive3] 记下生病关怀（{sym}）")

    # ---- 敷衍 → 过一会儿追问那件事（A7：要求她最近一条发言在 120 分钟内）----
    if (
        config.FOLLOWUP_ENABLED
        and prev_bot
        and (now - prev_bot) <= 120 * 60
        and is_perfunctory(text)
    ):
        queue_pending(u, {
            "text": "对方刚才回得很敷衍，过一会儿把那件事再问一句",
            "due_ts": now + config.PROBE_AFTER_MINUTES * 60,
            "kind": "probe",
            "done": False,
        })

    # ---- 她说完很久对方才回来 → 接上话头 ----
    if (
        config.FOLLOWUP_ENABLED
        and prev_bot
        and config.FOLLOWUP_AFTER_MINUTES < silence_min <= config.FOLLOWUP_MAX_MINUTES
        and not is_perfunctory(text)
    ):
        queue_pending(u, {
            "text": "对方隔了很久才回来，接着刚才的话头问一句",
            "due_ts": now + config.PROBE_AFTER_MINUTES * 60,
            "kind": "followup",
            "done": False,
        })

    # 清理
    keep = []
    for p in u.get("pending", []):
        if p.get("done"):
            continue
        # 对方已经回话了 →「她说完人走了」的接话就没意义了，撤掉
        if p.get("kind") == "continue":
            continue
        if (now - float(p.get("due_ts", 0))) > config.FOLLOWUP_MAX_MINUTES * 60 * 2:
            continue
        keep.append(p)
    u["pending"] = keep[-10:]

    # 钩子是 memories 的唯一生产者：让它对自己的列表有权威，
    # 否则 commit() 会用磁盘旧列表覆盖掉刚 add_memory 进去的那条
    commit(u, force={"memories"})
    return None


# ======================================================================== 命令


@plugin.mount_command(
    name="proactive",
    description="主动消息 v4：status / dry(预览) / test(真发) / log / on / off / reset",
    permission=CommandPermission.SUPER_USER,
    category="主动消息",
)
async def proactive_cmd(
    context: CommandExecutionContext,
    sub: str = Arg("子命令", positional=True, default="status"),
) -> CommandResponse:
    sub = (sub or "status").lower()

    st = load_state()
    chat_key = normalize_chat_key(config.CHAT_KEY) or st.get("last_chat_key", "")
    u = get_user(st, chat_key) if chat_key else None
    # 修复 R24-2：status 也要先跨天清零，否则会显示昨天的"今日已发"
    if u:
        ensure_today(u)

    if sub in ("on", "off"):
        config.ENABLE = (sub == "on")
        # 修复 C4：on 时如果调度器没在跑就拉起来；off 等下一轮自然停
        if sub == "on":
            global _scheduler_task
            if _scheduler_task is None or _scheduler_task.done():
                _scheduler_task = asyncio.create_task(scheduler_loop())
                return CmdCtl.success("已开启主动消息，调度器已启动")
            return CmdCtl.success("已开启主动消息（调度器本来就在跑）")
        return CmdCtl.success("已关闭主动消息（当前这轮结束后不再发，重启后也不会启动）")

    if sub == "reset":
        if not u:
            return CmdCtl.failed("没有目标私聊（请先配置 CHAT_KEY 或先私聊一次）")
        u.update({
            "urge": 0.0,
            "sent_today": 0,
            "sent_date": _today(),
            "pending": [],
            "silent_until": 0,
            "gate_skip_until": 0,
            "closer_count": 0,
            "no_reply_count": 0,
            "miss_today": 0,
            "reflect_count": 0,
            "suppress_count": {},
            "restraint_until": 0,
        })
        # A4：reset 要能真正清掉，不能被磁盘上的旧值合并顶回来
        commit(u, force={"pending", "cues", "sent_today", "sent_date", "closer_count",
                         "no_reply_count", "urge", "silent_until", "gate_skip_until",
                         "miss_today", "reflect_count", "suppress_count", "restraint_until"})
        return CmdCtl.success("已重置念头、冷却与停发状态")

    if sub == "log":
        if not u or not u.get("log"):
            return CmdCtl.success("还没有发过主动消息")
        rows = u["log"][-8:]
        lines = [
            f"{datetime.fromtimestamp(float(r['ts'])).strftime('%m-%d %H:%M')} [{r.get('kind')}] {r.get('reason', '')}"
            for r in rows
        ]
        return CmdCtl.success("最近主动消息：\n" + "\n".join(lines))

    if sub in ("dry", "preview"):
        if not u:
            return CmdCtl.failed("没有目标私聊（请先配置 CHAT_KEY）")
        msgs = await fetch_recent(chat_key, 16)
        got = await derive_reason(u, msgs, dry=True)
        if not got:
            return CmdCtl.success("此刻派不出由头（这轮不会说话）")
        reason, kind, _ref = got
        # 预览不消耗由头、不留下闸门冷却
        _saved_skip = float(u.get("gate_skip_until", 0) or 0)
        g = await gate(u, reason, msgs)
        u["gate_skip_until"] = _saved_skip
        commit(u)
        verdict = "模型会放行" if g else "模型会否决"
        return CmdCtl.success(
            f"【预览·不发送】\n"
            f"由头类型：{kind}\n"
            f"由头内容：{reason}\n"
            f"闸门结论：{verdict}\n"
            f"念头：{float(u.get('urge', 0)):.0f}/100"
        )

    if sub == "test":
        if not u:
            return CmdCtl.failed("没有目标私聊（请先配置 CHAT_KEY）")
        msgs = await fetch_recent(chat_key, 16)
        got = await derive_reason(u, msgs)
        if got:
            reason, kind, _ref = got
        else:
            reason, kind = "想你了，随便说句话", "test"
        ok = await send_with_reason(u, reason, kind)
        commit(u)
        return CmdCtl.success(f"已开口({kind}): {reason[:50]}") if ok else CmdCtl.failed("发送失败，看日志")

    # status
    if not u:
        return CmdCtl.success("主动消息 v4：还没有记录，等第一条私聊")
    now = _now()
    pending_n = len([p for p in u.get("pending", []) if not p.get("done")])
    restraint = now < float(u.get("restraint_until", 0) or 0)
    # 修复 R22：闸门有三种态 —— 关 / 开+有模型 / 开但无可用模型
    _margs = _model_args()
    if not config.LLM_GATE:
        gate_line = "关"
    elif not _margs:
        gate_line = "开·无可用模型（按关闭处理）"
    else:
        gate_line = f"开（分组 {_margs.get('group')} · {_margs.get('model')}）"
    lines = [
        "主动消息 v4（念头驱动 · 抑制 · 记忆 · 反省）",
        f"开关：{'开' if config.ENABLE else '关'}   "
        f"闸门：{gate_line}   "
        f"抑制：{'开' if config.SUPPRESS_ENABLED else '关'}",
        f"当前时间：{datetime.now().strftime('%H:%M')}（服务器本地）",
        f"目标：{chat_key}"
        + ("" if "-private_" in chat_key else "   ⚠️ 格式不对，应为 onebot_v11-private_QQ号"),
        f"念头：{float(u.get('urge', 0)):.0f}/100（攒满约 {config.URGE_REFILL_HOURS}h）",
        f"今日已发：{u.get('sent_today', 0)}/{config.MAX_SENDS_PER_DAY}",
        f"疲惫：{float(u.get('tired', 0) or 0):.0f}/100   "
        f"兴致：{float(u.get('mood', 0) or 0):.0f}/100",
        f"记忆：{len(u.get('memories', []) or [])} 条   未完话题：{pending_n} 条",
        f"距对方上次说话：{minutes_since(u.get('last_user_msg_ts', 0)):.0f} 分钟",
        f"距上次主动：{minutes_since(u.get('last_proactive_ts', 0)):.0f} 分钟",
        f"停发中：{'是' if now < float(u.get('silent_until', 0)) else '否'}   "
        f"收敛期：{'是' if restraint else '否'}",
        f"安静时段：{config.QUIET_START}-{config.QUIET_END}{'（当前在里面）' if in_quiet() else ''}",
    ]
    return CmdCtl.success("\n".join(lines))
