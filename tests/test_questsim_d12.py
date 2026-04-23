"""D12 meta-tests · QuestSim 骨架 + Domain 基础数据类型。

验证：
  1. Vec3 算术、BoundingBox / AABB 相交判定
  2. Entity / EntityRegistry 基本增删查、reset 恢复初始状态
  3. QuestSim reset / step(Wait) / snapshot / restore round-trip
  4. QuestSim 对未支持的 Action 给出优雅的 ActionOutcome(accepted=False)
  5. CLI factory 路由 questsim:v1 能构造成功
  6. pysim 既有测试不受影响（由 pytest 全套覆盖，这里不重复）
"""
from __future__ import annotations

import pytest

from gameguard.cli import resolve_sandbox_factory
from gameguard.domain import (
    BoundingBox,
    CastAction,
    Entity,
    EntityKind,
    EntityRegistry,
    NoopAction,
    Vec3,
    WaitAction,
    aabb_contains_point,
    aabb_intersects,
)
from gameguard.sandbox.questsim import QuestSim, make_questsim_sandbox


# =========================================================================== #
# 1. Geom 原语
# =========================================================================== #


def test_vec3_arithmetic() -> None:
    a = Vec3(x=1, y=2, z=3)
    b = Vec3(x=4, y=5, z=6)
    assert (a + b) == Vec3(x=5, y=7, z=9)
    assert (b - a) == Vec3(x=3, y=3, z=3)
    assert (a * 2) == Vec3(x=2, y=4, z=6)
    assert abs(a.length() - ((1 + 4 + 9) ** 0.5)) < 1e-9
    assert a.distance_to(b) == (a - b).length()
    assert a.distance_sq_to(b) == 27  # 3^2 + 3^2 + 3^2


def test_vec3_division_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        _ = Vec3.zero() / 0


def test_vec3_normalized_zero_stays_zero() -> None:
    assert Vec3.zero().normalized() == Vec3.zero()


def test_bbox_intersection_contact_edge_inclusive() -> None:
    """相切边界在 inclusive 模式下算相交 —— 这是 Q-BUG-001 的 oracle。"""
    a = BoundingBox.from_min_max(Vec3(x=0, y=0, z=0), Vec3(x=1, y=1, z=1))
    b = BoundingBox.from_min_max(Vec3(x=1, y=0, z=0), Vec3(x=2, y=1, z=1))
    assert aabb_intersects(a, b) is True


def test_bbox_point_contains_boundary_behavior() -> None:
    """包含判定默认 inclusive（含边界）；严格 <（exclusive）是 v2 改坏路径。"""
    box = BoundingBox.from_min_max(Vec3(x=0, y=0, z=0), Vec3(x=10, y=10, z=10))
    p = Vec3(x=10, y=5, z=5)           # 恰在边界上
    assert aabb_contains_point(box, p, inclusive=True) is True
    assert aabb_contains_point(box, p, inclusive=False) is False


def test_bbox_from_center_size_equivalence() -> None:
    b1 = BoundingBox.from_min_max(Vec3(x=-1, y=-1, z=-1), Vec3(x=1, y=1, z=1))
    b2 = BoundingBox.from_center_size(Vec3.zero(), Vec3(x=2, y=2, z=2))
    assert b1.min == b2.min and b1.max == b2.max


# =========================================================================== #
# 2. Entity / EntityRegistry
# =========================================================================== #


