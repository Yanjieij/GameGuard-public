"""D13 meta-tests · NavGrid / A* / MoveToAction / Scene。"""
from __future__ import annotations


from gameguard.domain import (
    BoundingBox,
    EntityRegistry,
    GridCoord,
    MoveToAction,
    Vec3,
)
from gameguard.domain.scene import NavGrid, Scene
from gameguard.sandbox.questsim import make_questsim_sandbox
from gameguard.sandbox.questsim.nav import (
    astar,
    path_to_world_waypoints,
    reachable_from,
    walkable_components,
)


# =========================================================================== #
# 1. NavGrid 坐标转换
# =========================================================================== #


def test_nav_grid_world_to_grid_roundtrip() -> None:
    """grid → world → grid 应回到同一坐标（对齐网格中心的输入）。"""
    nav = NavGrid(width=10, height=10, cell_size=1.0, origin=Vec3.zero())
    c = GridCoord(col=3, row=5)
    w = nav.grid_to_world(c, center=True)
    back = nav.world_to_grid(w)
    assert back == c


def test_nav_grid_in_bounds_and_walkable() -> None:
    nav = NavGrid(width=5, height=5)
    assert nav.in_bounds(GridCoord(0, 0))
    assert not nav.in_bounds(GridCoord(5, 0))
    assert nav.is_walkable(GridCoord(2, 2))
    nav.set_blocked(GridCoord(2, 2), True)
    assert not nav.is_walkable(GridCoord(2, 2))


def test_nav_grid_count_walkable_initially_all_true() -> None:
    nav = NavGrid(width=5, height=5)
    assert nav.count_walkable() == 25


def test_nav_grid_block_aabb_blocks_covered_cells() -> None:
    """block_aabb 把 AABB 覆盖的所有 cell 标为 blocked。"""
    nav = NavGrid(width=10, height=10, cell_size=1.0, origin=Vec3.zero())
    aabb = BoundingBox.from_min_max(Vec3(x=2, y=2, z=0), Vec3(x=4, y=4, z=0))
    nav.block_aabb(aabb)
    # 原本 100 walkable；AABB 覆盖大致是 3x3=9 个 cell
    remaining = nav.count_walkable()
    assert remaining < 100 and remaining > 80


# =========================================================================== #
# 2. A* 寻路
# =========================================================================== #


def test_astar_straight_path_no_obstacles() -> None:
    nav = NavGrid(width=10, height=10)
    path = astar(nav, GridCoord(0, 0), GridCoord(5, 0))
    assert path is not None
    assert path[0] == GridCoord(0, 0)
    assert path[-1] == GridCoord(5, 0)
    assert len(path) == 6   # 起点 + 5 步


def test_astar_goal_equals_start() -> None:
    nav = NavGrid(width=5, height=5)
    path = astar(nav, GridCoord(2, 2), GridCoord(2, 2))
    assert path == [GridCoord(2, 2)]


def test_astar_blocked_goal_returns_none() -> None:
    nav = NavGrid(width=5, height=5)
    nav.set_blocked(GridCoord(3, 3), True)
    assert astar(nav, GridCoord(0, 0), GridCoord(3, 3)) is None


def test_astar_finds_path_around_wall() -> None:
    """一堵竖墙，A* 应找到绕过路径。"""
    nav = NavGrid(width=10, height=10)
    # 在 col=5 处竖墙，但留出顶行可通
    for row in range(1, 10):
        nav.set_blocked(GridCoord(5, row), True)
    path = astar(nav, GridCoord(0, 5), GridCoord(9, 5))
    assert path is not None
    assert path[0] == GridCoord(0, 5)
    assert path[-1] == GridCoord(9, 5)
    # 路径中必有穿过顶行 row=0
    assert any(c.row == 0 for c in path)


def test_astar_no_path_when_fully_blocked() -> None:
    nav = NavGrid(width=10, height=10)
    for row in range(10):
        nav.set_blocked(GridCoord(5, row), True)
    assert astar(nav, GridCoord(0, 5), GridCoord(9, 5)) is None


# =========================================================================== #
# 3. 连通性 · walkable_components
# =========================================================================== #


def test_walkable_components_single_when_fully_open() -> None:
    nav = NavGrid(width=5, height=5)
    comps = walkable_components(nav)
    assert len(comps) == 1
    assert len(comps[0]) == 25


def test_walkable_components_split_by_wall() -> None:
    """竖墙把 grid 分成两个 connected components。"""
    nav = NavGrid(width=10, height=10)
    for row in range(10):
        nav.set_blocked(GridCoord(5, row), True)
    comps = walkable_components(nav)
    assert len(comps) == 2
    sizes = sorted(len(c) for c in comps)
    # 左半 5 列 × 10 行 = 50；右半 4 列（col 6-9）× 10 行 = 40
    assert sizes == [40, 50]


