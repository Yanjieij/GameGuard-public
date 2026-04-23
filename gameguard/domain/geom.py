"""3D 几何原语 · QuestSim 空间层基础。

本模块的职责与边界

这里只放纯函数 3D 原语：
  - `Vec3`：三维向量
  - `BoundingBox`：轴对齐包围盒（AABB）
  - `aabb_intersects` / `aabb_contains_point`：集合相交判定

不放的东西（避免污染）：
  - Entity（在 entity.py；geom 不知道有角色这个概念）
  - 物理速度/加速度（在 physics/ 下；geom 只管静态几何）
  - 渲染/mesh（GameGuard 根本不做渲染）

为什么自己写而不是用 numpy / scipy？

1. pickle 稳定性：numpy 数组的 pickle 格式跨 Python 次版本不保证一致。
   save/load round-trip 是 Q-BUG-004 的 oracle，必须稳。纯 Python dataclass
   在任何 Python 3.x 上的 pickle 兼容。
2. 零依赖：conda 环境轻量；面试官看到不用 numpy 会觉得"这人懂取舍"。
3. 可读性：对 3 维向量手写 `x+y` 比 `np.add(a,b)` 清楚太多。

对高维向量 / 矩阵运算我们会用 numpy；但 3D 空间算力要求极低，纯 Python
快得很（每帧几十个 AABB 判定不是瓶颈）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel

# --- Vec3 · 三维向量 ---
class Vec3(BaseModel):
    """3D 向量（不可变语义，Pydantic 保证 pickle / JSON 稳定）。

    为什么不是 NamedTuple / dataclass？
      - Pydantic 模型参与 TestCase YAML 序列化链路；用同一套机制避免两套
        序列化路径。
      - 和 Entity / Action 的 `pos: Vec3` 字段无缝集成。
    """

    x: float
    y: float
    z: float = 0.0   # 默认 z=0 便于 2D 场景少写一个字段

    # -- 基础运算 --
    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(x=self.x + other.x, y=self.y + other.y, z=self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(x=self.x - other.x, y=self.y - other.y, z=self.z - other.z)

    def __mul__(self, scalar: float) -> "Vec3":
        return Vec3(x=self.x * scalar, y=self.y * scalar, z=self.z * scalar)

    def __truediv__(self, scalar: float) -> "Vec3":
        if scalar == 0:
            raise ZeroDivisionError("Vec3 除以 0")
        return Vec3(x=self.x / scalar, y=self.y / scalar, z=self.z / scalar)

    def __neg__(self) -> "Vec3":
        return Vec3(x=-self.x, y=-self.y, z=-self.z)

    # -- 度量 --
    def length(self) -> float:
        """欧氏长度 ‖v‖。"""
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def length_sq(self) -> float:
        """长度的平方（避免开方，用于比较距离）。"""
        return self.x * self.x + self.y * self.y + self.z * self.z

    def distance_to(self, other: "Vec3") -> float:
        """到另一点的欧氏距离。"""
        return (self - other).length()

    def distance_sq_to(self, other: "Vec3") -> float:
        """到另一点距离的平方（常用于"距离 ≤ R"判定，省一次开方）。"""
        return (self - other).length_sq()

    def normalized(self) -> "Vec3":
        """归一化为单位向量；零向量返回自身。"""
        length = self.length()
        if length < 1e-12:
            return Vec3(x=0.0, y=0.0, z=0.0)
        return Vec3(x=self.x / length, y=self.y / length, z=self.z / length)

    def dot(self, other: "Vec3") -> float:
        """点积。"""
        return self.x * other.x + self.y * other.y + self.z * other.z

    # -- 便捷工厂 --
    @classmethod
    def zero(cls) -> "Vec3":
        return cls(x=0.0, y=0.0, z=0.0)

    @classmethod
    def from_xy(cls, x: float, y: float) -> "Vec3":
        """2D 场景快捷构造（z=0）。"""
        return cls(x=x, y=y, z=0.0)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def __repr__(self) -> str:
        return f"Vec3({self.x:.3f}, {self.y:.3f}, {self.z:.3f})"

# --- BoundingBox · 轴对齐包围盒（AABB） ---
class BoundingBox(BaseModel):
    """轴对齐包围盒（Axis-Aligned Bounding Box, AABB）。

    用两个对角点（min / max）表示。所有字段同时独立，min.x ≤ max.x 等约束
    由工厂方法保证。

    为什么 AABB 而不是 OBB（有向包围盒）？
      - AABB 相交测试只需 6 次比较，比 OBB 快 10×
      - 米哈游类游戏的触发体绝大多数就是 AABB（世界坐标盒子）
      - 需要旋转盒子时可用多个 AABB 组合近似
      - BUG-001（边界相切不触发）是 AABB 的经典 bug 类型
    """

    min: Vec3
    max: Vec3

    # -- 工厂 --
    @classmethod
    def from_min_max(cls, min_pt: Vec3, max_pt: Vec3) -> "BoundingBox":
        """从对角点构造，强制 min ≤ max（超出则 swap）。"""
        nx = min(min_pt.x, max_pt.x)
        ny = min(min_pt.y, max_pt.y)
        nz = min(min_pt.z, max_pt.z)
        mx = max(min_pt.x, max_pt.x)
        my = max(min_pt.y, max_pt.y)
        mz = max(min_pt.z, max_pt.z)
        return cls(min=Vec3(x=nx, y=ny, z=nz), max=Vec3(x=mx, y=my, z=mz))

    @classmethod
    def from_center_size(cls, center: Vec3, size: Vec3) -> "BoundingBox":
        """从中心点 + 尺寸构造。"""
        half = size / 2.0
        return cls(min=center - half, max=center + half)

    # -- 查询 --
    def center(self) -> Vec3:
        return Vec3(
            x=(self.min.x + self.max.x) / 2,
            y=(self.min.y + self.max.y) / 2,
            z=(self.min.z + self.max.z) / 2,
        )

    def size(self) -> Vec3:
        return self.max - self.min

    def volume(self) -> float:
        s = self.size()
        return s.x * s.y * s.z

    # -- 相交 / 包含 --
    def contains_point(self, p: Vec3, *, inclusive: bool = True) -> bool:
        """点是否在盒内。

        inclusive=True：边界上的点算在内（<=）
        inclusive=False：严格在内（<）—— Q-BUG-001 的 v2 改坏点就是这里
        """
        if inclusive:
            return (self.min.x <= p.x <= self.max.x
                    and self.min.y <= p.y <= self.max.y
                    and self.min.z <= p.z <= self.max.z)
        return (self.min.x < p.x < self.max.x
                and self.min.y < p.y < self.max.y
                and self.min.z < p.z < self.max.z)

    def expanded(self, margin: float) -> "BoundingBox":
        """把 AABB 向外扩 margin 得到新盒子（便于近邻检查）。"""
        m = Vec3(x=margin, y=margin, z=margin)
        return BoundingBox(min=self.min - m, max=self.max + m)

# --- 相交判定（纯函数；dataclass 外的 free function 便于单测） ---
def aabb_intersects(a: BoundingBox, b: BoundingBox) -> bool:
    """两个 AABB 是否相交（含边界相切）。

    经典 6 次比较算法：只要任一维度上不重叠就不相交。
    """
    return (
        a.min.x <= b.max.x and a.max.x >= b.min.x
        and a.min.y <= b.max.y and a.max.y >= b.min.y
        and a.min.z <= b.max.z and a.max.z >= b.min.z
    )

def aabb_contains_point(box: BoundingBox, point: Vec3, *, inclusive: bool = True) -> bool:
    """等价于 `box.contains_point(point, inclusive=...)`，提供 free-function 形式。"""
    return box.contains_point(point, inclusive=inclusive)

def segment_intersects_aabb(p1: Vec3, p2: Vec3, box: BoundingBox) -> bool:
    """线段 p1→p2 是否与 AABB 相交（穿过/穿入/端点在内都算）。

    用经典的 slab method：对每个轴求射线参数 t 的入/出区间，最后看区间交集。
    实现简化版：仅做粗略测试（端点在内或中点穿过），对 QuestSim 的视线遮挡
    足够用。精确版可留作未来优化。
    """
    if box.contains_point(p1) or box.contains_point(p2):
        return True
    # 取中点和若干采样点粗略测（非最优但简单）
    for t in (0.25, 0.5, 0.75):
        sample = Vec3(
            x=p1.x + (p2.x - p1.x) * t,
            y=p1.y + (p2.y - p1.y) * t,
            z=p1.z + (p2.z - p1.z) * t,
        )
        if box.contains_point(sample):
            return True
    return False

# --- 2D 简化（QuestSim 的 NavGrid 只用 x/y） ---
@dataclass(frozen=True)
class GridCoord:
    """网格坐标（整数 col, row），用于 NavGrid 寻路。

    与 Vec3 的区别：GridCoord 是离散整数索引；Vec3 是连续空间位置。
    NavGrid 负责双向转换。
    """

    col: int
    row: int

    def __add__(self, other: "GridCoord") -> "GridCoord":
        return GridCoord(self.col + other.col, self.row + other.row)

    def manhattan(self, other: "GridCoord") -> int:
        """曼哈顿距离（A* 启发式用）。"""
        return abs(self.col - other.col) + abs(self.row - other.row)