def _make_npc(id_: str = "npc_test") -> Entity:
    return Entity(
        id=id_,
        kind=EntityKind.NPC,
        name="Test NPC",
        pos=Vec3(x=5, y=0, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        state={"dialogue_step": 0, "alive": True},
    )


def test_entity_registry_add_get() -> None:
    reg = EntityRegistry()
    e = _make_npc()
    reg.add(e)
    assert len(reg) == 1
    assert reg.get("npc_test") is e
    assert "npc_test" in reg


def test_entity_registry_duplicate_id_raises() -> None:
    reg = EntityRegistry()
    reg.add(_make_npc("dup"))
    with pytest.raises(ValueError):
        reg.add(_make_npc("dup"))


def test_entity_reset_restores_initial_pos_and_state() -> None:
    """Q-BUG-003 的 oracle 依赖这个语义：reset 后 pos 和 state 都恢复。"""
    e = _make_npc()
    e.snapshot_initial()
    # 模拟跑了一会 state 被改
    e.pos = Vec3(x=99, y=99, z=99)
    e.state["dialogue_step"] = 5
    e.state["alive"] = False

    e.reset_to_initial()
    assert e.pos == Vec3(x=5, y=0, z=0)
    assert e.state == {"dialogue_step": 0, "alive": True}


def test_entity_world_bbox_shifts_to_pos() -> None:
    e = _make_npc()
    # npc bbox 是中心 ±0.5/1，pos=(5,0,0)
    wbb = e.world_bbox()
    assert wbb.min.x == 4.5 and wbb.max.x == 5.5
    assert wbb.min.z == -1.0 and wbb.max.z == 1.0


def test_entity_registry_find_by_kind_tag() -> None:
    reg = EntityRegistry()
    reg.add(_make_npc("npc_1"))
    reg.add(_make_npc("npc_2"))
    e2 = reg.get("npc_2")
    e2.tags = ["boss"]
    assert len(reg.find_by_kind(EntityKind.NPC)) == 2
    assert [x.id for x in reg.find_by_tag("boss")] == ["npc_2"]


# =========================================================================== #
# 3. QuestSim 骨架行为
# =========================================================================== #


def test_questsim_factory_returns_running_sandbox() -> None:
    sim = make_questsim_sandbox("v1")
    assert isinstance(sim, QuestSim)
    assert sim.version == "v1"
    assert sim.physics_backend_name == "dummy"
    assert sim.info.name == "questsim-v1"
    assert sim.info.deterministic is True


def test_questsim_version_string_with_backend() -> None:
    try:
        sim = make_questsim_sandbox("v2+pybullet")
    except ImportError:
        pytest.skip("pybullet 未安装；dummy backend 已有单独测试")
    assert sim.version == "v2"
    assert sim.physics_backend_name == "pybullet"
    assert sim.info.name == "questsim-v2+pybullet"


def test_questsim_rejects_invalid_version() -> None:
    with pytest.raises(ValueError):
        make_questsim_sandbox("v3")          # 不存在
    with pytest.raises(ValueError):
        make_questsim_sandbox("v1+havok")    # 不存在的 backend


def test_questsim_reset_clears_state_and_log() -> None:
    sim = make_questsim_sandbox("v1")
    # reset 前先 step 几下制造状态
    sim.reset(seed=1)
    sim.step(WaitAction(seconds=0.5))
    assert sim.state().tick > 0
    assert len(sim.trace()) > 0

    # reset 应清空
    sim.reset(seed=42)
    assert sim.state().tick == 0
    assert sim.state().seed == 42
    assert len(sim.trace()) == 0


def test_questsim_step_wait_advances_ticks() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    result = sim.step(WaitAction(seconds=1.0))
    assert result.outcome.accepted is True
    # 1.0s / 0.05s = 20 ticks
    assert sim.state().tick == 20
    assert abs(sim.state().t - 1.0) < 1e-9
    # tick 事件 + 1 个 wait 产生的事件统计
    ticks = sim.trace().of_kind("tick")
    assert len(ticks) == 20


def test_questsim_step_noop_advances_one_tick() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    sim.step(NoopAction())
    assert sim.state().tick == 1


def test_questsim_step_unsupported_action_returns_rejected() -> None:
    """QuestSim 不接受技能用的 CastAction；应优雅拒绝，不 raise。"""
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    result = sim.step(CastAction(actor="p1", skill="skill_x", target="p1"))
    assert result.outcome.accepted is False
    assert "cast" in (result.outcome.reason or "").lower()


def test_questsim_snapshot_restore_roundtrip() -> None:
    """跑一段后 snapshot，mutate，restore，状态应回到 snapshot 时。"""
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=7)
    sim.step(WaitAction(seconds=0.5))
    snap = sim.snapshot()
    state_before = sim.state().model_dump()
    trace_before = len(sim.trace())

    # 继续跑制造差异
    sim.step(WaitAction(seconds=1.0))
    assert sim.state().tick > state_before["tick"]

    # restore 后完全一致
    sim.restore(snap)
    assert sim.state().model_dump() == state_before
    assert len(sim.trace()) == trace_before


def test_questsim_entities_reset_to_initial() -> None:
    """Q-BUG-003 oracle 的基础：reset 后 Entity 回到 initial（无论 initial 是什么）。"""
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    p1 = sim.entities.get("p1")
    initial_pos = p1.pos.model_copy(deep=True)
    # 人为移动 + 改 state
    p1.pos = Vec3(x=99, y=99, z=99)
    p1.state["looted"] = True

    sim.reset(seed=1)
    p1 = sim.entities.get("p1")
    assert p1.pos == initial_pos
    assert "looted" not in p1.state


# =========================================================================== #
# 4. CLI factory 路由
# =========================================================================== #


def test_cli_resolve_sandbox_factory_questsim() -> None:
    sim = resolve_sandbox_factory("questsim:v1")
    assert isinstance(sim, QuestSim)


def test_cli_resolve_sandbox_factory_questsim_with_backend() -> None:
    try:
        sim = resolve_sandbox_factory("questsim:v1+pybullet")
    except ImportError:
        pytest.skip("pybullet 未安装；dummy backend 已有单独测试")
    assert sim.physics_backend_name == "pybullet"
