"""QuestSim 主循环：任务、3D、寻路、对话、物理的运行时沙箱。

QuestSim 实现 GameAdapter 接口（通过 SandboxBase 共享底盘），是米哈游类游戏
任务 + 3D 交互层的 Python 模拟。

和 PySim 是平行关系：各有各的 domain model（Character vs Entity）、各有各的
action 子集、各有各的事件类型。Runner 和 TriageAgent 看到的都是 GameAdapter，
不关心背后是谁。

D12 启动时这个文件只是空壳，只能 reset / snapshot / restore 和接受
NoopAction / WaitAction，其他一律拒绝。之后 D13 加 MoveToAction、D14 加
InteractAction、D15 加 Dialogue + Save/Load、D16 加物理——每天能跑 pytest，
一天加一点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gameguard.domain import (
    Action,
    ActionOutcome,
    DialogueAction,
    InteractAction,
    LoadAction,
    MoveToAction,
    NoopAction,
    SaveAction,
    Vec3,
    WaitAction,
)
from gameguard.domain.dialogue import DialogueGraph
from gameguard.domain.entity import EntityRegistry
from gameguard.domain.quest import Quest, TriggerKind
from gameguard.domain.scene import Scene
from gameguard.sandbox.adapter import (
    AdapterInfo,
    SandboxState,
    StepResult,
)
from gameguard.sandbox.base import SandboxBase
from gameguard.sandbox.questsim.dialogue_runtime import run_dialogue_path
from gameguard.sandbox.questsim.nav import (
    astar,
    path_to_world_waypoints,
    path_world_length,
)
from gameguard.sandbox.questsim.physics import PhysicsBackend, make_physics_backend
from gameguard.sandbox.questsim.quest_runtime import QuestRuntime
from gameguard.sandbox.questsim.save_codec import (
    PickleSaveCodec,
    SaveCodec,
    apply_load_payload,
    make_save_payload,
)

# tick 频率与 pysim 对齐（20 Hz = 0.05s），保证跨 sandbox 行为一致性与
# "跨 sandbox 测试报告 wall-clock 可比"。
QUESTSIM_TICK_DT = 0.05

@dataclass
class QuestSimConfig:
    """QuestSim 构造配置。

    D13：加入 Scene（含 NavGrid / StaticGeometry / Triggers）。
    D14+ 继续加 quest / dialogue / physics backend 等。
    """

    version: str = "v1"                                 # "v1" / "v2"
    physics_backend: str = "dummy"                      # "dummy" / "pybullet"
    entities: EntityRegistry = field(default_factory=EntityRegistry)
    scene: Scene | None = None
    quest: Quest | None = None
    # 对话图字典 npc_id -> DialogueGraph。一个 NPC 可能有多棵图（按 quest
    # 阶段切换），为简化只用 npc_id 做 key，最后一个 set 会覆盖前一个。
    dialogues: dict[str, DialogueGraph] = field(default_factory=dict)
    # Save/Load codec。v1 默认 PickleSaveCodec；v2 D18 用 LossyJsonSaveCodec。
    save_codec_factory: Any = field(default=PickleSaveCodec)

    # ---- D18 植入 bug 的 version-sensitive 行为 flag ----
    # 默认值 = v1 黄金行为；factory 根据 version 设置 v2 改坏值。

    # Q-BUG-001：trigger volume 边界相切是否触发。
    # v1: True (inclusive 判定，相切也算进入)；v2: False (严格 <，边界不触发)
    trigger_boundary_inclusive: bool = True

    # Q-BUG-003：quest_reset 时是否完整恢复 entity.state（不只 pos）。
    # v1: True；v2: False（只恢复 pos，state dict 里的残留保留下来）
    reset_restores_entity_state: bool = True

# 角色移动速度（单位/秒）。D13 所有角色共用一个值；
# D14+ 可改成 per-entity 字段。
DEFAULT_MOVE_SPEED = 4.0

@dataclass
class _ActiveMove:
    """某个角色正在进行中的 MoveTo 状态（多 tick path-following）。"""

    actor_id: str
    waypoints: list[Vec3]          # 世界坐标 waypoint 列表
    index: int = 0                 # 下一个目标 waypoint 索引
    speed: float = DEFAULT_MOVE_SPEED

    def current_target(self) -> Vec3 | None:
        if self.index >= len(self.waypoints):
            return None
        return self.waypoints[self.index]

class QuestSim(SandboxBase):
    """Quest/3D/对话/寻路/物理 的综合沙箱。

    生命周期：
      1. `__init__(config)` 构造，不启动任何模拟
      2. `reset(seed)` 把所有 Entity 恢复到 initial_*，RNG 注入 seed
      3. `step(action)` 单步推进（具体 action 类型 D13+ 陆续支持）
      4. `snapshot()` / `restore(bytes)` 任意时刻
    """

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    def __init__(self, config: QuestSimConfig) -> None:
        self._config = config
        self._init_base(tick_dt=QUESTSIM_TICK_DT)
        # 运行期只保留"正在进行的 MoveTo"。每个 actor 同时最多一条移动。
        self._active_moves: dict[str, _ActiveMove] = {}
        # Quest 运行时（若配置了 quest）。单 quest per sandbox（D14 够用；
        # 多 quest 可未来抽 list[QuestRuntime]）。
        self._quest_runtime: QuestRuntime | None = (
            QuestRuntime(quest=config.quest) if config.quest is not None else None
        )
        # Save/Load codec 实例（v1 Pickle / v2 LossyJson，由 factory 选择）
        self._save_codec: SaveCodec = config.save_codec_factory()
        # 物理 backend（dummy 默认；pybullet 可选）
        self._physics: PhysicsBackend = make_physics_backend(
            config.physics_backend, tick_dt=QUESTSIM_TICK_DT
        )
        # 用于在 reset 时重建物理世界：把所有 physics_mass>0 的 Entity
        # 登记进 backend。
        self._sync_physics_from_entities()

    # ------------------------------------------------------------------ #
    # GameAdapter 接口
    # ------------------------------------------------------------------ #

    @property
    def info(self) -> AdapterInfo:
        name = f"questsim-{self._config.version}"
        if self._config.physics_backend != "dummy":
            name += f"+{self._config.physics_backend}"
        return AdapterInfo(
            name=name,
            version=self._config.version,
            deterministic=True,
        )

    def reset(self, seed: int) -> SandboxState:
        """把 sandbox 回滚到初始状态。

        步骤：
          1. 重建 SandboxState（时间、tick 归零，seed 注入）
          2. EventLog 清空
          3. RNG 用 seed 重新播种
          4. 所有 Entity reset 到 initial_*（Q-BUG-003 的 oracle 在此）
          5. 清空运行期 move/trigger fired 状态
          6. 如果有 Scene，reset Scene 的 runtime 状态
        """
        import copy
        import random
        self._state = SandboxState(t=0.0, tick=0, seed=seed)
        self._log.events.clear()
        self._rng = random.Random(seed)
        self._active_moves.clear()

        # Q-BUG-003 关键点：v2 漏恢复 state。先保存当前（脏的）state 备用，
        # 再按 v1 方式完整恢复，最后 v2 覆盖回脏 state。
        # 这比拆分 reset_all_to_initial 的逻辑更简洁。
        dirty_states: dict[str, dict] = {}
        if not self._config.reset_restores_entity_state:
            for e in self._config.entities.all():
                if e.kind.value == "npc":
                    dirty_states[e.id] = copy.deepcopy(e.state)

        if self._config.scene is not None:
            self._config.scene.reset_runtime_state()
        else:
            self._config.entities.reset_all_to_initial()

        if not self._config.reset_restores_entity_state:
            # v2 漏恢复：把脏 state 覆盖回去（而 initial 的原版被丢）
            for eid, state in dirty_states.items():
                ent = self._config.entities.get_optional(eid)
                if ent is not None:
                    ent.state = state
        if self._config.quest is not None:
            self._config.quest.reset_runtime_state()
            self._emit(
                kind="quest_reset",
                meta={"quest_id": self._config.quest.id},
            )
            # 重新激活起始 step
            if self._quest_runtime is not None:
                self._quest_runtime.activate_start(self)
        # 重建物理世界
        self._physics.reset()
        self._sync_physics_from_entities()
        return self._state

    # ------------------------------------------------------------------ #
    # Physics 同步（D16）
    # ------------------------------------------------------------------ #

    def _sync_physics_from_entities(self) -> None:
        """把所有 Entity 登记到物理 backend。

        mass==0 的实体注册为 static；mass>0 为 dynamic。玩家 / NPC 通常 mass=0
        （移动由 MoveToAction 逻辑层处理，不走物理）；场景道具箱 mass>0。
        """
        for e in self._config.entities.all():
            wbb = e.world_bbox()
            if e.physics_mass > 0:
                self._physics.add_dynamic_box(e.id, wbb, e.physics_mass)
            else:
                self._physics.add_static_box(e.id, wbb)

    def _tick_physics(self) -> None:
        """每 tick 推进物理 + 同步回 Entity.pos。"""
        self._physics.step(self._tick_dt)
        for e in self._config.entities.all():
            if e.physics_mass <= 0:
                continue    # static 实体不动
            e.pos = self._physics.get_pose(e.id)

    def step(self, action: Action) -> StepResult:
        """单步推进。D13：接受 Noop/Wait/MoveTo。"""
        before_events = len(self._log)

        if isinstance(action, NoopAction):
            self._advance_ticks(1)
            outcome = ActionOutcome(accepted=True)
        elif isinstance(action, WaitAction):
            n = max(1, int(round(action.seconds / self._tick_dt)))
            self._advance_ticks(n)
            outcome = ActionOutcome(
                accepted=True,
                events=[f"waited {n * self._tick_dt:.2f}s"],
            )
        elif isinstance(action, MoveToAction):
            outcome = self._handle_move_to(action)
        elif isinstance(action, InteractAction):
            outcome = self._handle_interact(action)
        elif isinstance(action, DialogueAction):
            outcome = self._handle_dialogue(action)
        elif isinstance(action, SaveAction):
            outcome = self._handle_save(action)
        elif isinstance(action, LoadAction):
            outcome = self._handle_load(action)
        else:
            outcome = ActionOutcome(
                accepted=False,
                reason=(
                    f"QuestSim 尚未支持 action kind={getattr(action, 'kind', '?')!r}；"
                    f"将在 D14+ 加入"
                ),
            )

        return StepResult(
            state=self._state,
            outcome=outcome,
            new_events=len(self._log) - before_events,
            done=False,
        )

    # ------------------------------------------------------------------ #
    # MoveToAction 处理（D13）
    # ------------------------------------------------------------------ #

    def _handle_move_to(self, action: MoveToAction) -> ActionOutcome:
        """处理 MoveToAction。

        teleport 模式：直接把 actor.pos 改成目标，发 move_completed 事件
          （仍占 1 tick，保持 replay 稳定）。
        walk 模式：
          1. 用 NavGrid + A* 算路径
          2. 没路径 → 发 move_blocked + nav_stuck，返回 rejected
          3. 有路径 → 发 nav_path_found + move_started，在 _active_moves 里
             注册；等待 `_tick_movement` 在后续 tick 里推进
          4. 粗略估计总用时（路径长度 / speed），在 step() 调用方的 Wait 下
             自然消化；也可外部继续 step(Wait) 直至 move_completed
        """
        entity = self._config.entities.get_optional(action.actor)
        if entity is None:
            return ActionOutcome(
                accepted=False,
                reason=f"actor {action.actor!r} 不在 EntityRegistry",
            )

        # teleport：直接设位置
        if action.mode == "teleport":
            entity.pos = action.pos.model_copy(deep=True)
            self._emit(
                kind="move_completed",
                actor=action.actor,
                meta={
                    "mode": "teleport",
                    "target": action.pos.as_tuple(),
                },
            )
            self._advance_ticks(1)
            return ActionOutcome(
                accepted=True,
                events=[f"{action.actor} teleported to {action.pos}"],
            )

        # walk：需要 Scene 提供 NavGrid
        scene = self._config.scene
        if scene is None or scene.nav is None:
            return ActionOutcome(
                accepted=False,
                reason="walk 模式需要 Scene 配 NavGrid（当前未配置）",
            )
        nav = scene.nav

        start = nav.world_to_grid(entity.pos)
        goal = nav.world_to_grid(action.pos)

        # 目标超出 grid 边界或被阻挡 → 立即失败
        if not nav.in_bounds(goal) or not nav.is_walkable(goal):
            self._emit(
                kind="move_blocked",
                actor=action.actor,
                meta={"reason": "goal_unwalkable", "goal": action.pos.as_tuple()},
            )
            self._emit(
                kind="nav_stuck",
                actor=action.actor,
                meta={"start": entity.pos.as_tuple(), "goal": action.pos.as_tuple()},
            )
            self._advance_ticks(1)
            return ActionOutcome(
                accepted=False,
                reason=f"目标 {action.pos} 不在可行区内",
            )

        path = astar(nav, start, goal)
        if path is None:
            self._emit(
                kind="move_blocked",
                actor=action.actor,
                meta={"reason": "no_path", "goal": action.pos.as_tuple()},
            )
            self._emit(
                kind="nav_stuck",
                actor=action.actor,
                meta={"start": entity.pos.as_tuple(), "goal": action.pos.as_tuple()},
            )
            self._advance_ticks(1)
            return ActionOutcome(accepted=False, reason="无路径可达")

        waypoints = path_to_world_waypoints(nav, path)
        total_dist = path_world_length(waypoints)

        self._emit(
            kind="nav_path_found",
            actor=action.actor,
            meta={"waypoints": len(waypoints), "distance": round(total_dist, 4)},
        )
        self._emit(
            kind="move_started",
            actor=action.actor,
            meta={
                "from": entity.pos.as_tuple(),
                "to": action.pos.as_tuple(),
                "distance": round(total_dist, 4),
            },
        )

        # 注册 active move，让 _tick_movement 逐 tick 推进
        self._active_moves[action.actor] = _ActiveMove(
            actor_id=action.actor,
            waypoints=waypoints,
        )

        # 估计所需 tick 数（距离 / 速度 / tick_dt），+2 tick 容差避免浮点误差
        est_ticks = int(total_dist / (DEFAULT_MOVE_SPEED * self._tick_dt)) + 2
        self._advance_ticks(est_ticks)

        return ActionOutcome(
            accepted=True,
            events=[f"{action.actor} moving to {action.pos} via {len(waypoints)} wp"],
        )

    # ------------------------------------------------------------------ #
    # InteractAction 处理（D14）
    # ------------------------------------------------------------------ #

    def _handle_interact(self, action: InteractAction) -> ActionOutcome:
        """玩家与实体交互：距离检查 + 发 trigger_fired 给 quest runtime。

        I-10 `interaction_range_consistent` 要求 interact 时 actor 和 entity
        的距离 ≤ entity.interact_range。
        """
        actor = self._config.entities.get_optional(action.actor)
        if actor is None:
            return ActionOutcome(
                accepted=False, reason=f"actor {action.actor!r} 不存在"
            )
        entity = self._config.entities.get_optional(action.entity_id)
        if entity is None:
            return ActionOutcome(
                accepted=False, reason=f"entity {action.entity_id!r} 不存在"
            )

        # 距离检查
        dist = actor.pos.distance_to(entity.pos)
        if dist > entity.interact_range:
            return ActionOutcome(
                accepted=False,
                reason=(
                    f"距离过远：{dist:.2f} > {entity.interact_range} "
                    f"(entity.interact_range)"
                ),
            )

        # 发事件
        self._emit(
            kind="trigger_fired",
            actor=action.actor,
            target=action.entity_id,
            meta={
                "trigger_kind": "interact_entity",
                "distance": round(dist, 4),
            },
        )

        # 通知 quest runtime
        if self._quest_runtime is not None:
            self._quest_runtime.handle_trigger_event(
                self,
                TriggerKind.INTERACT_ENTITY,
                target=action.entity_id,
            )

        self._advance_ticks(1)
        return ActionOutcome(
            accepted=True,
            events=[f"{action.actor} interacted with {action.entity_id}"],
        )

    # ------------------------------------------------------------------ #
    # DialogueAction / Save / Load 处理（D15）
    # ------------------------------------------------------------------ #

    def _handle_dialogue(self, action: DialogueAction) -> ActionOutcome:
        """推进 NPC 对话，按 choice_path 走 DialogueGraph。"""
        graph = self._config.dialogues.get(action.npc_id)
        if graph is None:
            return ActionOutcome(
                accepted=False,
                reason=f"npc {action.npc_id!r} 未配置 DialogueGraph",
            )
        result = run_dialogue_path(
            self, graph, action.actor, action.choice_path
        )
        self._advance_ticks(1)
        if not result.ok:
            return ActionOutcome(accepted=False, reason=result.reason)
        return ActionOutcome(
            accepted=True,
            events=[
                f"dialogue with {action.npc_id} "
                f"took {result.choices_taken} choices, "
                f"ended at {result.end_node_id}"
            ],
        )

    def _handle_save(self, action: SaveAction) -> ActionOutcome:
        payload = make_save_payload(self)
        self._save_codec.save(action.slot, payload)
        self._emit(
            kind="save_written",
            meta={"slot": action.slot, "tick": self._state.tick},
        )
        self._advance_ticks(1)
        return ActionOutcome(
            accepted=True, events=[f"saved to slot {action.slot!r}"]
        )

    def _handle_load(self, action: LoadAction) -> ActionOutcome:
        payload = self._save_codec.load(action.slot)
        if payload is None:
            return ActionOutcome(
                accepted=False, reason=f"slot {action.slot!r} 无存档"
            )
        apply_load_payload(self, payload)
        # 关键：Load 之后重建物理世界，让 dynamic bodies 从 Entity.pos 重开始
        # （否则 backend 里 dynamic 的内部 velocity/pos 还是旧值，下 tick 就会
        # 把刚 load 的 Entity.pos 覆盖掉——这是真实游戏 save/load 的正确语义）
        self._physics.reset()
        self._sync_physics_from_entities()
        self._emit(
            kind="load_restored",
            meta={"slot": action.slot, "tick": self._state.tick},
        )
        self._advance_ticks(1)
        return ActionOutcome(
            accepted=True, events=[f"loaded from slot {action.slot!r}"]
        )

    # ------------------------------------------------------------------ #
    # Tick-level 触发体检测（D14）
    # ------------------------------------------------------------------ #

    def _tick_triggers(self) -> None:
        """每 tick 检查所有 TriggerVolume 是否有 watched entity 进入。

        一旦进入（且 once=True 未 fired）：
          1. 发 trigger_fired 事件
          2. 标记 fired=True
          3. 通知 quest runtime（kind=enter_volume，target=volume.id）

        Q-BUG-001 的 v2 改坏点：`contains_point` 的 inclusive 参数
        在 v2 里被改成 False，导致边界相切的玩家不被视为进入。
        """
        scene = self._config.scene
        if scene is None:
            return
        entities = scene.entities

        for vol in scene.triggers:
            if vol.once and vol.fired:
                continue
            for eid in vol.watch_entity_ids:
                e = entities.get_optional(eid)
                if e is None:
                    continue
                # v1 默认 inclusive=True；v2 handler 覆盖这个方法时可改
                if self._volume_contains(vol.bbox, e.pos):
                    vol.fired = True
                    self._emit(
                        kind="trigger_fired",
                        actor=eid,
                        target=vol.id,
                        meta={
                            "trigger_kind": "enter_volume",
                            "pos": e.pos.as_tuple(),
                        },
                    )
                    if self._quest_runtime is not None:
                        self._quest_runtime.handle_trigger_event(
                            self,
                            TriggerKind.ENTER_VOLUME,
                            target=vol.id,
                        )
                    break  # 同一 volume 每 tick 只发一次

    def _volume_contains(self, bbox, pos) -> bool:
        """判定点是否在触发体 AABB 内。

        v1 默认 inclusive=True（边界也算进入）。
        v2 通过 config.trigger_boundary_inclusive=False 制造 Q-BUG-001。
        """
        return bbox.contains_point(
            pos, inclusive=self._config.trigger_boundary_inclusive
        )

    # ------------------------------------------------------------------ #
    # Tick-level movement 推进
    # ------------------------------------------------------------------ #

    def _tick_movement(self) -> None:
        """把所有 active move 推进一个 tick。完成的从 `_active_moves` 删除。"""
        completed: list[str] = []
        for actor_id, mv in self._active_moves.items():
            entity = self._config.entities.get_optional(actor_id)
            if entity is None:
                completed.append(actor_id)
                continue
            step_budget = mv.speed * self._tick_dt      # 本 tick 能走的距离
            while step_budget > 1e-9:
                target = mv.current_target()
                if target is None:
                    # 所有 waypoint 走完
                    completed.append(actor_id)
                    self._emit(
                        kind="move_completed",
                        actor=actor_id,
                        meta={"final_pos": entity.pos.as_tuple()},
                    )
                    break
                remaining = entity.pos.distance_to(target)
                if remaining <= step_budget + 1e-9:
                    # 能一步到 waypoint
                    entity.pos = target.model_copy(deep=True)
                    step_budget -= remaining
                    mv.index += 1
                else:
                    # 沿方向走 step_budget
                    direction = (target - entity.pos).normalized()
                    entity.pos = entity.pos + direction * step_budget
                    step_budget = 0

        for a in completed:
            self._active_moves.pop(a, None)

    # ------------------------------------------------------------------ #
    # Tick 循环 · QuestSim 自己的主循环（D13+ 会加入 MoveTo 进度推进等）
    # ------------------------------------------------------------------ #

    def _advance_ticks(self, n: int) -> None:
        """推进 n 个 tick。

        D12：什么也不做，只累加 t / tick + 发 tick 事件。
        D13+：会加入 `_tick_movement` / `_tick_quest` / `_tick_physics` 等。
        """
        for _ in range(n):
            self._state.tick += 1
            self._state.t += self._tick_dt
            self._on_tick()
            self._emit(kind="tick", amount=self._tick_dt)

    def _on_tick(self) -> None:
        """单 tick 钩子：movement + 物理 + 触发体检测。"""
        self._tick_movement()
        self._tick_physics()
        self._tick_triggers()

    # ------------------------------------------------------------------ #
    # Snapshot 额外字段（把 EntityRegistry 也塞 pickle）
    # ------------------------------------------------------------------ #

    def _extra_snapshot_fields(self) -> dict[str, Any]:
        return {"entities": self._config.entities}

    def _restore_extra_fields(self, extra: dict[str, Any]) -> None:
        if "entities" in extra:
            self._config.entities = extra["entities"]

    # ------------------------------------------------------------------ #
    # 便捷访问（供 handler / invariant evaluator 用）
    # ------------------------------------------------------------------ #

    @property
    def entities(self) -> EntityRegistry:
        return self._config.entities

    @property
    def config(self) -> QuestSimConfig:
        return self._config

    @property
    def version(self) -> str:
        return self._config.version

    @property
    def physics_backend_name(self) -> str:
        return self._config.physics_backend
