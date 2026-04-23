"""D16 meta-tests · Physics backend (dummy + pybullet)。"""
from __future__ import annotations

import pytest

from gameguard.domain import (
    BoundingBox,
    Entity,
    EntityKind,
    EntityRegistry,
    Vec3,
    WaitAction,
)
from gameguard.domain.scene import NavGrid, Scene, StaticGeometry
from gameguard.sandbox.questsim import make_questsim_sandbox
from gameguard.sandbox.questsim.physics import (
    DummyBackend,
    make_physics_backend,
)


# =========================================================================== #
# 1. DummyBackend 基础行为
# =========================================================================== #


def test_dummy_backend_is_physics_backend() -> None:
    b = DummyBackend()
    # Protocol 检查
    assert hasattr(b, "step") and hasattr(b, "add_static_box")


def test_dummy_static_box_does_not_move() -> None:
    b = DummyBackend()
    bbox = BoundingBox.from_min_max(Vec3(x=0, y=0, z=0), Vec3(x=1, y=1, z=1))
    b.add_static_box("wall", bbox)
    for _ in range(100):
        b.step(0.05)
    pos = b.get_pose("wall")
    # 静态物体位置固定
    assert pos == bbox.center()


def test_dummy_dynamic_box_falls_under_gravity() -> None:
    """无 force 的动态盒子应被重力拉下（z 减小）。"""
    b = DummyBackend()
    bbox = BoundingBox.from_center_size(Vec3(x=0, y=0, z=5), Vec3(x=1, y=1, z=1))
    b.add_dynamic_box("crate", bbox, mass=1.0)
    initial_z = b.get_pose("crate").z
    for _ in range(20):   # 1 秒
        b.step(0.05)
    new_z = b.get_pose("crate").z
    assert new_z < initial_z


def test_dummy_impulse_changes_velocity() -> None:
    b = DummyBackend()
    bbox = BoundingBox.from_center_size(Vec3(x=0, y=0, z=10), Vec3(x=1, y=1, z=1))
    b.add_dynamic_box("crate", bbox, mass=2.0)
    b.apply_impulse("crate", Vec3(x=10, y=0, z=0))   # 沿 +x 冲量
    # 一 tick 后盒子应朝 +x 移动（+重力 -z）
    b.step(0.05)
    pos = b.get_pose("crate")
    assert pos.x > 0


def test_dummy_static_blocks_dynamic_through_collision() -> None:
    """动态盒子上方往下落，碰到静态盒子时 velocity 归零，不再穿透。"""
    b = DummyBackend()
    # 静态地板在 z=0
    b.add_static_box(
        "floor",
        BoundingBox.from_min_max(
            Vec3(x=-5, y=-5, z=-1), Vec3(x=5, y=5, z=0)
        ),
    )
    # 动态盒子从 z=2 落下
    b.add_dynamic_box(
        "crate",
        BoundingBox.from_center_size(Vec3(x=0, y=0, z=2), Vec3(x=1, y=1, z=1)),
        mass=1.0,
    )
    for _ in range(100):
        b.step(0.05)
    final_z = b.get_pose("crate").z
    # 不应穿到 z<-1（地板下面）
    assert final_z > -0.5


def test_dummy_snapshot_restore_roundtrip() -> None:
    b = DummyBackend()
    b.add_dynamic_box(
        "crate",
        BoundingBox.from_center_size(Vec3(x=1, y=2, z=3), Vec3(x=1, y=1, z=1)),
        mass=1.0,
    )
    for _ in range(5):
        b.step(0.05)
    pose_before = b.get_pose("crate")
    snap = b.snapshot()

    # 继续推
    for _ in range(20):
        b.step(0.05)
    pose_after = b.get_pose("crate")
    assert pose_after != pose_before

    b.restore(snap)
    assert b.get_pose("crate") == pose_before


def test_dummy_raycast_hits_static_body() -> None:
    b = DummyBackend()
    bbox = BoundingBox.from_min_max(Vec3(x=0, y=0, z=0), Vec3(x=1, y=1, z=1))
    b.add_static_box("target", bbox)
    hit = b.raycast(Vec3(x=-1, y=0.5, z=0.5), Vec3(x=2, y=0.5, z=0.5))
    assert hit == "target"


def test_dummy_raycast_misses_if_no_body() -> None:
    b = DummyBackend()
    assert b.raycast(Vec3(x=0, y=0, z=0), Vec3(x=10, y=10, z=10)) is None


# =========================================================================== #
# 2. Factory 路由
# =========================================================================== #


def test_make_physics_backend_dummy() -> None:
    b = make_physics_backend("dummy")
    assert isinstance(b, DummyBackend)


def test_make_physics_backend_unknown_raises() -> None:
    with pytest.raises(ValueError):
        make_physics_backend("havok")


def test_make_physics_backend_pybullet_either_succeeds_or_clean_error() -> None:
    """pybullet 装了就成功；没装应抛 ImportError，消息指向安装命令。"""
    try:
        b = make_physics_backend("pybullet")
        # 装了就能用
        assert hasattr(b, "step")
    except ImportError as e:
        # 没装的错误消息要友好
        assert "pybullet" in str(e).lower()


# =========================================================================== #
# 3. QuestSim 集成物理：dynamic entity 被同步回 pos
# =========================================================================== #


def _scene_with_dynamic_crate() -> Scene:
    """一个玩家 + 一个 dynamic mass=5 的箱子，放在 (5,5,2)，应当会落下。"""
    reg = EntityRegistry()
    reg.add(Entity(
        id="p1", kind=EntityKind.PLAYER,
        pos=Vec3(x=1, y=1, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
    ))
    reg.add(Entity(
        id="crate_1", kind=EntityKind.PROP,
        pos=Vec3(x=5, y=5, z=2),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=1)),
        physics_mass=5.0,
        tags=["crate"],
    ))
    reg.snapshot_all_initials()
    nav = NavGrid(width=10, height=10)
    return Scene(
        id="physics_scene", entities=reg,
        geometry=StaticGeometry(), nav=nav,
    )


def test_questsim_physics_syncs_dynamic_entity_pos() -> None:
    """QuestSim 集成物理时，动态 Entity.pos 应每 tick 被物理结果覆盖。"""
    sim = make_questsim_sandbox("v1", scene=_scene_with_dynamic_crate())
    sim.reset(seed=1)
    crate = sim.entities.get("crate_1")
    initial_z = crate.pos.z

    # 推进 1 秒
    sim.step(WaitAction(seconds=1.0))

    # 因为重力，箱子应当下落
    assert crate.pos.z < initial_z


def test_questsim_physics_reset_rebuilds_world() -> None:
    """reset 应重建物理世界，箱子回到初始位置。"""
    sim = make_questsim_sandbox("v1", scene=_scene_with_dynamic_crate())
    sim.reset(seed=1)
    sim.step(WaitAction(seconds=2.0))   # 箱子已下落
    crate_after = sim.entities.get("crate_1").pos
    assert crate_after.z < 2.0 - 0.1

    sim.reset(seed=1)
    crate_reset = sim.entities.get("crate_1").pos
    # Entity.reset_to_initial 已恢复 pos
    assert abs(crate_reset.z - 2.0) < 1e-6


def test_questsim_physics_backend_name_pybullet_version_string() -> None:
    """`questsim:v1+pybullet` 正确路由到 pybullet backend。"""
    try:
        sim = make_questsim_sandbox("v1+pybullet")
    except ImportError:
        pytest.skip("pybullet 未安装，跳过 pybullet backend 集成测试")
    assert sim.physics_backend_name == "pybullet"
