"""D14 meta-tests · Quest 状态机 + Trigger + InteractAction。"""
from __future__ import annotations

import pytest

from gameguard.domain import (
    BoundingBox,
    Entity,
    EntityKind,
    EntityRegistry,
    InteractAction,
    MoveToAction,
    Vec3,
    WaitAction,
)
from gameguard.domain.quest import (
    Quest,
    QuestFlags,
    QuestStep,
    Trigger,
    TriggerKind,
)
from gameguard.domain.scene import (
    NavGrid,
    Scene,
    StaticGeometry,
    TriggerVolume,
)
from gameguard.sandbox.questsim import make_questsim_sandbox


# =========================================================================== #
# 1. QuestFlags 命名空间校验
# =========================================================================== #


def test_quest_flags_valid_prefixes() -> None:
    flags = QuestFlags()
    flags.set("quest.harbor.open", True)
    flags.set("dlg.captain.greeted", True)
    flags.set("sys.save_slot", "auto")
    flags.set("scene.crate_opened", True)
    assert flags.is_true("quest.harbor.open")
    assert flags.get("sys.save_slot") == "auto"


def test_quest_flags_reject_invalid_prefix() -> None:
    flags = QuestFlags()
    with pytest.raises(ValueError):
        flags.set("foo.bar", True)


# =========================================================================== #
# 2. Quest reachability 与 orphan flag 分析
# =========================================================================== #


def _simple_quest() -> Quest:
    """S0 → S1 → S2 的直线三步任务，S1 给 set 一个 flag，S2 读它。"""
    return Quest(
        id="test_q",
        start_step_id="S0",
        end_step_ids=["S2"],
        steps={
            "S0": QuestStep(id="S0", name="start", next_steps=["S1"]),
            "S1": QuestStep(
                id="S1",
                name="mid",
                trigger=Trigger(kind=TriggerKind.INTERACT_ENTITY, target="npc_a"),
                on_enter_flags={"quest.test_q.flag_a": True},
                next_steps=["S2"],
            ),
            "S2": QuestStep(
                id="S2",
                name="end",
                requires_flags=["quest.test_q.flag_a"],
            ),
        },
    )


def test_quest_reachable_step_ids() -> None:
    q = _simple_quest()
    reach = q.reachable_step_ids()
    assert reach == {"S0", "S1", "S2"}


def test_quest_orphan_flag_none_in_clean_quest() -> None:
    q = _simple_quest()
    assert q.orphan_flags() == set()


def test_quest_orphan_flag_detects_unused() -> None:
    """S1 额外 set 一个没人读的 flag，应被 orphan 抓到。"""
    q = _simple_quest()
    q.steps["S1"].on_enter_flags["quest.test_q.unused"] = True
    assert q.orphan_flags() == {"quest.test_q.unused"}


def test_quest_step_ids_dict_key_matches_id() -> None:
    """Pydantic validator 强制 dict key == step.id。"""
    with pytest.raises(ValueError):
        Quest(
            id="q",
            start_step_id="S0",
            steps={"WRONG_KEY": QuestStep(id="S0")},
        )


# =========================================================================== #
# 3. QuestRuntime · step 推进（通过 InteractAction）
# =========================================================================== #


