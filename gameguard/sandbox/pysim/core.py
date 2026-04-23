"""PySim 的 tick 引擎，v1 和 v2 共用这份。

干的事：
    - 按固定步长推进时间（默认 0.05s，20 Hz）
    - 在 tick 边界处理排队的 action
    - 维护角色状态、冷却、buff
    - 往 append-only 日志里发事件
    - 提供 snapshot / restore round-trip 供 bug 复现

两点设计：

    - 确定性不可妥协。所有 RNG 都走 self.rng，reset 时统一 seed。SandboxState
      上的 rng_draws 计数器暴露抽取次数，BUG-005（暴击 RNG 没注入 seed）靠
      replay 相等性不变式抓。
    - v1 和 v2 只在插入的 SkillHandler 上有差别；tick 引擎本身共享，避免
      regression 藏在不同的帧循环里。
"""
from __future__ import annotations

import copy
import pickle
import random
from typing import Protocol

from gameguard.domain import (
    Action,
    ActionOutcome,
    BuffBook,
    CastAction,
    Character,
    CharacterState,
    Event,
    EventLog,
    InterruptAction,
    NoopAction,
    SkillBook,
    WaitAction,
)
from gameguard.sandbox.adapter import (
    AdapterInfo,
    GameAdapter,
    SandboxState,
    StepResult,
)


TICK_DT = 0.05  # 秒，20 Hz


class SkillHandler(Protocol):
    """v1 和 v2 技能实现都要满足的契约。"""

    def on_cast_start(self, sim: "PySim", actor: Character, skill_id: str, target_id: str) -> bool:
        """施法开始时调用。返回 True 表示施法被接受。"""

    def on_cast_complete(self, sim: "PySim", actor: Character, skill_id: str, target_id: str) -> None:
        """cast_time 走完时调用。负责结算伤害、挂 buff、进冷却。"""

    def on_interrupt(self, sim: "PySim", actor: Character, skill_id: str) -> None:
        """施法被打断时调用。handler 自己决定要不要退 mp、清理残留。"""

    def on_dot_tick(
        self,
        sim: "PySim",
        target: Character,
        buff_spec_id: str,
        magnitude: float,
        dt: float,
    ) -> None:
        """每个 tick 对角色身上的每个 DoT buff 调一次。

        默认空操作（没有 DoT 逻辑的 handler 直接跳过）。v1/v2 在这里按各自的
        精度策略结算 tick 伤害——BUG-004（浮点累积）就是植在这里的。
        """
        return None


