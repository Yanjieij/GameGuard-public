"""A* 寻路 + 强连通分量（SCC）分析。

本模块的职责

1. **A*（A-Star）寻路**：在 `NavGrid` 上找从起点 grid 坐标到终点 grid 坐标
   的最短路径。
2. 连通性分析：用于 `no_stuck_positions` invariant —— 检查所有可达 cell
   是否在同一个连通分量中（无"孤岛"）。
3. path simplification：把 grid path 平滑成世界坐标 waypoint 列表。

为什么不用 networkx / pyrecastdetour？

- networkx 太重（1MB+ 依赖）；我们只用到 BFS/A*/SCC，纯 Python 百行搞定
- pyrecastdetour C 扩展装起来麻烦（需编译）；Python A* 在 100x100 格子上
  < 1ms

保持零依赖是为了：
  - CI 跑得快
  - 跨平台无坑（米哈游内部测试集群常常有奇怪的 Linux 发行版）
  - 代码可读（面试讲算法 vs 讲库调用，前者更加分）
"""
from __future__ import annotations

import heapq
from collections.abc import Iterable

from gameguard.domain.geom import GridCoord, Vec3
from gameguard.domain.scene import NavGrid

# --- A* 寻路 ---
def astar(
    nav: NavGrid,
    start: GridCoord,
    goal: GridCoord,
    *,
    max_explored: int = 50_000,
) -> list[GridCoord] | None:
    """在 NavGrid 上跑 A*，返回从 start 到 goal 的路径（含两端）；找不到返回 None。

    参数：
      - max_explored：最多扩展多少 node 后放弃，防止超大地图死循环。
                      100×100 格子最坏约 10k 扩展，50k 留足余量。

    算法要点：
      - 启发式用曼哈顿距离（4-邻居 grid 最优、admissible）
      - open set 用 heapq（二叉堆）：Python 标准库自带，O(log n) 入/出
      - closed set 用 set：O(1) 判重
      - came_from dict 回溯路径
    """
    if not nav.is_walkable(start) or not nav.is_walkable(goal):
        return None
    if start == goal:
        return [start]

    # heap item: (f_score, tie_breaker, coord)
    # tie_breaker 用递增整数避免 coord 之间比较时 Pydantic 不支持 < 比较
    open_heap: list[tuple[int, int, GridCoord]] = []
    heapq.heappush(open_heap, (start.manhattan(goal), 0, start))
    counter = 1

    came_from: dict[GridCoord, GridCoord] = {}
    g_score: dict[GridCoord, int] = {start: 0}
    closed: set[GridCoord] = set()
    explored = 0

    while open_heap:
        explored += 1
        if explored > max_explored:
            return None   # 地图过大或搜索策略失败
        _, _, current = heapq.heappop(open_heap)
        if current == goal:
            return _reconstruct_path(came_from, current)
        if current in closed:
            continue
        closed.add(current)

        for neighbor in nav.walkable_neighbors(current):
            if neighbor in closed:
                continue
            tentative_g = g_score[current] + 1   # 4-邻居每步 cost=1
            if tentative_g < g_score.get(neighbor, float("inf")):
                g_score[neighbor] = tentative_g
                came_from[neighbor] = current
                f_score = tentative_g + neighbor.manhattan(goal)
                heapq.heappush(open_heap, (f_score, counter, neighbor))
                counter += 1

    return None

def _reconstruct_path(came_from: dict, end: GridCoord) -> list[GridCoord]:
    """从 came_from dict 回溯得到完整路径（start → end）。"""
    path = [end]
    while end in came_from:
        end = came_from[end]
        path.append(end)
    path.reverse()
    return path

def path_to_world_waypoints(nav: NavGrid, grid_path: list[GridCoord]) -> list[Vec3]:
    """把 grid 路径转成世界坐标 waypoint（每个 cell 的中心）。

    返回列表长度 = len(grid_path)。第一个点是 start 的 cell 中心，最后一个
    是 goal 的 cell 中心。MoveToAction 执行时会逐段平移。
    """
    return [nav.grid_to_world(c, center=True) for c in grid_path]

# --- 强连通分量 · 用于 no_stuck_positions invariant（I-08 Quest 变体） ---
def walkable_components(nav: NavGrid) -> list[set[GridCoord]]:
    """把所有 walkable cell 按 4-邻接分成若干连通分量；返回 list[set]。

    用于 `no_stuck_positions` invariant：理想情况下所有可行区域只有一个
    连通分量；若出现多个，说明 navmesh 被误切成孤岛（Q-BUG-005）。

    算法：对每个未访问的 walkable cell 跑 BFS 收集其连通分量。
    """
    visited: set[GridCoord] = set()
    components: list[set[GridCoord]] = []

    for row in range(nav.height):
        for col in range(nav.width):
            c = GridCoord(col=col, row=row)
            if c in visited or not nav.is_walkable(c):
                continue
            # 以 c 为起点 BFS
            comp = _bfs_component(nav, c)
            visited |= comp
            components.append(comp)

    return components

def _bfs_component(nav: NavGrid, start: GridCoord) -> set[GridCoord]:
    """从 start 开始 BFS，返回所有 4-邻接可达的 walkable cell 集合。"""
    comp: set[GridCoord] = {start}
    queue: list[GridCoord] = [start]
    while queue:
        cur = queue.pop(0)
        for nb in nav.walkable_neighbors(cur):
            if nb not in comp:
                comp.add(nb)
                queue.append(nb)
    return comp

def reachable_from(nav: NavGrid, start: GridCoord) -> set[GridCoord]:
    """start 所在的连通分量（实际就是一次 BFS）。"""
    if not nav.is_walkable(start):
        return set()
    return _bfs_component(nav, start)

# --- 路径长度估算 · 用于 move 耗时计算 ---
def path_world_length(waypoints: Iterable[Vec3]) -> float:
    """世界坐标 waypoint 列表的总欧氏长度。MoveToAction 用它估计完成时间。"""
    wps = list(waypoints)
    if len(wps) < 2:
        return 0.0
    total = 0.0
    for i in range(len(wps) - 1):
        total += wps[i].distance_to(wps[i + 1])
    return total
