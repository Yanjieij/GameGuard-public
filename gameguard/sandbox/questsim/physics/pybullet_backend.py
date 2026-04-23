"""PyBulletBackend · 基于 pybullet 的物理实现（可选依赖）。

使用方式

```
pip install gameguard[physics]    # 安装 pybullet 可选依赖
gameguard run --plan ... --sandbox questsim:v1+pybullet
```

未安装 pybullet 时构造本类会抛清晰的 ImportError 指向安装命令。

为什么 determinism 是精心调的

pybullet 默认多线程、浮点 SSE 优化可能导致跨机器 1e-6 级漂移。我们做：
  - `setPhysicsEngineParameter(fixedTimeStep=tick_dt)`：步长与 sandbox 对齐
  - `numSolverIterations=10`：固定迭代数
  - `numThreads=1`：单线程
  - `deterministicOverlappingPairs=1`：碰撞对排序确定
  - `reset()` 完全重建 world（不复用 client）

即便如此，位置仍可能有 1e-4 量级漂移；`replay_deterministic` invariant
对 pybullet 关卡要放宽 tolerance。这是真实 pybullet 项目的共识。
"""
from __future__ import annotations

import pickle

from gameguard.domain.geom import BoundingBox, Vec3
from gameguard.sandbox.questsim.physics.base import PhysicsBackend

