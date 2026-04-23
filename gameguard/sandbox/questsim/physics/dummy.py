"""DummyBackend · 零依赖的"玩具"物理引擎。

设计

- 静态物体：只记录 bbox，用于碰撞检测（阻挡动态物体移动）
- 动态物体：只有 pos + velocity；每 tick 积分 `pos += velocity * dt`；
  velocity 受累计力与重力影响（简化：`v += (g + F/m) * dt`）
- 碰撞：AABB vs AABB 阻挡；推箱子只支持沿 x/y 方向（z 方向上有重力
  但不模拟叠放）
- 确定性：纯 Python 算术，完全 deterministic；pickle 即完整状态

不做的：旋转 / 碰撞反弹 / 复杂形状 / 高速穿透保护。

对"推箱子到压力板"这个核心谜题足够用；高阶物理用 pybullet backend。
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass, field

from gameguard.domain.geom import BoundingBox, Vec3
from gameguard.sandbox.questsim.physics.base import PhysicsBackend

# 重力（m/s²）；沿 -z 方向
GRAVITY = Vec3(x=0, y=0, z=-9.81)
# 摩擦衰减系数（每 tick 速度乘这个；模拟滑动摩擦）
FRICTION = 0.9

@dataclass
class _DummyBody:
    """Dummy backend 里的单个物体（静态 or 动态）。"""

    body_id: str
    bbox_local: BoundingBox    # 局部 bbox（以 pos 为中心）
    pos: Vec3
    mass: float                 # 0 = static；>0 = dynamic
    velocity: Vec3 = field(default_factory=Vec3.zero)
    accumulated_force: Vec3 = field(default_factory=Vec3.zero)

    @property
    def is_static(self) -> bool:
        return self.mass <= 0.0

    def world_bbox(self) -> BoundingBox:
        return BoundingBox(
            min=self.bbox_local.min + self.pos,
            max=self.bbox_local.max + self.pos,
        )

class DummyBackend(PhysicsBackend):
    """零依赖的简化物理后端。"""

    def __init__(self) -> None:
        self._bodies: dict[str, _DummyBody] = {}

    # ---- 注册 ----

    def add_static_box(self, body_id: str, bbox: BoundingBox) -> None:
        self._bodies[body_id] = _DummyBody(
            body_id=body_id,
            bbox_local=BoundingBox(min=bbox.min - bbox.center(), max=bbox.max - bbox.center()),
            pos=bbox.center(),
            mass=0.0,
        )

    def add_dynamic_box(
        self, body_id: str, bbox: BoundingBox, mass: float
    ) -> None:
        if mass <= 0:
            raise ValueError(f"dynamic body 的 mass 必须 > 0，收到 {mass}")
        self._bodies[body_id] = _DummyBody(
            body_id=body_id,
            bbox_local=BoundingBox(min=bbox.min - bbox.center(), max=bbox.max - bbox.center()),
            pos=bbox.center(),
            mass=mass,
        )

    # ---- 施力 ----

    def apply_force(self, body_id: str, force: Vec3) -> None:
        b = self._bodies.get(body_id)
        if b is None or b.is_static:
            return
        b.accumulated_force = b.accumulated_force + force

    def apply_impulse(self, body_id: str, impulse: Vec3) -> None:
        b = self._bodies.get(body_id)
        if b is None or b.is_static:
            return
        b.velocity = b.velocity + impulse / b.mass

    # ---- 查询 ----

    def get_pose(self, body_id: str) -> Vec3:
        b = self._bodies.get(body_id)
        return b.pos.model_copy(deep=True) if b is not None else Vec3.zero()

    def raycast(self, from_pos: Vec3, to_pos: Vec3) -> str | None:
        """朴素实现：检查线段是否穿过任一 body 的 world bbox。取第一个击中。"""
        from gameguard.domain.geom import segment_intersects_aabb
        for bid, b in self._bodies.items():
            if segment_intersects_aabb(from_pos, to_pos, b.world_bbox()):
                return bid
        return None

    # ---- 推进 ----

    def step(self, dt: float) -> None:
        """一步物理积分。静态物体跳过；动态物体：加速度 → 速度 → 位置。"""
        if dt <= 0:
            return
        # 1. 计算所有动态物体的新速度与候选位置
        pending: list[tuple[_DummyBody, Vec3, Vec3]] = []   # (body, new_vel, new_pos)
        for b in self._bodies.values():
            if b.is_static:
                continue
            # a = g + F/m
            accel = GRAVITY + b.accumulated_force / b.mass
            new_vel = (b.velocity + accel * dt) * FRICTION
            new_pos = b.pos + new_vel * dt
            pending.append((b, new_vel, new_pos))

        # 2. 碰撞检测：候选位置若与任何静态物体相交，则撤回位置（保持原位），
        # 速度沿碰撞方向归零（简化：直接 velocity=0）
        for b, new_vel, new_pos in pending:
            new_bbox = BoundingBox(
                min=b.bbox_local.min + new_pos,
                max=b.bbox_local.max + new_pos,
            )
            blocked = False
            for other in self._bodies.values():
                if other.body_id == b.body_id:
                    continue
                if other.is_static:
                    if _aabb_overlap(new_bbox, other.world_bbox()):
                        blocked = True
                        break
            if blocked:
                b.velocity = Vec3.zero()
            else:
                b.velocity = new_vel
                b.pos = new_pos
            # 清空 force（force 是"单 tick"概念，不累加）
            b.accumulated_force = Vec3.zero()

    # ---- 生命周期 ----

    def reset(self) -> None:
        self._bodies.clear()

    def snapshot(self) -> bytes:
        return pickle.dumps(self._bodies)

    def restore(self, blob: bytes) -> None:
        self._bodies = pickle.loads(blob)

def _aabb_overlap(a: BoundingBox, b: BoundingBox) -> bool:
    """两个 AABB 是否严格相交（不含边界相切）。

    静态阻挡判定用严格相交（接触算贴边不推开）。
    """
    return (
        a.min.x < b.max.x and a.max.x > b.min.x
        and a.min.y < b.max.y and a.max.y > b.min.y
        and a.min.z < b.max.z and a.max.z > b.min.z
    )
