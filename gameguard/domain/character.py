"""角色 / 实体数据模型。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .buff import BuffInstance


class CharacterState(str, Enum):
    IDLE = "idle"
    CASTING = "casting"
    STUNNED = "stunned"
    DEAD = "dead"


class Character(BaseModel):
    """够技能系统 QA 用的最小角色模型。

    这里不模拟位置和物理——这个沙箱只关注技能 / 冷却 / buff 这一层的
    逻辑 bug。做更完整的沙箱可以加 transform 和碰撞，但那些字段很少
    出同款 regression，这里不浪费。
    """

    id: str
    name: str
    hp: float
    hp_max: float
    mp: float
    mp_max: float
    state: CharacterState = CharacterState.IDLE
    cooldowns: dict[str, float] = Field(default_factory=dict)  # skill_id -> 剩余秒数
    buffs: list[BuffInstance] = Field(default_factory=list)
    # 状态是 CASTING 时，下面两个字段才有值：
    casting_skill: str | None = None
    cast_remaining: float = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0.0 and self.state != CharacterState.DEAD

    def cooldown_remaining(self, skill_id: str) -> float:
        return max(0.0, self.cooldowns.get(skill_id, 0.0))

    def skill_ready(self, skill_id: str) -> bool:
        return self.cooldown_remaining(skill_id) <= 0.0