class PyBulletBackend(PhysicsBackend):
    """基于 pybullet 的物理实现。

    构造时会 try import pybullet；未装则抛详细的 ImportError。
    """

    def __init__(self, tick_dt: float = 0.05) -> None:
        try:
            import pybullet as p
        except ImportError as e:
            raise ImportError(
                "pybullet 未安装。请运行：\n"
                "  pip install gameguard[physics]\n"
                "或直接：\n"
                "  pip install pybullet>=3.2\n"
                "如果只想跑纯逻辑关卡，用 --sandbox questsim:v1（不加 +pybullet）"
            ) from e
        self._p = p
        self._tick_dt = tick_dt
        self._body_ids: dict[str, int] = {}   # our id → pybullet body id
        self._bbox_specs: dict[str, tuple[BoundingBox, float]] = {}  # 供 reset 重建
        self._client: int | None = None
        self._init_world()

    def _init_world(self) -> None:
        """创建 DIRECT 模式 client（无渲染），设 determinism 参数。"""
        self._client = self._p.connect(self._p.DIRECT)
        # 关 GUI / 图形相关（DIRECT 模式本就没图形，但更稳）
        self._p.setGravity(0, 0, -9.81, physicsClientId=self._client)
        self._p.setPhysicsEngineParameter(
            fixedTimeStep=self._tick_dt,
            numSolverIterations=10,
            deterministicOverlappingPairs=1,
            numThreads=1,
            physicsClientId=self._client,
        )

    # ---- 注册 ----

    def add_static_box(self, body_id: str, bbox: BoundingBox) -> None:
        self._add_box(body_id, bbox, mass=0.0)

    def add_dynamic_box(
        self, body_id: str, bbox: BoundingBox, mass: float
    ) -> None:
        if mass <= 0:
            raise ValueError("dynamic body mass 必须 > 0")
        self._add_box(body_id, bbox, mass=mass)

    def _add_box(self, body_id: str, bbox: BoundingBox, mass: float) -> None:
        p = self._p
        half = (bbox.size() / 2.0).as_tuple()
        center = bbox.center().as_tuple()
        col_shape = p.createCollisionShape(
            shapeType=p.GEOM_BOX, halfExtents=half, physicsClientId=self._client
        )
        body_idx = p.createMultiBody(
            baseMass=mass,
            baseCollisionShapeIndex=col_shape,
            basePosition=center,
            physicsClientId=self._client,
        )
        self._body_ids[body_id] = body_idx
        self._bbox_specs[body_id] = (bbox, mass)

    # ---- 施力 ----

    def apply_force(self, body_id: str, force: Vec3) -> None:
        if body_id not in self._body_ids:
            return
        idx = self._body_ids[body_id]
        self._p.applyExternalForce(
            objectUniqueId=idx,
            linkIndex=-1,
            forceObj=list(force.as_tuple()),
            posObj=[0, 0, 0],
            flags=self._p.LINK_FRAME,
            physicsClientId=self._client,
        )

    def apply_impulse(self, body_id: str, impulse: Vec3) -> None:
        """pybullet 没有直接的 impulse，用 mass*dv 等效（近似）。"""
        if body_id not in self._body_ids:
            return
        _bbox, mass = self._bbox_specs[body_id]
        if mass <= 0:
            return
        dv = impulse / mass
        # 读当前速度，加上 dv
        lin, ang = self._p.getBaseVelocity(
            self._body_ids[body_id], physicsClientId=self._client
        )
        new_lin = (lin[0] + dv.x, lin[1] + dv.y, lin[2] + dv.z)
        self._p.resetBaseVelocity(
            self._body_ids[body_id],
            linearVelocity=new_lin,
            angularVelocity=ang,
            physicsClientId=self._client,
        )

    # ---- 查询 ----

    def get_pose(self, body_id: str) -> Vec3:
        if body_id not in self._body_ids:
            return Vec3.zero()
        pos, _ori = self._p.getBasePositionAndOrientation(
            self._body_ids[body_id], physicsClientId=self._client
        )
        return Vec3(x=pos[0], y=pos[1], z=pos[2])

    def raycast(self, from_pos: Vec3, to_pos: Vec3) -> str | None:
        """pybullet 的 rayTest；返回击中的 body_id（我们 map 回自己的 id）。"""
        result = self._p.rayTest(
            from_pos.as_tuple(), to_pos.as_tuple(), physicsClientId=self._client
        )
        if not result:
            return None
        hit_idx = result[0][0]
        if hit_idx < 0:
            return None
        # 反查
        for bid, idx in self._body_ids.items():
            if idx == hit_idx:
                return bid
        return None

    # ---- 推进 ----

    def step(self, dt: float) -> None:
        # 我们固定步长；忽略传入的 dt（但也不违背语义——外部调用方保证 dt==tick_dt）
        self._p.stepSimulation(physicsClientId=self._client)

    # ---- 生命周期 ----

    def reset(self) -> None:
        """完全重建 world。保留 body spec 以便后续重新 add。"""
        # 先保存 specs
        specs = dict(self._bbox_specs)
        # 断开 client
        if self._client is not None:
            self._p.disconnect(physicsClientId=self._client)
        self._body_ids.clear()
        self._bbox_specs.clear()
        self._init_world()
        # 重新添加（由调用方 re-add，这里仅重置 client）
        # 但保留 specs 供外部参考
        self._bbox_specs = specs

    def snapshot(self) -> bytes:
        """序列化所有 body 的 pos + velocity 供 restore。"""
        state: dict[str, dict] = {}
        for bid, idx in self._body_ids.items():
            pos, ori = self._p.getBasePositionAndOrientation(idx, physicsClientId=self._client)
            lin, ang = self._p.getBaseVelocity(idx, physicsClientId=self._client)
            bbox, mass = self._bbox_specs[bid]
            state[bid] = {
                "pos": pos, "ori": ori, "lin": lin, "ang": ang,
                "bbox_min": bbox.min.as_tuple(),
                "bbox_max": bbox.max.as_tuple(),
                "mass": mass,
            }
        return pickle.dumps(state)

    def restore(self, blob: bytes) -> None:
        state = pickle.loads(blob)
        # 重建 world
        if self._client is not None:
            self._p.disconnect(physicsClientId=self._client)
        self._body_ids.clear()
        self._bbox_specs.clear()
        self._init_world()
        # 重新添加所有 body 并设速度
        for bid, s in state.items():
            bbox = BoundingBox(
                min=Vec3(x=s["bbox_min"][0], y=s["bbox_min"][1], z=s["bbox_min"][2]),
                max=Vec3(x=s["bbox_max"][0], y=s["bbox_max"][1], z=s["bbox_max"][2]),
            )
            self._add_box(bid, bbox, mass=s["mass"])
            idx = self._body_ids[bid]
            self._p.resetBasePositionAndOrientation(
                idx, s["pos"], s["ori"], physicsClientId=self._client
            )
            self._p.resetBaseVelocity(
                idx, linearVelocity=s["lin"], angularVelocity=s["ang"],
                physicsClientId=self._client,
            )

    def __del__(self) -> None:
        try:
            if self._client is not None:
                self._p.disconnect(physicsClientId=self._client)
        except Exception:
            # __del__ 里不要 raise
            pass
