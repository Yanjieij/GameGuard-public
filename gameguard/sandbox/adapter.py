"""把 Agent 和引擎隔开的 Adapter 抽象。

这里的 adapter 模式和真实 Unity QA 系统的做法一致：agent 层对一个小而强类型
的协议讲话，具体是翻译成 Python 函数调用（PySim）、gRPC 打到 Unity headless
（UnityAdapter）、还是预录轨迹回放（mock），由各自的 adapter 决定。

这个 ABC 要保持稳定——测试代码和 Agent 工具都依赖它的形状。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from gameguard.domain import Action, ActionOutcome, Character, EventLog


class SandboxState(BaseModel):
    """对外可观测的沙箱状态快照。

    只暴露 QA 需要断言的那部分，物理和渲染不进来。
    """

    t: float
    tick: int
    seed: int
    characters: dict[str, Character] = Field(default_factory=dict)
    rng_draws: int = 0  # 每次沙箱向 RNG 要数就 +1


class StepResult(BaseModel):
    state: SandboxState
    outcome: ActionOutcome
    new_events: int  # 这一步追加了多少条事件
    done: bool = False


class AdapterInfo(BaseModel):
    """记在 trace 和 bug 单里的元数据。"""

    name: str
    version: str        # 例如 "pysim-v1"、"pysim-v2"、"unity-0.1-mock"
    deterministic: bool


class GameAdapter(ABC):
    """沙箱的抽象接口。

    契约：
        - reset(seed) 必须确定性：同一个 seed 必须产出同一份 trace
        - step(action) 必须把事件追加到 event log；其他地方不得发事件
        - snapshot() / restore() 必须 round-trip 不漂移
    """

    @property
    @abstractmethod
    def info(self) -> AdapterInfo: ...

    @abstractmethod
    def reset(self, seed: int) -> SandboxState: ...

    @abstractmethod
    def step(self, action: Action) -> StepResult: ...

    @abstractmethod
    def state(self) -> SandboxState: ...

    @abstractmethod
    def trace(self) -> EventLog: ...

    @abstractmethod
    def snapshot(self) -> bytes: ...

    @abstractmethod
    def restore(self, snap: bytes) -> None: ...

    # 下面这个便利方法不算核心契约，但到处在用。
    def run(self, actions: list[Action]) -> StepResult:
        """顺次执行 actions，返回最后一步的 StepResult。"""
        result: StepResult | None = None
        for a in actions:
            result = self.step(a)
            if result.done:
                break
        if result is None:
            return StepResult(
                state=self.state(),
                outcome=ActionOutcome(accepted=True),
                new_events=0,
                done=False,
            )
        return result
