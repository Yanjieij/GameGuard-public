"""QuestSim 工厂 · 装配初始场景 + 版本 + 物理后端。

版本字符串语法

CLI 的 `--sandbox questsim:XX` 中 XX 的语法：

  v1                   → version='v1', physics='dummy'
  v2                   → version='v2', physics='dummy'
  v1+pybullet          → version='v1', physics='pybullet'
  v2+pybullet          → version='v2', physics='pybullet'

这种"冒号主版本 + 加号后端"的语法灵感来自 Linux 包管理器（`python3:ubuntu+backports`）。

D12 阶段能力

本文件当前只能：
  - `make_questsim_sandbox('v1')` 返回一个空场景的 QuestSim（1 个 player 实体）
  - `v2` 暂时等同 v1（植入 bug 在 D18 实装）

D13+ 会加入 Scene 装配、NavGrid 读取、Quest 定义加载等。
"""
from __future__ import annotations

from gameguard.domain.dialogue import DialogueGraph
from gameguard.domain.entity import (
    Entity,
    EntityKind,
    EntityRegistry,
)
from gameguard.domain.geom import BoundingBox, Vec3
from gameguard.domain.quest import Quest
from gameguard.domain.scene import NavGrid, Scene, StaticGeometry
from gameguard.sandbox.questsim.core import QuestSim, QuestSimConfig
from gameguard.sandbox.questsim.save_codec import (
    LossyJsonSaveCodec,
    PickleSaveCodec,
)

def _parse_version_string(raw: str) -> tuple[str, str]:
    """把 "v1" / "v2+pybullet" 拆成 (version, physics_backend)。"""
    if "+" in raw:
        version, backend = raw.split("+", 1)
    else:
        version, backend = raw, "dummy"
    if version not in ("v1", "v2"):
        raise ValueError(
            f"不支持的 questsim 版本 {version!r}；当前只支持 v1 / v2"
        )
    if backend not in ("dummy", "pybullet"):
        raise ValueError(
            f"不支持的 physics backend {backend!r}；当前只支持 dummy / pybullet"
        )
    return version, backend

def _default_entities() -> EntityRegistry:
    """D12 默认场景：一个 player 站在原点的 1x1x2 包围盒。

    D13 之后会被 harbor.py 的真实场景替换；这里只是让空 QuestSim 有个东西
    可以 reset / snapshot。
    """
    reg = EntityRegistry()
    reg.add(
        Entity(
            id="p1",
            kind=EntityKind.PLAYER,
            name="Player",
            pos=Vec3.zero(),
            bbox=BoundingBox.from_center_size(
                center=Vec3.zero(),
                size=Vec3(x=1.0, y=1.0, z=2.0),
            ),
            tags=["player"],
        )
    )
    reg.snapshot_all_initials()
    return reg

def _make_default_scene() -> Scene:
    """D13 默认场景：20×20 单元的空地，四周墙壁；一个玩家在 (1,1)。

    足够测 A* 寻路。harbor 场景 D14 用单独文件替代。
    """
    nav = NavGrid(width=20, height=20, cell_size=1.0, origin=Vec3.zero())
    # 四周 1 格宽的墙
    for col in range(20):
        nav.cells[0][col] = False
        nav.cells[19][col] = False
    for row in range(20):
        nav.cells[row][0] = False
        nav.cells[row][19] = False

    # Player 放在 (1.5, 1.5)（cell 1,18 的中心处）
    entities = EntityRegistry()
    entities.add(
        Entity(
            id="p1",
            kind=EntityKind.PLAYER,
            name="Player",
            pos=Vec3(x=1.5, y=1.5, z=0),
            bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
            tags=["player"],
        )
    )
    entities.snapshot_all_initials()

    scene = Scene(
        id="default_grid",
        name="D13 Default Grid",
        entities=entities,
        triggers=[],
        geometry=StaticGeometry(),
        nav=nav,
    )
    return scene

def make_harbor_sandbox(version_spec: str = "v1") -> QuestSim:
    """便捷入口：预装 harbor 分支任务场景的 QuestSim（D18 示例）。

    对应 CLI 命令：`gameguard run --sandbox questsim:v1-harbor`。
    """
    from gameguard.sandbox.questsim.scenes.harbor import load_harbor
    version, _ = _parse_version_string(version_spec)
    bundle = load_harbor(version)
    return make_questsim_sandbox(
        version_spec,
        scene=bundle["scene"],
        quest=bundle["quest"],
        dialogues=bundle["dialogues"],
    )

def make_questsim_sandbox(
    version_spec: str = "v1",
    *,
    scene: Scene | None = None,
    quest: Quest | None = None,
    dialogues: dict[str, DialogueGraph] | None = None,
) -> QuestSim:
    """按 version 字符串装配并返回一个 QuestSim 实例。

    参数：
      - version_spec：见模块顶注释（'v1' / 'v2+pybullet' 等）
      - scene：可选；若提供则用它；否则用 `_make_default_scene()`
      - quest：可选；若提供则绑定 QuestRuntime
      - dialogues：可选；npc_id → DialogueGraph
    """
    version, backend = _parse_version_string(version_spec)
    if scene is None:
        scene = _make_default_scene()

    # D18 v2 bug flags：根据 version 把 config 里 4 个 flag 切到 v2 值。
    # 这样 v1 / v2 只在这一处 diverge，维护简单、行为透明。
    if version == "v1":
        trigger_boundary_inclusive = True
        reset_restores_entity_state = True
        save_codec_factory = PickleSaveCodec
    else:  # v2
        trigger_boundary_inclusive = False     # Q-BUG-001
        reset_restores_entity_state = False    # Q-BUG-003
        save_codec_factory = LossyJsonSaveCodec  # Q-BUG-004
        # Q-BUG-002（任务分支死锁）= quest spec 差异（harbor.py 里构造 v1/v2 quest）
        # Q-BUG-005（nav 孤岛）= scene 差异（harbor.py 里构造 v1/v2 scene）

    config = QuestSimConfig(
        version=version,
        physics_backend=backend,
        entities=scene.entities,   # 共享同一 EntityRegistry 引用
        scene=scene,
        quest=quest,
        dialogues=dialogues or {},
        save_codec_factory=save_codec_factory,
        trigger_boundary_inclusive=trigger_boundary_inclusive,
        reset_restores_entity_state=reset_restores_entity_state,
    )
    return QuestSim(config)
