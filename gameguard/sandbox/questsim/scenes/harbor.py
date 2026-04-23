"""Harbor · "初识港口" 分支任务场景（D18 示例）。

场景结构

一张 20×20 的港口小地图：
  - 玩家从 (1,1) 出发
  - 港口东 (15,15) 有 船长 NPC
  - 港口西 (5,15) 有 商人 NPC
  - 仓库 (10,2)，需要 alliance_chosen flag 才能进
  - 仓库里有一个"箱子"（dynamic mass=5）放在 (11,3)，需要推到压力板 (13,3)

任务图：
  S0 抵达港口（enter_volume harbor_gate at (8-12, 8-12)）
   ↓
  S1 选择阵营（active 等 interact 船长 or 商人）
    ├─ S2A: interact 船长 → flag alliance_chosen=captain
    └─ S2B: interact 商人 → flag alliance_chosen=merchant
   ↓（汇流：S3 需要 alliance_chosen flag）
  S3 进入仓库（enter_volume warehouse at (9-11, 1-4)）
   ↓
  S4 推箱到压力板（trigger via trigger_fired for box on pad）
   ↓（end）

v1 vs v2 场景级差异

- Q-BUG-002 分支死锁：v2 的 S2B（商人分支）忘记 set `alliance_chosen`
  flag。走 S2B 的玩家永远不能推进到 S3（requires_flags 不满足）。
- Q-BUG-005 nav 孤岛：v2 的 NavGrid 在 (12, 10) 处多挖一个 3×3 blocked
  区域把仓库入口的一部分切断，形成孤岛。

v1/v2 在其它地方完全一致，保证这两个 bug 可被对应 oracle 唯一检测。
"""
from __future__ import annotations

from gameguard.domain.dialogue import Choice, DialogueGraph, DialogueNode
from gameguard.domain.entity import Entity, EntityKind, EntityRegistry
from gameguard.domain.geom import BoundingBox, Vec3
from gameguard.domain.quest import (
    Quest,
    QuestStep,
    Trigger,
    TriggerKind,
)
from gameguard.domain.scene import NavGrid, Scene, StaticGeometry, TriggerVolume

# --- 场景与实体 ---
def _make_harbor_scene(version: str) -> Scene:
    """构造 harbor 场景。"""
    # ---- Entities ----
    reg = EntityRegistry()
    reg.add(Entity(
        id="p1", kind=EntityKind.PLAYER, name="Player",
        pos=Vec3(x=1.5, y=1.5, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        tags=["player"],
    ))
    reg.add(Entity(
        id="npc_captain", kind=EntityKind.NPC, name="船长",
        pos=Vec3(x=15.0, y=15.0, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        state={"dialogue_step": 0, "greeted": False},
        interact_range=3.0,
    ))
    reg.add(Entity(
        id="npc_merchant", kind=EntityKind.NPC, name="商人",
        pos=Vec3(x=5.0, y=15.0, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        state={"dialogue_step": 0},
        interact_range=3.0,
    ))
    reg.add(Entity(
        id="crate_1", kind=EntityKind.PROP, name="木箱",
        pos=Vec3(x=11.0, y=3.0, z=0.5),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=1)),
        physics_mass=5.0,
        tags=["pushable"],
    ))
    reg.add(Entity(
        id="pressure_plate", kind=EntityKind.PROP, name="压力板",
        pos=Vec3(x=13.0, y=3.0, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=0.1)),
    ))
    reg.snapshot_all_initials()

    # ---- NavGrid ----
    nav = NavGrid(width=20, height=20, cell_size=1.0, origin=Vec3.zero())
    # 四周墙
    for row in range(20):
        for col in range(20):
            if row in (0, 19) or col in (0, 19):
                nav.cells[row][col] = False

    # Q-BUG-005：v2 在 (12-14, 7-9) 额外 blocked，造成仓库入口孤岛
    # 注意：这是场景加载级别的 bug，不是 runtime 行为
    if version == "v2":
        for col in range(12, 15):
            for row in range(7, 10):
                nav.cells[row][col] = False
        # 再把 (12-14, 16-18) 也 blocked，让剩余 walkable 无法单连通
        for col in range(12, 15):
            for row in range(16, 19):
                nav.cells[row][col] = False

    # ---- Trigger volumes ----
    triggers = [
        TriggerVolume(
            id="harbor_gate",
            bbox=BoundingBox.from_min_max(
                Vec3(x=8, y=8, z=0), Vec3(x=12, y=12, z=3),
            ),
            target_quest_id="harbor",
            target_step_id="S0",
            once=True,
        ),
        TriggerVolume(
            id="warehouse_entrance",
            bbox=BoundingBox.from_min_max(
                Vec3(x=9, y=1, z=0), Vec3(x=11, y=4, z=3),
            ),
            target_quest_id="harbor",
            target_step_id="S3",
            once=True,
        ),
        # 压力板触发体
        TriggerVolume(
            id="pressure_plate_vol",
            bbox=BoundingBox.from_min_max(
                Vec3(x=12.5, y=2.5, z=0), Vec3(x=13.5, y=3.5, z=1),
            ),
            watch_entity_ids=["crate_1"],
            target_quest_id="harbor",
            target_step_id="S4",
            once=True,
        ),
    ]

    return Scene(
        id="harbor",
        name=f"初识港口 ({version})",
        entities=reg,
        triggers=triggers,
        geometry=StaticGeometry(),
        nav=nav,
    )