def _scene_with_npc() -> Scene:
    """默认 20×20 grid + 1 个 NPC 在 (3,3)。"""
    reg = EntityRegistry()
    reg.add(Entity(
        id="p1", kind=EntityKind.PLAYER, name="Player",
        pos=Vec3(x=1.5, y=1.5, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
    ))
    reg.add(Entity(
        id="npc_a", kind=EntityKind.NPC, name="NPC A",
        pos=Vec3(x=3, y=3, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        interact_range=3.0,
    ))
    reg.snapshot_all_initials()

    nav = NavGrid(width=20, height=20, cell_size=1.0, origin=Vec3.zero())
    for row in range(20):
        for col in range(20):
            if row == 0 or row == 19 or col == 0 or col == 19:
                nav.cells[row][col] = False

    return Scene(
        id="test_scene",
        entities=reg,
        triggers=[],
        geometry=StaticGeometry(),
        nav=nav,
    )


def test_quest_starts_automatically_on_reset() -> None:
    """reset 时应自动激活 start step + 发 quest_reset 事件。"""
    sim = make_questsim_sandbox("v1", scene=_scene_with_npc(), quest=_simple_quest())
    sim.reset(seed=1)
    assert sim.trace().of_kind("quest_reset")
    # S0 没有 trigger → 应立即 completed
    assert sim.config.quest.steps["S0"].status == "completed"
    assert sim.config.quest.steps["S1"].status == "active"


def test_quest_interact_advances_step_and_sets_flag() -> None:
    sim = make_questsim_sandbox("v1", scene=_scene_with_npc(), quest=_simple_quest())
    sim.reset(seed=1)
    # 玩家靠近 NPC 再 interact
    sim.step(MoveToAction(actor="p1", pos=Vec3(x=3, y=3, z=0), mode="teleport"))
    r = sim.step(InteractAction(actor="p1", entity_id="npc_a"))
    assert r.outcome.accepted
    # S1 应 completed，flag 被 set
    assert sim.config.quest.steps["S1"].status == "completed"
    assert sim.config.quest.flags.is_true("quest.test_q.flag_a")
    # S2 的 requires_flags 被满足 → 应自动激活 → 无 trigger 情况下不会自动完成
    # 我们简单 quest 里 S2 没 trigger，所以它应该是 active（但无法推进）
    # 这里的行为需要设计：S2 无 trigger 意味着它是 end 被动等待。
    # 目前 runtime 只在 _complete_step 检查 end_step_ids；
    # S2 不会自动完成。实际任务设计里 end step 应有 trigger。


def test_quest_interact_distance_enforced() -> None:
    """interact 距离超出 interact_range 应 reject。"""
    sim = make_questsim_sandbox("v1", scene=_scene_with_npc(), quest=_simple_quest())
    sim.reset(seed=1)
    # 玩家还在 (1.5,1.5)，NPC 在 (3,3)。dist = sqrt(1.5^2+1.5^2) ≈ 2.12，
    # 在 NPC interact_range=3.0 内。先把 NPC 调远看拒绝行为：
    sim.entities.get("npc_a").pos = Vec3(x=10, y=10, z=0)
    r = sim.step(InteractAction(actor="p1", entity_id="npc_a"))
    assert r.outcome.accepted is False
    assert "距离" in (r.outcome.reason or "")


# =========================================================================== #
# 4. TriggerVolume · 自动检测
# =========================================================================== #


def _scene_with_volume() -> Scene:
    """默认 scene + 1 个 TriggerVolume 在 (5,5)～(7,7)。"""
    scene = _scene_with_npc()
    scene.triggers.append(
        TriggerVolume(
            id="vol_gate",
            bbox=BoundingBox.from_min_max(Vec3(x=5, y=5, z=0), Vec3(x=7, y=7, z=2)),
            target_quest_id="test_q",
            target_step_id="S1",
            once=True,
        )
    )
    return scene


def _volume_quest() -> Quest:
    """S0 → S1 (trigger=enter_volume vol_gate) → S2 end。"""
    return Quest(
        id="test_q",
        start_step_id="S0",
        end_step_ids=["S2"],
        steps={
            "S0": QuestStep(id="S0", next_steps=["S1"]),
            "S1": QuestStep(
                id="S1",
                trigger=Trigger(
                    kind=TriggerKind.ENTER_VOLUME,
                    target="vol_gate",
                ),
                next_steps=["S2"],
            ),
            "S2": QuestStep(id="S2"),
        },
    )


def test_trigger_volume_fires_on_enter() -> None:
    """玩家 teleport 进 volume → 触发 trigger_fired → quest step 推进。"""
    sim = make_questsim_sandbox("v1", scene=_scene_with_volume(), quest=_volume_quest())
    sim.reset(seed=1)
    assert sim.config.quest.steps["S1"].status == "active"
    # 瞬移到 volume 中心
    sim.step(MoveToAction(actor="p1", pos=Vec3(x=6, y=6, z=0), mode="teleport"))
    # 再 Wait 一 tick 让 _tick_triggers 跑
    sim.step(WaitAction(seconds=0.1))

    fired = sim.trace().of_kind("trigger_fired")
    enter_vol_fired = [e for e in fired if e.meta.get("trigger_kind") == "enter_volume"]
    assert len(enter_vol_fired) >= 1
    # quest 推进
    assert sim.config.quest.steps["S1"].status == "completed"


def test_trigger_volume_fires_once_only() -> None:
    """once=True 的 volume 触发一次就不再触发。"""
    sim = make_questsim_sandbox("v1", scene=_scene_with_volume(), quest=_volume_quest())
    sim.reset(seed=1)
    sim.step(MoveToAction(actor="p1", pos=Vec3(x=6, y=6, z=0), mode="teleport"))
    sim.step(WaitAction(seconds=0.5))
    # 已触发，fired=True
    vol = sim.config.scene.get_trigger("vol_gate")
    assert vol is not None and vol.fired is True

    # 离开再回来，不该再触发
    sim.step(MoveToAction(actor="p1", pos=Vec3(x=1.5, y=1.5, z=0), mode="teleport"))
    sim.step(WaitAction(seconds=0.5))
    sim.step(MoveToAction(actor="p1", pos=Vec3(x=6, y=6, z=0), mode="teleport"))
    sim.step(WaitAction(seconds=0.5))

    enter_vol_count = sum(
        1 for e in sim.trace().of_kind("trigger_fired")
        if e.meta.get("trigger_kind") == "enter_volume" and e.target == "vol_gate"
    )
    assert enter_vol_count == 1
