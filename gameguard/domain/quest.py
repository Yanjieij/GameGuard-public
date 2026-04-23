"""Quest · 任务图数据模型。

本模块 = 任务状态机的"规范"（spec），不含运行时行为

数据模型：
  - `Quest`：一个完整任务（若干 QuestStep 构成的有向图 + 汇流点）
  - `QuestStep`：任务中的一步；有 enter 条件 + 进入时执行的 on_enter 操作
    + 指向下游 step 的 branches
  - `Trigger`：step 的激活条件（enter_volume / interact_entity / set_flag / ...）
  - `QuestFlags`：运行期的状态字典（类似 Unity 的 PersistentVariables）

不放的东西：
  - Quest 执行逻辑（在 `gameguard/sandbox/questsim/quest_runtime.py`）
  - Quest 对应的实体（用 EntityRegistry，通过 entity_id 引用）
  - 对话内容（在 dialogue.py）

命名空间约定（避免 flag 命名冲突）

Flag key 强制前缀：
  - `quest.<quest_id>.<key>`：quest 系统设置
  - `dlg.<npc_id>.<key>`：对话系统设置
  - `sys.*`：系统内部（如 `sys.save_slot`）
  - `scene.<key>`：场景层面（触发体激活等）

`QuestFlags.set` 会做前缀校验；非法 key 抛 ValueError。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --- TriggerKind · Step 的激活条件类型 ---
class TriggerKind(str, Enum):
    ENTER_VOLUME = "enter_volume"
    """玩家进入某个 TriggerVolume（scene.triggers 里的）。target=volume_id。"""

    INTERACT_ENTITY = "interact_entity"
    """玩家与某实体交互。target=entity_id。"""

    FLAG_TRUE = "flag_true"
    """某个 flag 变成 True。target=flag_key（完整路径如 "quest.harbor.ally_chosen"）。"""

    DIALOGUE_CHOICE = "dialogue_choice"
    """对话中选择了某个特定选项。target=dialogue_node_id, target_choice=int。"""

    TIMER = "timer"
    """某个 tick / 时间点（暂未实装；预留）。"""

class Trigger(BaseModel):
    """QuestStep 的激活条件。"""

    kind: TriggerKind
    target: str | None = Field(
        None,
        description=(
            "触发对象 id。语义依 kind 而定："
            "enter_volume→volume_id；interact_entity→entity_id；"
            "flag_true→flag_key；dialogue_choice→dialogue_node_id"
        ),
    )
    target_choice: int | None = Field(
        None,
        description="仅 kind=dialogue_choice 时使用：选了哪个选项（0-based index）",
    )

# --- QuestStep · 任务中的一步 ---
class QuestStep(BaseModel):
    """任务中的一步。

    status 生命周期：pending → active → completed。
    - pending：尚未激活（所有前置都没满足）
    - active：当前等待玩家触发 Trigger
    - completed：Trigger 已经发生，on_enter 已执行
    """

    id: str
    name: str = ""
    description: str = ""

    # 激活条件（None = 任务开始时立即激活，用于起始 step）
    trigger: Trigger | None = None

    # 进入该 step 时要 set 的 flag 集合
    on_enter_flags: dict[str, Any] = Field(
        default_factory=dict,
        description="例：{'quest.harbor.ally_chosen': 'captain'}",
    )

    # 前置 flag：必须全为 True 才能激活该 step（额外过滤条件）
    requires_flags: list[str] = Field(default_factory=list)

    # 下游 step id 列表（任务图）
    next_steps: list[str] = Field(
        default_factory=list,
        description="有分支的 step 会有多个 next；end step 为空列表",
    )

    # 运行期状态（非 spec 部分，但放在这里便于 pickle）
    status: Literal["pending", "active", "completed"] = "pending"

    # 触发时间（完成时的 sandbox tick / t，便于 trace 追溯）
    completed_tick: int | None = None
    completed_t: float | None = None

# --- QuestFlags · 带命名空间校验的 flag dict ---
_ALLOWED_PREFIXES = ("quest.", "dlg.", "sys.", "scene.")

class QuestFlags(BaseModel):
    """任务 flag 字典，带命名空间前缀校验。

    前缀：quest.<id>.<key> / dlg.<npc>.<key> / sys.<key> / scene.<key>
    """

    values: dict[str, Any] = Field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        self._assert_valid_key(key)
        self.values[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def is_true(self, key: str) -> bool:
        return bool(self.values.get(key, False))

    def has(self, key: str) -> bool:
        return key in self.values

    def keys(self) -> set[str]:
        return set(self.values.keys())

    @staticmethod
    def _assert_valid_key(key: str) -> None:
        if not any(key.startswith(p) for p in _ALLOWED_PREFIXES):
            raise ValueError(
                f"Flag key {key!r} 必须以 {_ALLOWED_PREFIXES} 之一开头"
            )

# --- Quest · 一个完整任务 ---
class Quest(BaseModel):
    """一个完整任务（含若干 step 的 DAG + 汇流）。"""

    id: str
    name: str = ""
    description: str = ""

    steps: dict[str, QuestStep] = Field(
        default_factory=dict,
        description="按 id 索引的所有 QuestStep",
    )
    start_step_id: str = Field(..., description="任务开始时激活的 step id")
    end_step_ids: list[str] = Field(
        default_factory=list,
        description="完成任一即视为 quest_completed",
    )

    # 运行期 flag（quest.<id>.xxx 空间）
    flags: QuestFlags = Field(default_factory=QuestFlags)

    @field_validator("steps")
    @classmethod
    def _validate_step_ids(cls, v: dict[str, QuestStep]) -> dict[str, QuestStep]:
        """step dict 的 key 必须等于 QuestStep.id。"""
        for k, step in v.items():
            if k != step.id:
                raise ValueError(f"steps dict key {k!r} 与 step.id {step.id!r} 不一致")
        return v

    # -------- 查询 --------

    def get_step(self, step_id: str) -> QuestStep:
        if step_id not in self.steps:
            raise KeyError(f"Quest {self.id!r} 没有 step {step_id!r}")
        return self.steps[step_id]

    def all_step_ids(self) -> list[str]:
        return list(self.steps.keys())

    # -------- reachability 用于 I-01 quest_step_reachable invariant --------

    def reachable_step_ids(self, *, include_flag_gated: bool = True) -> set[str]:
        """从 start_step_id 做 BFS，返回所有可达 step id。

        include_flag_gated=True 时把 requires_flags 视为"潜在可达"（图结构
        意义上可达，至于运行时能否通过另说）；False 时把 requires_flags 严格
        判定（只要 flag 未设即不可达）。

        I-01 默认用 True：我们只检查"图可达性"，不管 flag 是否实际满足。
        """
        visited: set[str] = set()
        queue: list[str] = [self.start_step_id]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            if cur not in self.steps:
                continue
            visited.add(cur)
            step = self.steps[cur]
            if not include_flag_gated and step.requires_flags:
                # 严格模式：检查 flag
                if not all(self.flags.is_true(f) for f in step.requires_flags):
                    continue
            queue.extend(step.next_steps)
        return visited

    # -------- orphan flag 分析（I-03） --------

    def orphan_flags(self) -> set[str]:
        """返回"被 set 但没有任何 step 读取"的 flag 集合。

        I-03 quest_no_orphan_flag invariant 用它判断设计/实现是否一致。
        被读取的定义：(a) step.requires_flags 列表，或 (b) 某 Trigger
        用到该 flag (kind=flag_true)。
        """
        # 本任务里所有被 set 的 flag（静态分析：从所有 step.on_enter_flags 收集）
        set_keys: set[str] = set()
        for step in self.steps.values():
            set_keys |= set(step.on_enter_flags.keys())

        # 所有被读取的 flag
        read_keys: set[str] = set()
        for step in self.steps.values():
            read_keys |= set(step.requires_flags)
            if step.trigger and step.trigger.kind == TriggerKind.FLAG_TRUE:
                if step.trigger.target:
                    read_keys.add(step.trigger.target)

        return set_keys - read_keys

    # -------- reset --------

    def reset_runtime_state(self) -> None:
        """quest_reset 事件的载体：所有 step 回 pending、flag 清空。"""
        for step in self.steps.values():
            step.status = "pending"
            step.completed_tick = None
            step.completed_t = None
        self.flags.values.clear()
