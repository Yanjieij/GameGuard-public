"""QuestSim 等新沙箱的共享底盘。

原本 PySim 自己实现了 GameAdapter 的全部接口。加 QuestSim 时不想让两个沙箱
各写一遍 reset / snapshot / _emit 等样板，就把可复用的部分抽到这里：

  SandboxBase
    ├── 托管 tick / 时间 / seed / RNG
    ├── 托管 append-only EventLog
    ├── 提供 _emit() 事件发射
    └── 提供 snapshot / restore round-trip（pickle）

D12 引入时为了风险控制做了两个决定：

  - 不改 PySim。现有的 pysim/core.py::PySim 保持原样，PySim 的 65 条既有
    测试零风险。
  - SandboxBase 先只给 QuestSim 用。后面稳定了再看要不要把 PySim 也切过来，
    不在本期做。

这种"先建平行实现、不碰旧代码"的重构叫 strangler fig pattern，是生产代码
库的标准做法——可以避免大爆炸式重写引入的 regression。

SandboxBase 有意留给子类的事：

- 不定义 tick 循环。pysim 跑 cooldown/buff/cast，questsim 跑
  move/quest/trigger/physics，差别太大，强行统一会让基类长出一堆 hook。
  子类自己写 _advance_ticks()，调基类 helper 就行。
- 不定义 step() 分发。Action 类型差太多，让子类决定怎么路由。
- 不定义 factory。各沙箱在自己的 factory.py 里装初始状态。

SandboxBase 只保证"每个沙箱都要有的那些 plumbing"一致。
"""
from __future__ import annotations

import copy
import pickle
import random
from typing import Any

from gameguard.domain import EventLog
from gameguard.domain.event import Event
from gameguard.sandbox.adapter import (
    AdapterInfo,
    GameAdapter,
    SandboxState,
)

class SandboxBase(GameAdapter):
    """所有新 sandbox 的共享底盘；实现 GameAdapter 的 plumbing 方法。

    子类需要实现的 抽象方法：
      - `info` (property) —— 返回 AdapterInfo
      - `reset(seed)` —— 重置到初始状态
      - `step(action)` —— 处理单个 action

    子类 不需要重写 的（直接继承用）：
      - `state()` / `trace()` / `snapshot()` / `restore()`
      - `_emit()` / `emit()` 事件发射
      - `rng` 属性（带 seed 的 random.Random）

    子类可选的 hook：
      - `_build_initial_state(seed)` —— 子类在 reset 时调用返回初始 State
      - `_extra_snapshot_fields()` —— 子类额外要 pickle 的字段
      - `_restore_extra_fields(fields)` —— 子类额外字段的恢复
    """

    # ------------------------------------------------------------------ #
    # 构造器（子类通常自己写 __init__ 再调 _init_base()）
    # ------------------------------------------------------------------ #

    def _init_base(
        self,
        *,
        tick_dt: float,
    ) -> None:
        """子类在自己的 __init__ 末尾调用，完成基类字段初始化。

        我们没有在 __init__ 里直接处理是因为：各 sandbox 的构造参数差异大
        （pysim 有 skill_book / buff_book / handler，questsim 有 scene /
        physics backend / quest），强行让 SandboxBase 有统一 __init__ 会
        导致子类 super().__init__(...) 参数列表爆炸。这种"构造后回填"模式
        对 dataclass-heavy Python 代码很常用。
        """
        self._tick_dt: float = tick_dt
        self._state: SandboxState = SandboxState(t=0.0, tick=0, seed=0)
        self._log: EventLog = EventLog()
        self._rng: random.Random = random.Random(0)

    # ------------------------------------------------------------------ #
    # Adapter 接口实现（公共部分）
    # ------------------------------------------------------------------ #

    def state(self) -> SandboxState:
        return self._state

    def trace(self) -> EventLog:
        return self._log

    @property
    def tick_dt(self) -> float:
        """子步长（秒）。SandboxBase 不改变它，但暴露给 handler。"""
        return self._tick_dt

    # ------------------------------------------------------------------ #
    # RNG（子类用这个写入事件时要通过它走）
    # ------------------------------------------------------------------ #

    @property
    def rng(self) -> random.Random:
        """seeded RNG 的访问入口。

        注意：和 pysim/core.py 里的 `_TrackingRandom` 不同，SandboxBase 版
        返回原始 `random.Random`。因为 questsim 用 `state.rng_draws` 计数
        的场景（BUG-005 类）在 pysim 已经专门处理过；questsim 的随机事件
        更多是"分支路径伪随机"而非"战斗暴击"，暂不强制跟踪。

        若将来 questsim 也要抓 determinism bug，可再包一层 TrackingRandom。
        """
        return self._rng

    # ------------------------------------------------------------------ #
    # 事件发射
    # ------------------------------------------------------------------ #

    def _emit(self, **fields: Any) -> None:
        """内部事件发射；把当前 tick/t 自动填入。"""
        self._log.append(
            Event(tick=self._state.tick, t=round(self._state.t, 6), **fields)
        )

    def emit(self, **fields: Any) -> None:
        """供 handler 等外部代码调用的公开版本。"""
        self._emit(**fields)

    # ------------------------------------------------------------------ #
    # Snapshot / Restore（pickle 完整状态，便于一键复现 bug）
    # ------------------------------------------------------------------ #

    def snapshot(self) -> bytes:
        """pickle 全部 runtime state：state + log + rng + 子类额外字段。"""
        payload = {
            "state": self._state,
            "log": self._log,
            "rng": self._rng.getstate(),
            "extra": self._extra_snapshot_fields(),
        }
        return pickle.dumps(payload)

    def restore(self, snap: bytes) -> None:
        """从 snapshot 完整恢复；子类字段通过 `_restore_extra_fields` 回填。"""
        payload = pickle.loads(snap)
        self._state = copy.deepcopy(payload["state"])
        self._log = copy.deepcopy(payload["log"])
        self._rng = random.Random()
        self._rng.setstate(payload["rng"])
        self._restore_extra_fields(payload["extra"])

    # ------------------------------------------------------------------ #
    # 子类可选 hooks（默认无操作；questsim 需要时覆写）
    # ------------------------------------------------------------------ #

    def _extra_snapshot_fields(self) -> dict[str, Any]:
        """子类覆写返回要额外 pickle 的字段（如 quest_state / scene / physics）。

        默认返回空 dict（子类没有额外状态的话）。
        """
        return {}

    def _restore_extra_fields(self, extra: dict[str, Any]) -> None:
        """子类覆写从 snapshot 的 extra dict 恢复自己的字段。

        配对 `_extra_snapshot_fields`；默认无操作。
        """
        del extra  # 默认不用

    # ------------------------------------------------------------------ #
    # 抽象接口（子类必须实现）
    # ------------------------------------------------------------------ #

    @property
    def info(self) -> AdapterInfo:   # type: ignore[override]
        raise NotImplementedError("子类必须实现 info property")

    def reset(self, seed: int) -> SandboxState:   # type: ignore[override]
        raise NotImplementedError("子类必须实现 reset(seed)")

    def step(self, action):   # type: ignore[override]
        raise NotImplementedError("子类必须实现 step(action)")