class PySim(GameAdapter):
    """用 Python tick 引擎做后端的 adapter 实现。"""

    def __init__(
        self,
        version: str,
        skill_book: SkillBook,
        buff_book: BuffBook,
        initial_characters: list[Character],
        handler: SkillHandler,
        tick_dt: float = TICK_DT,
    ):
        self._version = version
        self._skills = skill_book
        self._buffs = buff_book
        self._initial_characters = [c.model_copy(deep=True) for c in initial_characters]
        self._handler = handler
        self._tick_dt = tick_dt
        # 下面这些在 reset 时填充：
        self._state: SandboxState = SandboxState(t=0.0, tick=0, seed=0)
        self._log: EventLog = EventLog()
        self._rng: random.Random = random.Random(0)

    # ---- GameAdapter 协议 ------------------------------------------------

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="pysim", version=self._version, deterministic=True)

    @property
    def skills(self) -> SkillBook:
        return self._skills

    @property
    def buffs(self) -> BuffBook:
        return self._buffs

    @property
    def tick_dt(self) -> float:
        return self._tick_dt

    @property
    def log(self) -> EventLog:
        return self._log

    @property
    def rng(self) -> random.Random:
        """注入 seed 的 RNG，每次抽取都把 state.rng_draws 加 1。"""
        # 包一层，让调用方走 random() 时也能被统计到
        return _TrackingRandom(self._rng, self._state)  # type: ignore[return-value]

    def reset(self, seed: int) -> SandboxState:
        self._state = SandboxState(
            t=0.0,
            tick=0,
            seed=seed,
            characters={c.id: c.model_copy(deep=True) for c in self._initial_characters},
            rng_draws=0,
        )
        self._log = EventLog()
        self._rng = random.Random(seed)
        return self._state

    def state(self) -> SandboxState:
        return self._state

    def trace(self) -> EventLog:
        return self._log

    def snapshot(self) -> bytes:
        return pickle.dumps((self._state, self._log, self._rng.getstate()))

    def restore(self, snap: bytes) -> None:
        st, lg, rng_state = pickle.loads(snap)
        self._state = copy.deepcopy(st)
        self._log = copy.deepcopy(lg)
        self._rng = random.Random()
        self._rng.setstate(rng_state)

    def step(self, action: Action) -> StepResult:
        # 每个顶层 action 至少推进一个 tick。wait 可以推进多个；cast 和
        # interrupt 只推进一个，让 agent 保持 tick 级控制力。
        before_event_count = len(self._log)
        outcome = self._dispatch(action)
        return StepResult(
            state=self._state,
            outcome=outcome,
            new_events=len(self._log) - before_event_count,
            done=self._all_dead(),
        )

    # ---- internal dispatch ---------------------------------------------------

    def _dispatch(self, action: Action) -> ActionOutcome:
        if isinstance(action, NoopAction):
            self._advance_ticks(1)
            return ActionOutcome(accepted=True)
        if isinstance(action, WaitAction):
            n = max(1, int(round(action.seconds / self._tick_dt)))
            self._advance_ticks(n)
            return ActionOutcome(accepted=True, events=[f"waited {n * self._tick_dt:.2f}s"])
        if isinstance(action, CastAction):
            return self._handle_cast(action)
        if isinstance(action, InterruptAction):
            return self._handle_interrupt(action)
        return ActionOutcome(accepted=False, reason=f"unknown action type {type(action)!r}")

    def _handle_cast(self, action: CastAction) -> ActionOutcome:
        if action.actor not in self._state.characters:
            return ActionOutcome(accepted=False, reason=f"unknown actor {action.actor!r}")
        actor = self._state.characters[action.actor]
        if not actor.alive:
            return ActionOutcome(accepted=False, reason=f"{actor.id} is not alive")
        if actor.state == CharacterState.CASTING:
            return ActionOutcome(accepted=False, reason=f"{actor.id} is already casting")
        if action.skill not in self._skills.specs:
            return ActionOutcome(accepted=False, reason=f"unknown skill {action.skill!r}")
        spec = self._skills.get(action.skill)
        if actor.cooldown_remaining(action.skill) > 0:
            return ActionOutcome(accepted=False, reason=f"{action.skill} on cooldown")
        if actor.mp < spec.mp_cost:
            return ActionOutcome(accepted=False, reason="insufficient mp")

        accepted = self._handler.on_cast_start(self, actor, action.skill, action.target)
        if not accepted:
            return ActionOutcome(accepted=False, reason="handler rejected cast")

        self._emit(
            kind="cast_start",
            actor=actor.id,
            target=action.target,
            skill=action.skill,
            amount=spec.mp_cost,
        )
        self._advance_ticks(1)
        return ActionOutcome(accepted=True, events=[f"{actor.id} started casting {action.skill}"])

    def _handle_interrupt(self, action: InterruptAction) -> ActionOutcome:
        if action.actor not in self._state.characters:
            return ActionOutcome(accepted=False, reason=f"unknown actor {action.actor!r}")
        actor = self._state.characters[action.actor]
        if actor.state != CharacterState.CASTING or actor.casting_skill is None:
            return ActionOutcome(accepted=False, reason=f"{actor.id} is not casting")
        skill_id = actor.casting_skill
        self._handler.on_interrupt(self, actor, skill_id)
        self._advance_ticks(1)
        return ActionOutcome(accepted=True, events=[f"{actor.id} interrupted while casting {skill_id}"])

    # ---- tick loop -----------------------------------------------------------

    def _advance_ticks(self, n: int) -> None:
        for _ in range(n):
            if self._all_dead():
                return
            self._state.tick += 1
            self._state.t += self._tick_dt
            self._tick_cooldowns()
            self._tick_dots()       # D8：DoT 结算放在 buff 衰减前
            self._tick_buffs()
            self._tick_casts()
            self._emit(kind="tick", amount=self._tick_dt)

    def _tick_dots(self) -> None:
        """对每个角色身上的 DoT 类 buff 调用 handler.on_dot_tick。

        DoT (Damage over Time) 是真实游戏 buff 系统里最容易出 bug 的子系统
        之一——浮点累积、tick 边界、buff 刷新与 DoT 结算的顺序，都是 regression
        重灾区（参考 NetEase Wuji 论文的"数值精度"类 bug）。

        我们把"是否 DoT"的判断放在 BuffSpec.is_dot 上，handler 只管单次结算
        策略，便于 v1/v2 实现差异化逻辑（v1 精确 / v2 故意有累积漂移 → BUG-004）。
        """
        for c in self._state.characters.values():
            if not c.alive:
                continue
            for b in c.buffs:
                spec = self._buffs.specs.get(b.spec_id)
                if spec is None or not spec.is_dot:
                    continue
                self._handler.on_dot_tick(
                    self, c, b.spec_id, b.magnitude, self._tick_dt
                )

    def _tick_cooldowns(self) -> None:
        for c in self._state.characters.values():
            for skill_id in list(c.cooldowns.keys()):
                c.cooldowns[skill_id] = max(0.0, c.cooldowns[skill_id] - self._tick_dt)
                if c.cooldowns[skill_id] == 0.0:
                    # 清掉归零的条目让 state 干净，事件只发一次。
                    self._emit(kind="cooldown_elapsed", actor=c.id, skill=skill_id)
                    del c.cooldowns[skill_id]

    def _tick_buffs(self) -> None:
        for c in self._state.characters.values():
            kept: list = []
            for b in c.buffs:
                if b.tick(self._tick_dt):
                    kept.append(b)
                else:
                    self._emit(kind="buff_expired", actor=c.id, buff=b.spec_id)
            c.buffs = kept

    def _tick_casts(self) -> None:
        for c in self._state.characters.values():
            if c.state != CharacterState.CASTING or c.casting_skill is None:
                continue
            c.cast_remaining -= self._tick_dt
            if c.cast_remaining <= 1e-9:
                skill_id = c.casting_skill
                # 从 cast_start 事件的 metadata 里把 target 找回来。
                target_id = self._lookup_cast_target(c.id, skill_id)
                self._handler.on_cast_complete(
                    self, c, skill_id, target_id or c.id
                )

    def _lookup_cast_target(self, actor_id: str, skill_id: str) -> str | None:
        for e in reversed(self._log.events):
            if e.kind == "cast_start" and e.actor == actor_id and e.skill == skill_id:
                return e.target
        return None

    # ---- helpers for handlers -----------------------------------------------

    def _emit(self, **fields) -> None:
        self._log.append(
            Event(tick=self._state.tick, t=round(self._state.t, 6), **fields)
        )

    def emit(self, **fields) -> None:
        """Public variant for skill handlers."""
        self._emit(**fields)

    def _all_dead(self) -> bool:
        return all(not c.alive for c in self._state.characters.values())


class _TrackingRandom:
    """Wraps random.Random to bump the ``rng_draws`` counter on each draw.

    This exists so the ``replay_deterministic`` invariant can notice if v2
    regresses by drawing from a different RNG (BUG-005).
    """

    def __init__(self, inner: random.Random, state: SandboxState):
        self._inner = inner
        self._state = state

    def random(self) -> float:
        self._state.rng_draws += 1
        return self._inner.random()

    def uniform(self, a: float, b: float) -> float:
        self._state.rng_draws += 1
        return self._inner.uniform(a, b)

    def randint(self, a: int, b: int) -> int:
        self._state.rng_draws += 1
        return self._inner.randint(a, b)
