"""D15 meta-tests · Dialogue + Save/Load。"""
from __future__ import annotations


from gameguard.domain import (
    BoundingBox,
    DialogueAction,
    Entity,
    EntityKind,
    EntityRegistry,
    LoadAction,
    MoveToAction,
    SaveAction,
    Vec3,
)
from gameguard.domain.dialogue import Choice, DialogueGraph, DialogueNode
from gameguard.domain.quest import (
    Quest,
    QuestStep,
)
from gameguard.domain.scene import NavGrid, Scene, StaticGeometry
from gameguard.sandbox.questsim import make_questsim_sandbox
from gameguard.sandbox.questsim.save_codec import (
    LossyJsonSaveCodec,
    PickleSaveCodec,
)


# =========================================================================== #
# 1. DialogueGraph 结构与可达性
# =========================================================================== #


def _simple_graph() -> DialogueGraph:
    """root → (0) → mid → (0,1) → {leafA, leafB}。"""
    return DialogueGraph(
        id="captain",
        npc_id="npc_captain",
        root_node_id="root",
        nodes={
            "root": DialogueNode(
                id="root",
                speaker="npc_captain",
                text="嗨，陌生人",
                choices=[
                    Choice(label="你好", next_node="mid"),
                ],
            ),
            "mid": DialogueNode(
                id="mid",
                speaker="npc_captain",
                text="有何贵干？",
                choices=[
                    Choice(
                        label="帮你",
                        next_node="leafA",
                        sets_flag=("quest.test_q.ally_captain", True),
                    ),
                    Choice(label="路过",  next_node="leafB"),
                ],
            ),
            "leafA": DialogueNode(id="leafA", text="谢谢！"),
            "leafB": DialogueNode(id="leafB", text="慢走"),
        },
    )


def test_dialogue_graph_reachable_from_root() -> None:
    g = _simple_graph()
    assert g.reachable_from_root() == {"root", "mid", "leafA", "leafB"}


def test_dialogue_graph_can_reach_terminal_all_nodes() -> None:
    """所有节点最终都能到 terminal（leafA/leafB）。"""
    g = _simple_graph()
    can = g.nodes_that_can_reach_terminal()
    assert can == {"root", "mid", "leafA", "leafB"}


def test_dialogue_graph_dead_end_detected() -> None:
    """把 mid 的两个 choice 都指 None（终止）—— 仍能到 terminal。
    但若加一个"孤立" node（没人指向它），可达性应漏掉它。
    """
    g = _simple_graph()
    g.nodes["orphan"] = DialogueNode(id="orphan", text="我不在任何路径上")
    reach = g.reachable_from_root()
    assert "orphan" not in reach


def test_dialogue_graph_dead_branch_detected() -> None:
    """把 mid 的一个 choice 指向 stuck node（无 choice），该 stuck 不在
    can_reach_terminal 里是错的 —— stuck 本身是 terminal。
    我们构造真正的"非 terminal 且无法到 terminal"：A→B，B→A（形成环），
    这样 A/B 都不在 can_reach_terminal 里。
    """
    g = DialogueGraph(
        id="cyc",
        npc_id="n",
        root_node_id="r",
        nodes={
            "r": DialogueNode(id="r", choices=[Choice(label="go", next_node="A")]),
            "A": DialogueNode(id="A", choices=[Choice(label="loop", next_node="B")]),
            "B": DialogueNode(id="B", choices=[Choice(label="loop", next_node="A")]),
        },
    )
    can = g.nodes_that_can_reach_terminal()
    assert "A" not in can and "B" not in can


# =========================================================================== #
# 2. DialogueAction · 跑通对话 + set flag
# =========================================================================== #


