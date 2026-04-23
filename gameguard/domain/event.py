"""只追加不修改的事件日志。

沙箱每个 tick 都发事件，整段事件序列就是这局游戏的 trace。trace 是 bug
报告的主要证据，也是 TriageAgent 做分析的原料。"只追加 + 纯数据"是刻意
这么设计的——这样 v1 / v2 之间的 replay 和 diff 才好做。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventKind = Literal[
    "cast_start",
    "cast_complete",
    "cast_interrupted",
    "damage_dealt",
    "heal_received",
    "buff_applied",
    "buff_expired",
    "buff_refreshed",
    "cooldown_started",
    "cooldown_elapsed",
    "death",
    "tick",
    "dot_tick",      # D8：每 tick 的 DoT 结算事件（I-09 oracle 用）
    "rng_draw",
    "sandbox_crash",
    # QuestSim 事件族（D13+）
    "move_started",
    "move_completed",
    "move_blocked",
    "quest_step_entered",
    "quest_completed",
    "quest_reset",
    "trigger_fired",
    "dialogue_node_entered",
    "dialogue_choice_made",
    "save_written",
    "load_restored",
    "nav_path_found",
    "nav_stuck",
    "physics_contact",
]


class Event(BaseModel):
    """A single event in the trace.

    `tick` and `t` are both recorded — `tick` is the integer step number
    (useful for strict ordering) and `t` is simulated seconds (useful for
    human-readable traces and invariant expressions).
    """

    tick: int
    t: float
    kind: EventKind
    actor: str | None = None
    target: str | None = None
    skill: str | None = None
    buff: str | None = None
    amount: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class EventLog(BaseModel):
    events: list[Event] = Field(default_factory=list)

    def append(self, e: Event) -> None:
        self.events.append(e)

    def of_kind(self, kind: EventKind) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def between(self, t_start: float, t_end: float) -> list[Event]:
        return [e for e in self.events if t_start <= e.t <= t_end]

    def __len__(self) -> int:
        return len(self.events)
