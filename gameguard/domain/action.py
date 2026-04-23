"""能提交给沙箱的 Action 集合。

Action 的表面保持小而纯数据，一是让 LLM tool schema 紧凑，二是让 action 日志
可以直接 replay。

Action 家族按 sandbox 分两组：
  - pysim 用：WaitAction / CastAction / InterruptAction / NoopAction
  - questsim 用：MoveToAction / InteractAction / DialogueAction /
                 SaveAction / LoadAction（D13-D15 陆续启用）

Wait 和 Noop 跨 sandbox 共用。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from gameguard.domain.geom import Vec3


class WaitAction(BaseModel):
    kind: Literal["wait"] = "wait"
    seconds: float


class CastAction(BaseModel):
    kind: Literal["cast"] = "cast"
    actor: str
    skill: str
    target: str


class InterruptAction(BaseModel):
    """Cancel the actor's in-progress cast (if any)."""

    kind: Literal["interrupt"] = "interrupt"
    actor: str


class NoopAction(BaseModel):
    kind: Literal["noop"] = "noop"


# --------------------------------------------------------------------------- #
# QuestSim 的 Action 家族（D13+）
# --------------------------------------------------------------------------- #


class MoveToAction(BaseModel):
    """让角色从当前位置移动到目标世界坐标。

    - mode="walk"：走 A* 路径，逐 tick 沿 waypoint 推进（受 NavGrid 阻挡）
    - mode="teleport"：瞬移到目标（测试用，不走寻路）

    为什么 mode 用字符串而非 enum？—— YAML 里写字符串对 LLM 最友好，
    Pydantic Literal 也能强校验值域。
    """

    kind: Literal["move_to"] = "move_to"
    actor: str
    pos: Vec3
    mode: Literal["walk", "teleport"] = "walk"


class InteractAction(BaseModel):
    """让角色与某个实体交互（打开箱子、拾取物品、与 NPC 对话入口等）。

    交互的"后果"由 target entity 的 state 和 quest runtime 决定——
    比如 interact NPC 可能开启对话节点，interact 箱子可能触发 quest step。
    """

    kind: Literal["interact"] = "interact"
    actor: str
    entity_id: str


class DialogueAction(BaseModel):
    """推进一段对话树，给出选项路径。

    choice_path 是 int 列表，表示在每个对话节点选第几个选项。
    replay 确定性要求这个列表是有限 int 序列，不含任何浮点 / 文本。
    """

    kind: Literal["dialogue"] = "dialogue"
    actor: str
    npc_id: str
    choice_path: list[int] = Field(default_factory=list)


class SaveAction(BaseModel):
    """保存当前 quest/entity state 到槽位。测存档读档 bug 的关键 action。"""

    kind: Literal["save"] = "save"
    slot: str = "auto"


class LoadAction(BaseModel):
    """从槽位恢复 state。"""

    kind: Literal["load"] = "load"
    slot: str = "auto"


Action = (
    WaitAction
    | CastAction
    | InterruptAction
    | NoopAction
    | MoveToAction
    | InteractAction
    | DialogueAction
    | SaveAction
    | LoadAction
)


class ActionOutcome(BaseModel):
    """Result of submitting one Action to the sandbox."""

    accepted: bool
    reason: str | None = None
    events: list[str] = Field(default_factory=list)  # short, human-readable summaries
