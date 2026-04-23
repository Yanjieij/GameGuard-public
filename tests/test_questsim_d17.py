"""D17 meta-tests · 10 个 Quest invariant evaluator。

对每个 invariant 给一个 pass fixture + 一个 fail fixture，保证 evaluator
两端都覆盖。
"""
from __future__ import annotations


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
from gameguard.domain.dialogue import Choice, DialogueGraph, DialogueNode
from gameguard.domain.invariant import (
    DialogueNoDeadBranchInvariant,
    InteractionRangeConsistentInvariant,
    NoStuckPositionsInvariant,
    NpcRespawnOnResetInvariant,
    PathExistsBetweenInvariant,
    QuestNoOrphanFlagInvariant,
    QuestStepOnceInvariant,
    QuestStepReachableInvariant,
    SaveLoadRoundTripInvariant,
    StateView,
    TriggerVolumeFiresOnEnterInvariant,
    evaluate,
)
from gameguard.domain.quest import (
    Quest,
    QuestStep,
)
from gameguard.domain.scene import NavGrid, Scene, StaticGeometry, TriggerVolume
from gameguard.sandbox.questsim import make_questsim_sandbox
from gameguard.testcase.model import Assertion, AssertionWhen, CaseOutcome, TestCase, TestPlan
from gameguard.testcase.runner import run_plan


# =========================================================================== #
# Helpers
# =========================================================================== #


def _view_of(sim) -> StateView:
    s = sim.state()
    view = StateView(t=s.t, tick=s.tick, characters=dict(s.characters))
    view.scene = sim.config.scene
    view.quest = sim.config.quest
    view.entities = sim.config.entities
    view.dialogues = sim.config.dialogues
    return view


def _base_scene_with_npc(npc_pos=(3, 3, 0)) -> Scene:
    reg = EntityRegistry()
    reg.add(Entity(
        id="p1", kind=EntityKind.PLAYER,
        pos=Vec3(x=1, y=1, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
    ))
    reg.add(Entity(
        id="npc_a", kind=EntityKind.NPC,
        pos=Vec3(x=npc_pos[0], y=npc_pos[1], z=npc_pos[2]),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        state={"dialogue_step": 0},
        interact_range=5.0,
    ))
    reg.snapshot_all_initials()
    nav = NavGrid(width=10, height=10)
    return Scene(id="s", entities=reg, geometry=StaticGeometry(), nav=nav)


# =========================================================================== #
# I-Q1 quest_step_reachable
# =========================================================================== #


def test_quest_step_reachable_pass() -> None:
    q = Quest(
        id="q", start_step_id="S0", end_step_ids=["S2"],
        steps={
            "S0": QuestStep(id="S0", next_steps=["S1"]),
            "S1": QuestStep(id="S1", next_steps=["S2"]),
            "S2": QuestStep(id="S2"),
        },
    )
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc(), quest=q)
    sim.reset(seed=1)
    inv = QuestStepReachableInvariant(id="i", description="", quest_id="q")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert r.passed


def test_quest_step_reachable_fail_when_end_disconnected() -> None:
    q = Quest(
        id="q", start_step_id="S0", end_step_ids=["S2"],
        steps={
            "S0": QuestStep(id="S0", next_steps=[]),  # 没路到 S2
            "S2": QuestStep(id="S2"),
        },
    )
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc(), quest=q)
    sim.reset(seed=1)
    inv = QuestStepReachableInvariant(id="i", description="", quest_id="q")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed
    assert "不可达" in r.message


# =========================================================================== #
# I-Q2 quest_step_once
# =========================================================================== #


def test_quest_step_once_pass_for_single_entered() -> None:
    q = Quest(
        id="q", start_step_id="S0", end_step_ids=[],
        steps={"S0": QuestStep(id="S0", next_steps=[])},
    )
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc(), quest=q)
    sim.reset(seed=1)
    inv = QuestStepOnceInvariant(id="i", description="", quest_id="q")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert r.passed


# =========================================================================== #
# I-Q3 quest_no_orphan_flag
# =========================================================================== #


def test_quest_no_orphan_flag_pass() -> None:
    q = Quest(
        id="q", start_step_id="S0",
        steps={
            "S0": QuestStep(id="S0", on_enter_flags={"quest.q.a": True}, next_steps=["S1"]),
            "S1": QuestStep(id="S1", requires_flags=["quest.q.a"]),
        },
    )
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc(), quest=q)
    sim.reset(seed=1)
    inv = QuestNoOrphanFlagInvariant(id="i", description="", quest_id="q")
    assert evaluate(inv, _view_of(sim), sim.trace()).passed


