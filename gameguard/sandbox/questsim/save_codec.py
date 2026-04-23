"""Save/Load Codec · 游戏内 SaveAction/LoadAction 的序列化实现。

本模块的职责

SaveAction/LoadAction 是游戏内的存档（玩家在某个点存档，下次从这里
加载），不是 GameGuard 的 snapshot/restore（后者是"整个 sandbox 的 pickle
复现"，用于 bug 报告一键复现）。两者正交：

  snapshot/restore   = 整个 QuestSim + 所有 Entity/Quest/Scene + tick/seed/RNG
  SaveAction/LoadAction = 玩家在 slot="auto" 里保存当前 quest + entity 状态，
                           可在同一 sandbox 运行期内 Load 回来

为什么 v1 用 pickle、v2 用 json.dumps(__dict__)？

真实游戏的存档 bug 最常见的根因：程序员把 `Enum` 或自定义类 `__dict__` 化
成 JSON 时丢失类型信息。Load 回来只剩字符串/dict，Enum 变字符串、Vec3 变
dict，runtime 行为变了但看起来"存上了"。这是 Q-BUG-004 的 v2 改坏路径。

v1（黄金）：pickle 保证类型完整 round-trip。
v2：`json.dumps({k: v.__dict__ for k, v in things})` → Load 回来丢类型。

codec 作为抽象接口，v1/v2 各自实现。默认 v1。
"""
from __future__ import annotations

import copy
import pickle
from typing import Any, Protocol

from gameguard.domain.quest import Quest

class SaveCodec(Protocol):
    """存档编解码器协议。

    一个"槽位字典"：slot name → bytes（或任何可存的 payload）。
    """

    def save(self, slot: str, payload: dict[str, Any]) -> None:
        """把 payload 存进 slot。"""

    def load(self, slot: str) -> dict[str, Any] | None:
        """从 slot 取出 payload；没存过返回 None。"""

# --- v1 黄金：pickle 编解码（完整保留类型信息） ---
class PickleSaveCodec:
    """v1 实现：直接 pickle 整个 payload。类型完整 round-trip。"""

    def __init__(self) -> None:
        self._slots: dict[str, bytes] = {}

    def save(self, slot: str, payload: dict[str, Any]) -> None:
        self._slots[slot] = pickle.dumps(payload)

    def load(self, slot: str) -> dict[str, Any] | None:
        blob = self._slots.get(slot)
        if blob is None:
            return None
        return pickle.loads(blob)

# --- v2 改坏：用 json.dumps(__dict__) 丢失类型（D18 启用） ---
class LossyJsonSaveCodec:
    """v2 Q-BUG-004：用 json 序列化 __dict__，Enum 变字符串、Vec3 变 dict。

    Load 时只还原到 dict，不还原到原 Pydantic 类。这导致后续代码用
    `quest.flags.is_true(...)` 时可能在 dict 上调 Pydantic 方法而崩溃或
    行为错乱。

    为了让 bug 可观测但不立即 crash，我们在 load 时尝试用 Pydantic 重建，
    但对"无法重建的字段"静默退化成 dict。这样 save_load_round_trip
    invariant 能看出 state 字段类型/值的漂移。
    """

    def __init__(self) -> None:
        self._slots: dict[str, bytes] = {}

    def save(self, slot: str, payload: dict[str, Any]) -> None:
        """故意用 json + __dict__ 而不是 pickle。"""
        import json
        shallow = {}
        for k, v in payload.items():
            if hasattr(v, "model_dump"):
                # Pydantic 对象：model_dump 丢 class 信息（Enum 变 str 等）
                shallow[k] = v.model_dump(mode="json")
            elif hasattr(v, "__dict__"):
                shallow[k] = dict(v.__dict__)
            else:
                shallow[k] = v
        self._slots[slot] = json.dumps(shallow, default=str).encode("utf-8")

    def load(self, slot: str) -> dict[str, Any] | None:
        import json
        blob = self._slots.get(slot)
        if blob is None:
            return None
        return json.loads(blob.decode("utf-8"))

# --- save 的 payload 构造（v1/v2 通用） ---
def make_save_payload(sim) -> dict[str, Any]:
    """从 sandbox 状态构造 save payload。v1/v2 都调这个函数，区别在 codec。

    包含：
      - quest flags 完整 dict
      - 所有 step 的 status
      - 所有 entity 的 pos + state
      - 触发体 fired 状态
    """

    payload: dict[str, Any] = {
        "tick": sim.state().tick,
        "t": sim.state().t,
    }
    if sim.config.quest is not None:
        q: Quest = sim.config.quest
        payload["quest_flags"] = copy.deepcopy(q.flags.values)
        payload["quest_step_status"] = {
            sid: step.status for sid, step in q.steps.items()
        }
    payload["entities"] = {}
    for e in sim.entities.all():
        payload["entities"][e.id] = {
            "pos": e.pos.as_tuple(),
            "state": copy.deepcopy(e.state),
        }
    if sim.config.scene is not None:
        payload["triggers_fired"] = {
            t.id: t.fired for t in sim.config.scene.triggers
        }
    return payload

def apply_load_payload(sim, payload: dict[str, Any]) -> None:
    """反向操作：把 load 回来的 payload 应用到 sandbox 状态上。

    注意：v2 LossyJson codec 返回的 payload 里，子字段类型可能丢失
    （Enum 变 str、Vec3 变 dict）。这里我们尽量容错 ——
    pos 会从 list/tuple/dict 恢复成 Vec3，state 直接覆盖。
    """
    from gameguard.domain.geom import Vec3

    if sim.config.quest is not None and "quest_flags" in payload:
        sim.config.quest.flags.values = dict(payload["quest_flags"])
    if sim.config.quest is not None and "quest_step_status" in payload:
        for sid, status in payload["quest_step_status"].items():
            if sid in sim.config.quest.steps:
                sim.config.quest.steps[sid].status = status
    if "entities" in payload:
        for eid, e_data in payload["entities"].items():
            ent = sim.entities.get_optional(eid)
            if ent is None:
                continue
            pos = e_data.get("pos")
            if pos is not None:
                # pos 可能是 tuple/list（pickle 保真）或被 json 变成 list；
                # 也可能 v2 codec 返回了 Vec3.__dict__ 的 dict
                if isinstance(pos, (list, tuple)) and len(pos) == 3:
                    ent.pos = Vec3(x=pos[0], y=pos[1], z=pos[2])
                elif isinstance(pos, dict):
                    ent.pos = Vec3(
                        x=pos.get("x", 0), y=pos.get("y", 0), z=pos.get("z", 0)
                    )
            if "state" in e_data:
                ent.state = dict(e_data["state"])
    if sim.config.scene is not None and "triggers_fired" in payload:
        for t in sim.config.scene.triggers:
            if t.id in payload["triggers_fired"]:
                t.fired = bool(payload["triggers_fired"][t.id])
