"""Entity · 场景中一切"可查询物件"的通用表示。

本模块的职责

QuestSim 场景里有这些东西需要被抽象：
  - 玩家（player）：可以移动、可以 interact
  - NPC（npc）：有对话树、可被 interact、有 state 字典
  - 拾取物（item）：靠近自动或手动拾取
  - 场景道具（prop）：箱子、门、压力板等（可能参与物理）
  - 触发器实体（trigger_entity）：看不见的逻辑触发体（与 TriggerVolume 分开——
    TriggerVolume 在 scene.py，由空间判定；这里的 trigger_entity 是某个
    实体本身作为触发源，比如"击败 BOSS → 某 quest 进下一步"）

如果每种物件一个 class，会很乱。我们用一个 Entity + EntityKind 枚举
统一表达，state 字典承载类型特定数据。这和 Unity 的 GameObject + Component
理念一致（我们没有 Component 层只是因为简化）。

与 Character（已有）的关系

`gameguard/domain/character.py` 的 `Character` 是技能系统专用的（有 hp/mp/
buffs/cooldowns/casting_skill）。QuestSim 里的玩家/NPC 用的是这里的 Entity
（有 pos/bbox/kind/state_dict），两套模型不冲突：

- skill system 只认 Character（不需要 3D 位置）
- quest system 只认 Entity（不需要技能槽）
- 将来要做"带技能的 Boss"就在 Entity.state 里塞 Character 引用

保持两个模型分离让 pysim 的既有测试零影响（零破坏性变更）。
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from gameguard.domain.geom import BoundingBox, Vec3

# --- EntityKind · 实体类型枚举 ---
class EntityKind(str, Enum):
    """场景中一切 Entity 的类型枚举。

    str enum 让 YAML / JSON 序列化为字符串（"player" / "npc" ...）而非
    int 枚举值，可读性大幅提升，也对 LLM 友好（Prompt 里能直接看到语义）。
    """

    PLAYER = "player"
    NPC = "npc"
    ITEM = "item"                  # 拾取物
    PROP = "prop"                  # 场景道具（箱子、门等）
    TRIGGER_ENTITY = "trigger_entity"  # 逻辑触发源（对应某个 quest step）

# --- Entity · 通用场景实体 ---
class Entity(BaseModel):
    """场景中任意物件的统一表示。

    字段设计原则：
      - pos / bbox：空间字段必填（QuestSim 是 3D 系统）
      - state：任意 dict，承载类型特定数据（对话进度、HP、开关状态等）
      - tags：字符串标签，便于分组查询（"enemy"、"quest:harbor"）
      - initial_pos / initial_state：reset 时回滚到这些值（Q-BUG-003 的
        oracle 就是 reset 后比较 pos 和 state 是否恢复）
    """

    id: str = Field(..., description="全局唯一 id，例 'npc_captain' / 'prop_crate_1'")
    kind: EntityKind = Field(..., description="实体类型")
    name: str = Field("", description="可选显示名，进 bug 报告更易读")
    pos: Vec3 = Field(..., description="当前位置（世界坐标）")
    bbox: BoundingBox = Field(..., description="碰撞包围盒（相对于 pos 平移）")
    state: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "类型特定的运行期状态。例："
            "NPC 用 {dialogue_step: int, hp: int}；"
            "Prop 用 {opened: bool}；玩家一般空"
        ),
    )
    tags: list[str] = Field(default_factory=list, description="便于批量查询的字符串标签")

    # ---- reset 专用：记录初始值 ----
    initial_pos: Vec3 | None = Field(None, description="reset 时要恢复的位置")
    initial_state: dict[str, Any] = Field(
        default_factory=dict, description="reset 时要恢复的 state dict（深拷贝）"
    )

    # ---- 交互属性 ----
    interact_range: float = Field(
        2.0, description="玩家必须在该距离内才能 interact；单位与 pos 一致"
    )

    # ---- 物理 ----
    physics_mass: float = Field(
        0.0,
        description=(
            "0 = 静态物体（不受力）；>0 = 动态物体（pybullet 会模拟受力）。"
            "Q-BUG: 推箱子关卡的箱子 mass=5.0"
        ),
    )

    # ---- 便捷方法 ----
    def world_bbox(self) -> BoundingBox:
        """返回世界坐标下的 bbox（将本地 bbox 平移到 pos）。

        约定：``bbox`` 字段存"以原点为中心的局部 AABB"；需要世界 AABB 时
        通过本方法平移。这让 Entity 移动时不用每帧更新 bbox。
        """
        return BoundingBox(min=self.bbox.min + self.pos, max=self.bbox.max + self.pos)

    def snapshot_initial(self) -> None:
        """在 Scene 加载后、sandbox reset 前调用一次，记录初始值。

        Q-BUG-003 的 oracle 依赖这个：reset 应把 pos/state 恢复到 initial_*；
        v2 漏恢复就被抓。
        """
        if self.initial_pos is None:
            self.initial_pos = self.pos.model_copy(deep=True)
        if not self.initial_state:
            # 深拷贝避免后续修改 state 污染 initial
            import copy
            self.initial_state = copy.deepcopy(self.state)

    def reset_to_initial(self) -> None:
        """把 Entity 恢复到 initial_pos / initial_state（完整的 v1 行为）。

        v2 的 Q-BUG-003 会漏掉 state 那一部分。
        """
        import copy
        if self.initial_pos is not None:
            self.pos = self.initial_pos.model_copy(deep=True)
        self.state = copy.deepcopy(self.initial_state)

    def __repr__(self) -> str:
        return f"Entity({self.kind.value}:{self.id} @ {self.pos})"

# --- EntityRegistry · 场景内实体集合的轻量容器 ---
class EntityRegistry(BaseModel):
    """场景中所有 Entity 的集合 + 按 id / kind / tag 的快速查询。

    Pydantic 模型让它也能参与 snapshot/restore pickle 流程。
    """

    entities: dict[str, Entity] = Field(default_factory=dict)

    # ---- 注册 ----
    def add(self, entity: Entity) -> None:
        if entity.id in self.entities:
            raise ValueError(f"Entity id 冲突: {entity.id!r}")
        self.entities[entity.id] = entity

    def add_many(self, entities: list[Entity]) -> None:
        for e in entities:
            self.add(e)

    # ---- 查询 ----
    def get(self, entity_id: str) -> Entity:
        if entity_id not in self.entities:
            raise KeyError(f"未知 entity_id: {entity_id!r}")
        return self.entities[entity_id]

    def get_optional(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def find_by_kind(self, kind: EntityKind) -> list[Entity]:
        return [e for e in self.entities.values() if e.kind == kind]

    def find_by_tag(self, tag: str) -> list[Entity]:
        return [e for e in self.entities.values() if tag in e.tags]

    def all(self) -> list[Entity]:
        return list(self.entities.values())

    def __len__(self) -> int:
        return len(self.entities)

    def __contains__(self, entity_id: str) -> bool:
        return entity_id in self.entities

    # ---- reset 协助 ----
    def snapshot_all_initials(self) -> None:
        for e in self.entities.values():
            e.snapshot_initial()

    def reset_all_to_initial(self) -> None:
        for e in self.entities.values():
            e.reset_to_initial()
