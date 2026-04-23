"""Scene · 场景容器、触发体、导航网格、静态几何。

四大子概念

1. `StaticGeometry`：不可移动的障碍物列表（墙、家具、地形边界）。寻路 +
   视线检查要绕开它们。

2. `TriggerVolume`：看不见的逻辑触发区域。玩家 pos 进入 volume 的 AABB
   就发射 `trigger_fired` 事件，通常用来推进 quest step。Q-BUG-001
   的 v2 改坏点就在这里：边界相切判定不同。

3. `NavGrid`：寻路用的 2D 网格（x/y，z 忽略）。cells[row][col] 是 True
   代表可通行，False 代表阻挡。用纯 list-of-list 保证 pickle 稳定。

4. `Scene`：以上三者 + `EntityRegistry` 的顶层容器。一份 Scene 描述一张
   完整地图（港口场景、仓库场景等）。

为什么 NavGrid 是 list[list[bool]] 而不是 numpy？

- pickle 跨 Python 版本稳定：numpy 数组的 pickle 格式在 Py 3.11 → 3.12
  之间曾经破坏过，save/load round-trip 不可信。list[list[bool]] 是纯
  Python 对象，完全稳定。Q-BUG-004 的 oracle 依赖这个。
- 性能足够：典型场景 100×100 格子 = 10000 bool，Python list 查询
  ns 级，远不是瓶颈。
- LLM 友好：YAML / JSON 序列化时 list[list[bool]] 就是个二维数组，
  LLM 能直接读懂 Scene 文档。

NavGrid 坐标系

  - world 坐标：Vec3(x, y, z)，z 被忽略
  - grid 坐标：GridCoord(col, row)，row 对应 -y 方向（标准 2D 数组视角）
  - 转换：`world_to_grid(world_x, world_y)` / `grid_to_world(col, row)`
  - cell_size：每个 grid 单元的世界尺寸（通常 1.0 米）
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from gameguard.domain.entity import EntityRegistry
from gameguard.domain.geom import BoundingBox, GridCoord, Vec3

# --- TriggerVolume · 看不见的逻辑触发区域 ---
class TriggerVolume(BaseModel):
    """玩家 / 指定实体进入 AABB 时触发的逻辑区域。

    例子：`enter_volume harbor_gate` → 推进 quest step1。
    """

    id: str = Field(..., description="触发体 id，例 'harbor_gate'")
    bbox: BoundingBox = Field(..., description="世界坐标 AABB（不是本地！）")
    # 谁进入才触发
    watch_entity_ids: list[str] = Field(
        default_factory=lambda: ["p1"],
        description="只对这些 entity 的 pos 做检测；默认只看玩家",
    )
    # 触发钩子目标（例：推进哪个 quest step）
    target_quest_id: str | None = None
    target_step_id: str | None = None
    # 是否只触发一次
    once: bool = True
    # 运行期状态
    fired: bool = Field(False, description="若 once=True 且已触发，后续 tick 跳过")

# --- StaticGeometry · 不可移动障碍物列表 ---
class StaticGeometry(BaseModel):
    """一组不可移动的 AABB 障碍物（墙 / 家具）。

    寻路算法需要它们来阻挡（NavGrid.cells 会根据这些 AABB 自动生成）；
    视线 / 射线测试也会用到。
    """

    obstacles: list[BoundingBox] = Field(default_factory=list)

    def blocks_point(self, p: Vec3) -> bool:
        """某点是否被任一障碍物遮挡（在障碍物 AABB 内）。"""
        return any(obs.contains_point(p) for obs in self.obstacles)

# --- NavGrid · 2D 网格寻路 ---
class NavGrid(BaseModel):
    """2D 网格（walkable / blocked）用于 A* 寻路。

    cells[row][col]：True = 可通行，False = 阻挡。row 和 col 都从 0 起。

    约定：
      - world 坐标系 x 对应 col 正方向，y 对应 row 负方向（常见 2D 系统）
      - origin_world = Vec3(x=0, y=0, z=0) 对应 cell (col=0, row=height-1)
        即 grid 的左下角；这让 y 增加 = row 减少，符合"上北下南"直觉
      - cell_size：每 grid 单元对应的世界长度（米）
    """

    width: int = Field(..., gt=0)       # 列数 (cols)
    height: int = Field(..., gt=0)      # 行数 (rows)
    cell_size: float = Field(1.0, gt=0)
    origin: Vec3 = Field(default_factory=Vec3.zero, description="grid 左下角的世界坐标")

    # cells[row][col] = walkable?
    cells: list[list[bool]] = Field(
        default_factory=list,
        description="二维 bool 数组；True 可通行。使用时确保 len==height，内层 len==width",
    )

    # -------- 构造 --------

    def model_post_init(self, __context: Any) -> None:
        """如果 cells 为空，初始化为全 walkable。"""
        if not self.cells:
            self.cells = [[True] * self.width for _ in range(self.height)]
        # 宽高一致性校验
        if len(self.cells) != self.height:
            raise ValueError(
                f"NavGrid.cells 行数 {len(self.cells)} != height {self.height}"
            )
        for row_idx, row in enumerate(self.cells):
            if len(row) != self.width:
                raise ValueError(
                    f"NavGrid.cells 第 {row_idx} 行宽度 {len(row)} != width {self.width}"
                )

    # -------- 坐标转换 --------

    def world_to_grid(self, pos: Vec3) -> GridCoord:
        """世界坐标 → grid 坐标。超出边界仍返回最接近的 grid 坐标（由调用方 clamp）。"""
        col = int((pos.x - self.origin.x) / self.cell_size)
        # y 增加 → row 减少。row 0 是最上一行（grid 顶部），对应世界 y 最大的地方
        row_from_bottom = int((pos.y - self.origin.y) / self.cell_size)
        row = self.height - 1 - row_from_bottom
        return GridCoord(col=col, row=row)

    def grid_to_world(self, coord: GridCoord, *, center: bool = True) -> Vec3:
        """grid 坐标 → 世界坐标；center=True 返回单元中心。"""
        offset = 0.5 if center else 0.0
        x = self.origin.x + (coord.col + offset) * self.cell_size
        row_from_bottom = self.height - 1 - coord.row
        y = self.origin.y + (row_from_bottom + offset) * self.cell_size
        return Vec3(x=x, y=y, z=self.origin.z)

    # -------- 查询 --------

    def in_bounds(self, coord: GridCoord) -> bool:
        return 0 <= coord.col < self.width and 0 <= coord.row < self.height

    def is_walkable(self, coord: GridCoord) -> bool:
        """越界或 blocked 都算不可通行。"""
        if not self.in_bounds(coord):
            return False
        return self.cells[coord.row][coord.col]

    def set_blocked(self, coord: GridCoord, blocked: bool = True) -> None:
        if not self.in_bounds(coord):
            raise IndexError(f"GridCoord {coord} 越界")
        self.cells[coord.row][coord.col] = not blocked

    def block_aabb(self, aabb: BoundingBox) -> None:
        """把 AABB 覆盖的所有 grid 单元标为 blocked。

        场景加载时把 StaticGeometry 的每个 obstacle 转成 grid 块的工具。
        """
        min_c = self.world_to_grid(aabb.min)
        max_c = self.world_to_grid(aabb.max)
        for row in range(
            max(0, min(min_c.row, max_c.row)),
            min(self.height, max(min_c.row, max_c.row) + 1),
        ):
            for col in range(
                max(0, min(min_c.col, max_c.col)),
                min(self.width, max(min_c.col, max_c.col) + 1),
            ):
                self.cells[row][col] = False

    def walkable_neighbors(self, coord: GridCoord) -> list[GridCoord]:
        """4-邻居（不走对角线，保证曼哈顿启发式 admissible）。"""
        candidates = [
            GridCoord(col=coord.col + 1, row=coord.row),
            GridCoord(col=coord.col - 1, row=coord.row),
            GridCoord(col=coord.col, row=coord.row + 1),
            GridCoord(col=coord.col, row=coord.row - 1),
        ]
        return [c for c in candidates if self.is_walkable(c)]

    def count_walkable(self) -> int:
        return sum(1 for row in self.cells for c in row if c)

# --- Scene · 顶层场景容器 ---
class Scene(BaseModel):
    """一个完整场景的集合：实体 + 触发体 + 静态几何 + 导航网格。"""

    id: str = Field(..., description="场景 id，例 'harbor'")
    name: str = ""
    entities: EntityRegistry = Field(default_factory=EntityRegistry)
    triggers: list[TriggerVolume] = Field(default_factory=list)
    geometry: StaticGeometry = Field(default_factory=StaticGeometry)
    nav: NavGrid | None = None

    # -------- 查询 --------

    def get_trigger(self, trigger_id: str) -> TriggerVolume | None:
        for t in self.triggers:
            if t.id == trigger_id:
                return t
        return None

    def build_nav_from_geometry(self) -> None:
        """根据 StaticGeometry 自动把障碍物标到 NavGrid 上。

        典型用法：场景加载器先读 obstacles，再调此方法把他们翻译到 nav。
        需要 self.nav 已被初始化。
        """
        if self.nav is None:
            return
        for obs in self.geometry.obstacles:
            self.nav.block_aabb(obs)

    # -------- reset 支持 --------

    def reset_runtime_state(self) -> None:
        """把运行期状态回滚（Entity 的 pos/state、Trigger.fired）。

        sandbox reset 时调用。不改 NavGrid / Geometry / Trigger 定义本身。
        """
        self.entities.reset_all_to_initial()
        for t in self.triggers:
            t.fired = False