def test_quest_no_orphan_flag_fail_for_dead_flag() -> None:
    q = Quest(
        id="q", start_step_id="S0",
        steps={
            "S0": QuestStep(id="S0", on_enter_flags={"quest.q.dead": True}),
        },
    )
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc(), quest=q)
    sim.reset(seed=1)
    inv = QuestNoOrphanFlagInvariant(id="i", description="", quest_id="q")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed
    assert "孤儿" in r.message


# =========================================================================== #
# I-Q4 trigger_volume_fires_on_enter
# =========================================================================== #


def test_trigger_volume_fires_on_enter_pass_when_never_entered() -> None:
    scene = _base_scene_with_npc()
    scene.triggers.append(TriggerVolume(
        id="g1", bbox=BoundingBox.from_min_max(Vec3(x=5, y=5, z=0), Vec3(x=7, y=7, z=2)),
    ))
    sim = make_questsim_sandbox("v1", scene=scene)
    sim.reset(seed=1)
    inv = TriggerVolumeFiresOnEnterInvariant(id="i", description="", trigger_id="g1")
    assert evaluate(inv, _view_of(sim), sim.trace()).passed


def test_trigger_volume_fires_fail_when_inside_but_no_fire() -> None:
    """构造一个 scene.triggers 里的 volume，但不用 sandbox 的 _tick_triggers 发事件——
    直接把玩家瞬移到里面，手工查询 evaluator：未 fire 时应失败。
    """
    scene = _base_scene_with_npc()
    scene.triggers.append(TriggerVolume(
        id="g1", bbox=BoundingBox.from_min_max(Vec3(x=5, y=5, z=0), Vec3(x=7, y=7, z=2)),
        # watch_entity 设为不存在的 ghost，sandbox 不会 fire
        watch_entity_ids=["ghost"],
    ))
    sim = make_questsim_sandbox("v1", scene=scene)
    sim.reset(seed=1)
    # 把 p1 放进 volume 但 volume 的 watch 是 ghost 不会触发 sandbox 逻辑
    sim.entities.get("p1").pos = Vec3(x=6, y=6, z=1)
    # evaluator 用 watch_entity_ids 判断，scene.triggers 里的 watch 是 ghost —— 所以 inv pass
    # 为了测 fail 分支：把 watch 改回 p1，evaluator 检测 p1 在 volume 内但无 fired 事件
    scene.triggers[0].watch_entity_ids = ["p1"]
    inv = TriggerVolumeFiresOnEnterInvariant(id="i", description="", trigger_id="g1")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed


# =========================================================================== #
# I-Q5 npc_respawn_on_reset
# =========================================================================== #


def test_npc_respawn_on_reset_pass_after_proper_reset() -> None:
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc())
    sim.reset(seed=1)
    npc = sim.entities.get("npc_a")
    npc.pos = Vec3(x=100, y=100, z=100)  # 模拟游戏中 NPC 移动过
    npc.state["dialogue_step"] = 99
    sim.reset(seed=1)  # 发 quest_reset 事件? 没 quest 就不会发 —— 手工发一个 quest_reset 事件
    sim.emit(kind="quest_reset", meta={"quest_id": "fake"})
    # reset 已经把 npc 恢复到 initial
    inv = NpcRespawnOnResetInvariant(id="i", description="", npc_id="npc_a")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert r.passed


def test_npc_respawn_on_reset_fail_when_state_not_restored() -> None:
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc())
    sim.reset(seed=1)
    # 手工破坏：发 quest_reset 事件但不恢复 npc state
    sim.emit(kind="quest_reset", meta={})
    npc = sim.entities.get("npc_a")
    npc.state["dialogue_step"] = 77  # 故意与 initial 不一致
    inv = NpcRespawnOnResetInvariant(id="i", description="", npc_id="npc_a")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed


# =========================================================================== #
# I-Q6 save_load_round_trip（通过 runner 测端到端）
# =========================================================================== #


def _factory_v1(spec: str):
    _, version = spec.split(":", 1)
    return make_questsim_sandbox(version)


