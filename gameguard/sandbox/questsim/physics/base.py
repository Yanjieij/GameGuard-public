"""PhysicsBackend · 抽象接口（Protocol）。

设计目标

QuestSim 的物理需求很窄：
  - 推箱子（dynamic box 被力推到压力板）
  - 可能的掉落（动态实体受重力）
  - 压力板触发（box 接触 board 时 quest 推进）

不需要：刚体关节、软体、流体、布娃娃。所以接口小（7 个方法），且**能被
纯 Python "dummy" backend 轻松实现**。pybullet 只是"更认真的 dummy"。

Backend 契约

- `step(dt)`：推进一个物理子步（通常就是 sandbox 的 tick_dt）
- `add_static_box(id, bbox)` / `add_dynamic_box(id, bbox, mass)`：注册物体
- `apply_force(id, force)` / `apply_impulse(id, impulse)`：施力（dynamic 才有效）
- `get_pose(id) -> (Vec3, rotation)`：查询当前位姿（rotation 暂用 euler 或忽略）
- `raycast(from, to) -> hit_id | None`：视线 / 拾取
- `reset()`：清空世界并重新从初始状态开始
- `snapshot() -> bytes` / `restore(bytes)`：序列化世界完整状态
"""
from __future__ import annotations

from typing import Protocol

from gameguard.domain.geom import BoundingBox, Vec3

class PhysicsBackend(Protocol):
    """所有物理后端必须实现的接口。"""

    def step(self, dt: float) -> None:
        """推进 dt 秒的物理模拟。"""
        ...

    def add_static_box(self, body_id: str, bbox: BoundingBox) -> None:
        """注册静态刚体（不受力、不动）。对应 Unity 的无 Rigidbody Collider。"""
        ...

    def add_dynamic_box(
        self, body_id: str, bbox: BoundingBox, mass: float
    ) -> None:
        """注册动态刚体（受力、会动）。mass > 0。"""
        ...

    def apply_force(self, body_id: str, force: Vec3) -> None:
        """施加持续力（单位: N）。pybullet 里等同 applyExternalForce。"""
        ...

    def apply_impulse(self, body_id: str, impulse: Vec3) -> None:
        """施加瞬时冲量（单位: N·s）。"""
        ...

    def get_pose(self, body_id: str) -> Vec3:
        """返回当前位置。姿态 / 欧拉角暂不返回（简化）。"""
        ...

    def raycast(self, from_pos: Vec3, to_pos: Vec3) -> str | None:
        """从 from 到 to 射线检测；返回击中的 body_id 或 None。"""
        ...

    def reset(self) -> None:
        """清空世界，准备重新添加物体。"""
        ...

    def snapshot(self) -> bytes:
        """完整序列化物理世界（用于 sandbox snapshot 的一部分）。"""
        ...

    def restore(self, blob: bytes) -> None:
        """从 snapshot 恢复完整物理世界。"""
        ...
