"""Quest 运行时 · 处理 step 激活、trigger 触发、flag set。

本模块的位置

`quest.py` 定义任务的 静态数据结构。本文件定义 动态行为：
  - `QuestRuntime`：把 Quest 挂在 sandbox 上
  - `handle_trigger_event(kind, payload)`：sandbox 发事件时调这个把 trigger 关联到 step
  - `try_advance_to_step(step_id)`：触发条件满足后推进到 next step

为什么和 Quest spec 分开？
  - Quest spec 是纯数据（进 YAML、进 plan），可被 LLM/策划直接修改
  - 运行时有 side effect（发事件、改状态），要和 sandbox 绑定

Trigger 的两种激活方式

1. 事件驱动（默认）：sandbox 其它模块发事件（trigger_fired / 玩家 interact /
   flag 改变）时，QuestRuntime 检查当前 active steps 里是否有匹配的 Trigger。

2. 轮询（仅用于 TIMER kind，当前未启用）：每 tick 查询 active steps 是否
   超过时间门槛。

为保持简单，D14 只实现事件驱动。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gameguard.domain.quest import Quest, QuestStep, Trigger, TriggerKind

if TYPE_CHECKING:
    from gameguard.sandbox.questsim.core import QuestSim

@dataclass
class QuestRuntime:
    """把一个 Quest 绑定到 sandbox；提供 handle_* 接口供 sandbox 分发事件。

    用法：
        rt = QuestRuntime(quest=..., sim=sim)
        rt.activate_start()              # 任务开始时调用
        rt.handle_trigger_event(...)     # sandbox 每发相关事件时调用
    """

    quest: Quest

    # ------------------------------------------------------------------ #
    # 初始化 · 激活起始 step
    # ------------------------------------------------------------------ #

    def activate_start(self, sim: "QuestSim") -> None:
        """任务开始时调用：把 start_step 置 active；若它没有 trigger，立刻完成。"""
        start = self.quest.get_step(self.quest.start_step_id)
        self._set_active(sim, start)
        # 如果 start step 没有 trigger（spec 里 trigger=None），立即完成
        if start.trigger is None:
            self._complete_step(sim, start)

    # ------------------------------------------------------------------ #
    # 事件驱动的 trigger 匹配
    # ------------------------------------------------------------------ #

    def handle_trigger_event(
        self,
        sim: "QuestSim",
        kind: TriggerKind,
        *,
        target: str | None = None,
        target_choice: int | None = None,
    ) -> None:
        """sandbox 发生某个事件时调用本方法；自动激活所有匹配的 active step。

        注意：不按 active step 列表顺序匹配 —— 所有匹配的都触发。
        真实游戏里一个事件可能同时推进多个并行 quest。
        """
        for step in list(self.quest.steps.values()):
            if step.status != "active" or step.trigger is None:
                continue
            if self._trigger_matches(step.trigger, kind, target, target_choice):
                self._complete_step(sim, step)

    @staticmethod
    def _trigger_matches(
        trig: Trigger,
        kind: TriggerKind,
        target: str | None,
        target_choice: int | None,
    ) -> bool:
        if trig.kind != kind:
            return False
        if trig.target is not None and trig.target != target:
            return False
        if (
            trig.target_choice is not None
            and trig.target_choice != target_choice
        ):
            return False
        return True

    # ------------------------------------------------------------------ #
    # Step 状态转换
    # ------------------------------------------------------------------ #

    def _set_active(self, sim: "QuestSim", step: QuestStep) -> None:
        """把一个 step 从 pending 变 active；检查 requires_flags。"""
        if step.status != "pending":
            return
        # requires_flags：若设置了但 flag 未满足，step 保持 pending
        if step.requires_flags:
            if not all(self.quest.flags.is_true(f) for f in step.requires_flags):
                return
        step.status = "active"

    def _complete_step(self, sim: "QuestSim", step: QuestStep) -> None:
        """完成一个 step：设 flag、发事件、推进到 next_steps。

        这是 Q-BUG-002 的关键路径：v2 handler 会在这里漏 set 某个关键
        flag，导致下游 step 的 requires_flags 永不满足。
        """
        if step.status != "active":
            return
        step.status = "completed"
        step.completed_tick = sim.state().tick
        step.completed_t = sim.state().t

        # 发事件
        sim.emit(
            kind="quest_step_entered",
            meta={
                "quest_id": self.quest.id,
                "step_id": step.id,
                "step_name": step.name,
            },
        )

        # set 该 step 的 on_enter_flags（Q-BUG-002 的 v2 会漏某些 flag）
        for fk, fv in step.on_enter_flags.items():
            self.quest.flags.set(fk, fv)

        # 推进到下游 steps
        for next_id in step.next_steps:
            if next_id in self.quest.steps:
                nxt = self.quest.steps[next_id]
                self._set_active(sim, nxt)

        # 是否到 end
        if step.id in self.quest.end_step_ids:
            sim.emit(
                kind="quest_completed",
                meta={"quest_id": self.quest.id, "end_step": step.id},
            )

    # ------------------------------------------------------------------ #
    # 外部事件：flag 变化时再扫一次 pending steps
    # ------------------------------------------------------------------ #

    def refresh_pending_steps(self, sim: "QuestSim") -> None:
        """flag 变化后调用：把 requires_flags 新满足的 pending step 提升为 active。"""
        for step in list(self.quest.steps.values()):
            if step.status == "pending":
                self._set_active(sim, step)
        # flag_true kind trigger 是显式读 flag 的；这里一并检查
        for step in list(self.quest.steps.values()):
            if (
                step.status == "active"
                and step.trigger is not None
                and step.trigger.kind == TriggerKind.FLAG_TRUE
            ):
                if (
                    step.trigger.target
                    and self.quest.flags.is_true(step.trigger.target)
                ):
                    self._complete_step(sim, step)