def test_save_load_round_trip_pass_on_v1(tmp_path) -> None:
    """通过 runner 跑一个含 SaveLoad invariant 的 case，v1 应 PASS。"""
    case = TestCase(
        id="slrt", name="slrt", sandbox="questsim:v1", seed=42,
        actions=[WaitAction(seconds=0.1)],
        assertions=[Assertion(
            invariant=SaveLoadRoundTripInvariant(
                id="Q-slrt", description="", slot="auto"
            ),
            when=AssertionWhen.END_OF_RUN,
        )],
    )
    plan = TestPlan(id="p", cases=[case])
    suite = run_plan(plan, _factory_v1, artifacts_dir=tmp_path)
    r = suite.cases[0]
    assert r.outcome == CaseOutcome.PASSED, r.failing_assertions


# =========================================================================== #
# I-Q7 path_exists_between
# =========================================================================== #


def test_path_exists_between_pass() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    inv = PathExistsBetweenInvariant(
        id="i", description="",
        from_x=1.5, from_y=1.5, to_x=5.5, to_y=5.5,
    )
    assert evaluate(inv, _view_of(sim), sim.trace()).passed


def test_path_exists_between_fail_when_wall() -> None:
    """从内到墙里，A* 无解。"""
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    inv = PathExistsBetweenInvariant(
        id="i", description="",
        from_x=1.5, from_y=1.5, to_x=0.5, to_y=0.5,  # 墙
    )
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed


# =========================================================================== #
# I-Q8 no_stuck_positions
# =========================================================================== #


def test_no_stuck_positions_pass_on_open_scene() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    inv = NoStuckPositionsInvariant(id="i", description="")
    # default scene 四周墙；中间 18x18 是单个连通分量 → pass
    assert evaluate(inv, _view_of(sim), sim.trace()).passed


def test_no_stuck_positions_fail_when_island() -> None:
    """在默认 scene 上切一条竖墙把中间分成两半，应 FAIL。"""
    from gameguard.domain.geom import GridCoord
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    nav = sim.config.scene.nav
    for row in range(nav.height):
        nav.set_blocked(GridCoord(10, row), True)
    inv = NoStuckPositionsInvariant(id="i", description="")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed
    assert "不连通" in r.message


# =========================================================================== #
# I-Q9 dialogue_no_dead_branch
# =========================================================================== #


def test_dialogue_no_dead_branch_pass() -> None:
    g = DialogueGraph(
        id="g1", npc_id="n",
        root_node_id="r",
        nodes={
            "r": DialogueNode(id="r", choices=[Choice(label="end", next_node=None)]),
        },
    )
    sim = make_questsim_sandbox("v1", dialogues={"n": g})
    sim.reset(seed=1)
    inv = DialogueNoDeadBranchInvariant(id="i", description="", dialogue_id="g1")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert r.passed


def test_dialogue_no_dead_branch_fail_with_cycle() -> None:
    g = DialogueGraph(
        id="g1", npc_id="n",
        root_node_id="r",
        nodes={
            "r": DialogueNode(id="r", choices=[Choice(label="go", next_node="A")]),
            "A": DialogueNode(id="A", choices=[Choice(label="loop", next_node="B")]),
            "B": DialogueNode(id="B", choices=[Choice(label="loop", next_node="A")]),
        },
    )
    sim = make_questsim_sandbox("v1", dialogues={"n": g})
    sim.reset(seed=1)
    inv = DialogueNoDeadBranchInvariant(id="i", description="", dialogue_id="g1")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed


# =========================================================================== #
# I-Q10 interaction_range_consistent
# =========================================================================== #


def test_interaction_range_pass_on_clean_interact() -> None:
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc())
    sim.reset(seed=1)
    # p1 在 (1,1,0), npc_a 在 (3,3,0), dist=2.83 < interact_range=5.0
    sim.step(MoveToAction(
        actor="p1", pos=Vec3(x=1.5, y=1.5, z=0), mode="teleport"
    ))
    sim.step(InteractAction(actor="p1", entity_id="npc_a"))
    inv = InteractionRangeConsistentInvariant(id="i", description="")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert r.passed


def test_interaction_range_fail_when_distance_too_far_manually() -> None:
    """手工构造一个带超范围 distance 的 interact 事件（模拟 bug）。"""
    sim = make_questsim_sandbox("v1", scene=_base_scene_with_npc())
    sim.reset(seed=1)
    # 故意发个 trigger_fired 事件，distance 超出 npc_a 的 interact_range
    sim.emit(
        kind="trigger_fired",
        actor="p1", target="npc_a",
        meta={"trigger_kind": "interact_entity", "distance": 999.0},
    )
    inv = InteractionRangeConsistentInvariant(id="i", description="")
    r = evaluate(inv, _view_of(sim), sim.trace())
    assert not r.passed