def _scene_with_npc_and_dialogue():
    """场景：玩家 + npc_captain + dialogue graph + 一个 quest 需要 ally flag。"""
    reg = EntityRegistry()
    reg.add(Entity(
        id="p1", kind=EntityKind.PLAYER, name="Player",
        pos=Vec3(x=1.5, y=1.5, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
    ))
    reg.add(Entity(
        id="npc_captain", kind=EntityKind.NPC, name="Captain",
        pos=Vec3(x=3, y=3, z=0),
        bbox=BoundingBox.from_center_size(Vec3.zero(), Vec3(x=1, y=1, z=2)),
        interact_range=5.0,
    ))
    reg.snapshot_all_initials()
    nav = NavGrid(width=10, height=10)
    for row in range(10):
        for col in range(10):
            if row in (0, 9) or col in (0, 9):
                nav.cells[row][col] = False
    return Scene(
        id="dlg_scene", entities=reg,
        geometry=StaticGeometry(),
        nav=nav,
    )


def _dialogue_quest() -> Quest:
    return Quest(
        id="test_q",
        start_step_id="S0",
        end_step_ids=["S1"],
        steps={
            "S0": QuestStep(id="S0", next_steps=["S1"]),
            "S1": QuestStep(
                id="S1",
                requires_flags=["quest.test_q.ally_captain"],
            ),
        },
    )


def test_dialogue_choice_sets_flag_and_advances_quest() -> None:
    sim = make_questsim_sandbox(
        "v1",
        scene=_scene_with_npc_and_dialogue(),
        quest=_dialogue_quest(),
        dialogues={"npc_captain": _simple_graph()},
    )
    sim.reset(seed=1)
    # 走 root→mid→leafA，第二个选项 index=0 "帮你" 设 flag
    r = sim.step(DialogueAction(
        actor="p1", npc_id="npc_captain", choice_path=[0, 0],
    ))
    assert r.outcome.accepted, r.outcome.reason
    # flag 被 set
    assert sim.config.quest.flags.is_true("quest.test_q.ally_captain")
    # S1 的 requires_flags 满足 → 变 active（无 trigger → 不会自动 complete）
    assert sim.config.quest.steps["S1"].status == "active"
    # trace 有 entered 和 choice_made
    assert sim.trace().of_kind("dialogue_node_entered")
    assert sim.trace().of_kind("dialogue_choice_made")


def test_dialogue_invalid_choice_index_rejected() -> None:
    sim = make_questsim_sandbox(
        "v1",
        scene=_scene_with_npc_and_dialogue(),
        dialogues={"npc_captain": _simple_graph()},
    )
    sim.reset(seed=1)
    r = sim.step(DialogueAction(
        actor="p1", npc_id="npc_captain", choice_path=[99],
    ))
    assert r.outcome.accepted is False
    assert "超出" in (r.outcome.reason or "")


def test_dialogue_unknown_npc_rejected() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    r = sim.step(DialogueAction(
        actor="p1", npc_id="nonexistent", choice_path=[0],
    ))
    assert r.outcome.accepted is False


# =========================================================================== #
# 3. SaveAction / LoadAction round-trip
# =========================================================================== #


def test_save_load_pickle_roundtrip_preserves_state() -> None:
    """v1 PickleSaveCodec：save → advance → load 应完整恢复 state。"""
    sim = make_questsim_sandbox(
        "v1",
        scene=_scene_with_npc_and_dialogue(),
        quest=_dialogue_quest(),
        dialogues={"npc_captain": _simple_graph()},
    )
    sim.reset(seed=1)
    # 触发 dialogue 改变 flag
    sim.step(DialogueAction(
        actor="p1", npc_id="npc_captain", choice_path=[0, 0],
    ))
    assert sim.config.quest.flags.is_true("quest.test_q.ally_captain")

    # 存档
    r_save = sim.step(SaveAction(slot="slot_1"))
    assert r_save.outcome.accepted

    # 推进：移动 player + 清 flag（模拟后续状态变化）
    sim.step(MoveToAction(actor="p1", pos=Vec3(x=5, y=5, z=0), mode="teleport"))
    sim.config.quest.flags.values.pop("quest.test_q.ally_captain", None)
    assert not sim.config.quest.flags.is_true("quest.test_q.ally_captain")

    # 读档
    r_load = sim.step(LoadAction(slot="slot_1"))
    assert r_load.outcome.accepted

    # v1 应完整恢复
    assert sim.config.quest.flags.is_true("quest.test_q.ally_captain")
    p1 = sim.entities.get("p1")
    # save 时 p1 还在起点 (1.5, 1.5)
    assert abs(p1.pos.x - 1.5) < 1e-6
    assert abs(p1.pos.y - 1.5) < 1e-6


def test_save_load_empty_slot_rejected() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    r = sim.step(LoadAction(slot="empty_slot"))
    assert r.outcome.accepted is False


def test_save_codec_lossyjson_drops_subtype_info() -> None:
    """单独测 LossyJsonSaveCodec：JSON 化会丢类型。"""
    codec = LossyJsonSaveCodec()
    payload = {
        "entities": {
            "p1": {"pos": (1.5, 1.5, 0), "state": {"looted": True}},
        },
    }
    codec.save("slot_1", payload)
    out = codec.load("slot_1")
    # JSON 把 tuple 转 list（隐式类型漂移）
    assert out is not None
    assert out["entities"]["p1"]["pos"] == [1.5, 1.5, 0]


def test_save_codec_pickle_preserves_tuple() -> None:
    codec = PickleSaveCodec()
    payload = {"pos": (1.0, 2.0, 3.0)}
    codec.save("s", payload)
    out = codec.load("s")
    assert out is not None
    assert out["pos"] == (1.0, 2.0, 3.0)
    assert isinstance(out["pos"], tuple)
