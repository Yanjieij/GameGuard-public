"""技能定义数据结构。

SkillSpec 连接两端：一端是策划写的技能文档（Markdown 表格 + 伤害公式），
另一端是沙箱里跑的仿真代码。在真实 Unity 工程里这通常是 ScriptableObject，
这里换成 Pydantic 模型——DesignDocAgent 读完文档能直接 emit 成对象。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DamageType(str, Enum):
    PHYSICAL = "physical"
    FIRE = "fire"
    FROST = "frost"
    ARCANE = "arcane"
    TRUE = "true"


class SkillSpec(BaseModel):
    id: str
    name: str
    mp_cost: float
    cast_time: float          # 施法时间（秒），0 表示瞬发
    cooldown: float           # 冷却时间（秒）
    damage_base: float
    damage_type: DamageType = DamageType.PHYSICAL
    # 伤害公式用字符串存（例如 "base * (1 + caster_power)"），不直接存 Python 代码。
    # 这样 DesignDocAgent 从文档生成公式时，不会触发 exec 任意代码的安全风险。
    damage_formula: str = "base"
    # 成功施法后挂在施法者身上的 buff id 列表。
    self_buffs: list[str] = Field(default_factory=list)
    # 命中后挂在目标身上的 buff id 列表。
    target_buffs: list[str] = Field(default_factory=list)
    interruptible: bool = True
    # 伤害结算时机，用占 cast_time 的比例表示。沙箱不模拟动画帧，只需要知道
    # 策划约定了"伤害在施法 50% 时结算"之类的契约，QA 就能据此写不变式
    # （例如 "damage fires at 0.5 * cast_time"）。
    damage_event_at: float = 0.5


class SkillBook(BaseModel):
    specs: dict[str, SkillSpec] = Field(default_factory=dict)

    def register(self, spec: SkillSpec) -> None:
        self.specs[spec.id] = spec

    def get(self, skill_id: str) -> SkillSpec:
        return self.specs[skill_id]

    def all_ids(self) -> list[str]:
        return list(self.specs.keys())
