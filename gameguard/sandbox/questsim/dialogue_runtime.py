"""Dialogue 运行时 · 推进对话 + 发事件。

DialogueAction 的执行语义

DialogueAction 携带 choice_path（int 列表）。运行时：
  1. 从 root_node_id 开始，依次按 choice_path[i] 进第 i 个节点
  2. 每进一个节点发 `dialogue_node_entered` 事件
  3. 每选一个 choice 发 `dialogue_choice_made` 事件
  4. 选中的 Choice 若有 sets_flag，调 quest.flags.set(...)
  5. 每次 flag 变化后通知 QuestRuntime refresh_pending_steps

choice_path 允许"不走完"：如果 i 超出某 node 的 choices 长度，对话停在那
（本次 action 结束）。这让 LLM 生成的 action 更鲁棒。

和 Quest 的联动

- sets_flag → 可能激活 pending step 的 requires_flags → refresh
- dialogue_choice_made 事件 → QuestRuntime.handle_trigger_event
  (kind=DIALOGUE_CHOICE, target=node_id, target_choice=index)

这样 Quest 可以设 trigger = dialogue_choice({node_id}, {choice index})
精确绑定"选了哪个选项"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gameguard.domain.dialogue import Choice, DialogueGraph
from gameguard.domain.quest import TriggerKind

if TYPE_CHECKING:
    from gameguard.sandbox.questsim.core import QuestSim

@dataclass
class DialogueResult:
    ok: bool
    end_node_id: str | None
    choices_taken: int
    reason: str | None = None

def run_dialogue_path(
    sim: "QuestSim",
    graph: DialogueGraph,
    actor_id: str,
    choice_path: list[int],
) -> DialogueResult:
    """按 choice_path 走 dialogue graph，发事件、set flag。

    返回最终停留的 node id（可能是 terminal、也可能是 path 消耗完时的中间节点）。
    """
    # 首节点：发 entered 事件
    current_id = graph.root_node_id
    if current_id not in graph.nodes:
        return DialogueResult(
            ok=False, end_node_id=None, choices_taken=0,
            reason=f"DialogueGraph {graph.id!r} 无 root {current_id!r}",
        )

    sim.emit(
        kind="dialogue_node_entered",
        actor=actor_id,
        target=graph.npc_id,
        meta={
            "graph_id": graph.id,
            "node_id": current_id,
        },
    )

    choices_taken = 0
    for choice_idx in choice_path:
        node = graph.get_node(current_id)
        if choice_idx < 0 or choice_idx >= len(node.choices):
            return DialogueResult(
                ok=False, end_node_id=current_id, choices_taken=choices_taken,
                reason=(
                    f"choice_path[{choices_taken}]={choice_idx} 超出节点 "
                    f"{current_id!r} 的 choices 范围 [0, {len(node.choices)})"
                ),
            )
        choice: Choice = node.choices[choice_idx]

        # requires_flag 校验
        if choice.requires_flag is not None:
            quest = sim.config.quest
            if quest is None or not quest.flags.is_true(choice.requires_flag):
                return DialogueResult(
                    ok=False, end_node_id=current_id, choices_taken=choices_taken,
                    reason=(
                        f"choice requires_flag={choice.requires_flag!r} 不满足"
                    ),
                )

        # 发 choice_made 事件
        sim.emit(
            kind="dialogue_choice_made",
            actor=actor_id,
            target=graph.npc_id,
            meta={
                "graph_id": graph.id,
                "node_id": current_id,
                "choice_index": choice_idx,
                "choice_label": choice.label,
            },
        )

        # 通知 quest runtime（DIALOGUE_CHOICE kind）
        if sim._quest_runtime is not None:   # noqa: SLF001 跨模块访问
            sim._quest_runtime.handle_trigger_event(
                sim,
                TriggerKind.DIALOGUE_CHOICE,
                target=current_id,
                target_choice=choice_idx,
            )

        # 执行 sets_flag
        if choice.sets_flag is not None:
            key, value = choice.sets_flag
            if sim.config.quest is not None:
                sim.config.quest.flags.set(key, value)
                if sim._quest_runtime is not None:   # noqa: SLF001
                    sim._quest_runtime.refresh_pending_steps(sim)

        choices_taken += 1

        # 跳转 next_node
        if choice.next_node is None:
            # 选择结束对话；不再有 node_entered
            return DialogueResult(
                ok=True, end_node_id=current_id, choices_taken=choices_taken,
            )
        current_id = choice.next_node
        if current_id not in graph.nodes:
            return DialogueResult(
                ok=False, end_node_id=None, choices_taken=choices_taken,
                reason=f"choice 指向不存在的节点 {current_id!r}",
            )
        # 进入新节点
        sim.emit(
            kind="dialogue_node_entered",
            actor=actor_id,
            target=graph.npc_id,
            meta={"graph_id": graph.id, "node_id": current_id},
        )

    # choice_path 走完，但当前节点可能还有 choices（action 可 chain）
    return DialogueResult(
        ok=True, end_node_id=current_id, choices_taken=choices_taken,
    )