# --- Quest 图 ---
def _make_harbor_quest(version: str) -> Quest:
    """构造 harbor quest。

    v2 的 Q-BUG-002：S2B 忘记 set `quest.harbor.alliance_chosen` flag。
    结果：走 S2B 分支的玩家永远推进不了 S3（requires_flags 不满足）。
    """
    s2b_flags: dict = {}
    if version == "v1":
        s2b_flags = {"quest.harbor.alliance_chosen": "merchant"}
    # else v2: 空 dict = Q-BUG-002 漏 set

    return Quest(
        id="harbor",
        name="初识港口",
        description=f"港口小镇的入门任务（{version}）",
        start_step_id="S0",
        end_step_ids=["S4"],
        steps={
            "S0": QuestStep(
                id="S0",
                name="抵达港口",
                trigger=Trigger(
                    kind=TriggerKind.ENTER_VOLUME, target="harbor_gate"
                ),
                next_steps=["S1"],
            ),
            "S1": QuestStep(
                id="S1",
                name="选择阵营",
                next_steps=["S2A", "S2B"],
            ),
            "S2A": QuestStep(
                id="S2A",
                name="帮船长",
                trigger=Trigger(
                    kind=TriggerKind.INTERACT_ENTITY, target="npc_captain"
                ),
                on_enter_flags={
                    "quest.harbor.alliance_chosen": "captain",
                },
                next_steps=["S3"],
            ),
            "S2B": QuestStep(
                id="S2B",
                name="帮商人",
                trigger=Trigger(
                    kind=TriggerKind.INTERACT_ENTITY, target="npc_merchant"
                ),
                # v1 set flag；v2 漏 set（Q-BUG-002）
                on_enter_flags=s2b_flags,
                next_steps=["S3"],
            ),
            "S3": QuestStep(
                id="S3",
                name="进入仓库",
                trigger=Trigger(
                    kind=TriggerKind.ENTER_VOLUME, target="warehouse_entrance"
                ),
                requires_flags=["quest.harbor.alliance_chosen"],
                next_steps=["S4"],
            ),
            "S4": QuestStep(
                id="S4",
                name="推箱到压力板",
                trigger=Trigger(
                    kind=TriggerKind.ENTER_VOLUME, target="pressure_plate_vol"
                ),
            ),
        },
    )

# --- Dialogues ---
def _make_captain_dialogue() -> DialogueGraph:
    return DialogueGraph(
        id="captain_intro",
        npc_id="npc_captain",
        root_node_id="greet",
        nodes={
            "greet": DialogueNode(
                id="greet", speaker="npc_captain",
                text="嗨，新来的。",
                choices=[
                    Choice(label="你好", next_node="offer"),
                    Choice(label="路过", next_node=None),
                ],
            ),
            "offer": DialogueNode(
                id="offer", speaker="npc_captain",
                text="能帮我搬搬货吗？",
                choices=[
                    Choice(label="好", next_node=None),
                    Choice(label="再说", next_node=None),
                ],
            ),
        },
    )

def _make_merchant_dialogue() -> DialogueGraph:
    return DialogueGraph(
        id="merchant_intro",
        npc_id="npc_merchant",
        root_node_id="greet",
        nodes={
            "greet": DialogueNode(
                id="greet", speaker="npc_merchant",
                text="生意好呀。",
                choices=[
                    Choice(label="我能帮什么？", next_node="offer"),
                    Choice(label="走了", next_node=None),
                ],
            ),
            "offer": DialogueNode(
                id="offer", speaker="npc_merchant",
                text="陪我谈个价钱？",
                choices=[
                    Choice(label="好", next_node=None),
                ],
            ),
        },
    )

# --- 对外入口 ---
def load_harbor(version: str = "v1"):
    """构造完整 harbor 套件（scene + quest + dialogues）。"""
    return {
        "scene": _make_harbor_scene(version),
        "quest": _make_harbor_quest(version),
        "dialogues": {
            "npc_captain": _make_captain_dialogue(),
            "npc_merchant": _make_merchant_dialogue(),
        },
    }