def test_reachable_from_returns_single_component() -> None:
    nav = NavGrid(width=5, height=5)
    reach = reachable_from(nav, GridCoord(2, 2))
    assert len(reach) == 25


# =========================================================================== #
# 4. MoveToAction · teleport 模式
# =========================================================================== #


def test_move_teleport_changes_pos_in_one_step() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    r = sim.step(MoveToAction(
        actor="p1", pos=Vec3(x=5, y=5, z=0), mode="teleport"
    ))
    assert r.outcome.accepted
    p1 = sim.entities.get("p1")
    assert p1.pos == Vec3(x=5, y=5, z=0)
    # 应发了 move_completed 事件
    assert len(sim.trace().of_kind("move_completed")) == 1


def test_move_teleport_unknown_actor_rejected() -> None:
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    r = sim.step(MoveToAction(
        actor="ghost", pos=Vec3(x=1, y=1, z=0), mode="teleport"
    ))
    assert r.outcome.accepted is False


# =========================================================================== #
# 5. MoveToAction · walk 模式（A* 寻路 + 逐 tick 推进）
# =========================================================================== #


def test_move_walk_to_reachable_target() -> None:
    """默认 scene 是 20×20 的空房间，p1 在 (1.5,1.5)。
    走到 (5.5,1.5) = col=5,row=18 的中心（距离 4 单位）。
    """
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    r = sim.step(MoveToAction(
        actor="p1", pos=Vec3(x=5.5, y=1.5, z=0), mode="walk"
    ))
    assert r.outcome.accepted, r.outcome.reason
    # 寻路事件
    assert len(sim.trace().of_kind("nav_path_found")) == 1
    assert len(sim.trace().of_kind("move_started")) == 1
    assert len(sim.trace().of_kind("move_completed")) == 1
    # 到达目标（允许极小浮点误差）
    p1 = sim.entities.get("p1")
    assert abs(p1.pos.x - 5.5) < 1e-6
    assert abs(p1.pos.y - 1.5) < 1e-6


def test_move_walk_blocked_goal_returns_nav_stuck() -> None:
    """走到墙里应 reject + 发 nav_stuck 事件。"""
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    # 默认 scene 的 (0, 0) 是边界墙，设置为 blocked 单元
    r = sim.step(MoveToAction(
        actor="p1", pos=Vec3(x=0.5, y=0.5, z=0), mode="walk"
    ))
    assert r.outcome.accepted is False
    assert len(sim.trace().of_kind("nav_stuck")) == 1
    assert len(sim.trace().of_kind("move_blocked")) == 1


def test_move_walk_no_path_returns_nav_stuck() -> None:
    """把整条中线封死，A* 找不到路径。"""
    sim = make_questsim_sandbox("v1")
    sim.reset(seed=1)
    nav = sim.config.scene.nav
    # 竖墙从 col=10 从上到下（把地图分成两半）
    for row in range(nav.height):
        nav.set_blocked(GridCoord(10, row), True)
    r = sim.step(MoveToAction(
        actor="p1", pos=Vec3(x=15.5, y=1.5, z=0), mode="walk"
    ))
    assert r.outcome.accepted is False
    assert len(sim.trace().of_kind("nav_stuck")) == 1


# =========================================================================== #
# 6. 路径 waypoint 长度计算
# =========================================================================== #


def test_path_to_world_waypoints_and_length() -> None:
    nav = NavGrid(width=5, height=5, cell_size=2.0, origin=Vec3.zero())
    path = astar(nav, GridCoord(0, 0), GridCoord(3, 0))
    assert path is not None
    waypoints = path_to_world_waypoints(nav, path)
    # 4 个 cell 距离（每 cell 2.0 米）
    assert len(waypoints) == 4
    # 中心到中心相邻距离 = cell_size = 2.0
    assert abs(waypoints[1].distance_to(waypoints[0]) - 2.0) < 1e-9


# =========================================================================== #
# 7. Scene · reset 回滚 trigger/Entity state
# =========================================================================== #


def test_scene_reset_runtime_state_resets_trigger_fired() -> None:
    from gameguard.domain.scene import TriggerVolume
    scene = Scene(
        id="test",
        entities=EntityRegistry(),
        triggers=[
            TriggerVolume(
                id="g1",
                bbox=BoundingBox.from_min_max(Vec3.zero(), Vec3(x=1, y=1, z=1)),
            ),
        ],
    )
    scene.triggers[0].fired = True
    scene.reset_runtime_state()
    assert scene.triggers[0].fired is False
