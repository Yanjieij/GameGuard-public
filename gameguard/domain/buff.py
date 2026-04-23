"""Buff / Debuff 数据模型。

技能系统里 buff 叠加是第二大 bug 类（我们的植入 bug 分类参考了 NetEase
Wuji）。真实游戏（WoW、原神、崩铁）一般区分几种叠加规则：

- replace：新的一次施加覆盖掉旧的，magnitude 和 duration 都换成新值
- refresh：magnitude 保持不变，duration 重置到最大
- add：magnitude 累加到 max_stacks 上限，duration 重置
- independent：各自独立存在（例如不同施法者挂的同名 buff）

这几种规则是设计和实现最容易错位的地方。所以这里把规则显式写进模型，
而不是把某种行为写死在代码里。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class StackRule(str, Enum):
    REPLACE = "replace"
    REFRESH = "refresh"
    ADD = "add"
    INDEPENDENT = "independent"


class BuffSpec(BaseModel):
    """Buff 的静态定义，来自策划文档。"""

    id: str
    name: str
    magnitude: float
    duration: float
    stack_rule: StackRule = StackRule.REFRESH
    max_stacks: int = 1
    is_debuff: bool = False
    # 是否是持续伤害型 buff：True 时 sandbox 在每个 tick 调用 handler 的
    # on_dot_tick 结算伤害。magnitude 在 DoT 语义下表示"每秒伤害"，单 tick
    # 伤害 = magnitude * tick_dt。设计文档 §4.2 给出了精确公式。
    is_dot: bool = False


class BuffInstance(BaseModel):
    """挂在某个角色身上的 buff 实例（运行时）。"""

    spec_id: str
    magnitude: float
    remaining: float
    stacks: int = 1
    source_id: str | None = None
    applied_at: float = 0.0

    def tick(self, dt: float) -> bool:
        """推进时间，返回 buff 是否还活着。"""
        self.remaining -= dt
        return self.remaining > 0.0


class BuffBook(BaseModel):
    """按 id 索引 BuffSpec 的注册表。"""

    specs: dict[str, BuffSpec] = Field(default_factory=dict)

    def register(self, spec: BuffSpec) -> None:
        self.specs[spec.id] = spec

    def get(self, spec_id: str) -> BuffSpec:
        return self.specs[spec_id]
