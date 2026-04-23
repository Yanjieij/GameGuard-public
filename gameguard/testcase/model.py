"""TestCase / TestPlan / TestResult —— 测试用例的数据结构。

这一层在整个 GameGuard 中的位置

    策划文档 (md)              ← 人写的自然语言 + 数据表
       │
       ▼
    SpecBundle / InvariantBundle   ← DesignDocAgent 的产物（纯数据）
       │
       ▼
    TestCase / TestPlan            ← 本文件！TestGenAgent 的产物
       │
       ▼
    TestResult / TestSuiteResult   ← ExecutorAgent/Runner 跑完后的产物
       │
       ▼
    Bug 报告 (Jira-compatible)     ← TriageAgent 的产物

因此本文件是 纯数据契约：不包含任何 I/O、不包含 LLM 调用、不包含
游戏逻辑。所有字段必须是 Pydantic 可序列化的，这样：
  1) TestGenAgent 让 LLM 直接以 JSON 格式产出 TestCase
  2) 我们能把 TestCase 以 YAML 保存到版本库（和代码一起 review）
  3) 回归模式能把两次跑的 Result 序列化后 diff

为什么 TestCase 要 "数据驱动" 而不是 "Python 函数驱动"？

传统 pytest 写法中，一个测试 = 一个 Python 函数。那样很难：
  - 让 LLM 凭空生成（生成 Python 源码不如生成 JSON 稳）
  - 进行回归 diff（函数签名一样但行为不同，难定位）
  - 做 "测试用例本身的 review"（策划/QA 看不懂 Python）

所以我们让测试用例 = 数据：一串动作 + 一串断言。这个模式在业内称为
"data-driven testing"（TestRail、Xray、Jepsen、Hypothesis 都是这条路），
米哈游类游戏团队的 QA 通常也用 Excel/YAML 组织回归用例。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from gameguard.domain.action import Action
from gameguard.domain.invariant import Invariant, InvariantResult

# --------------------------------------------------------------------------- #
# 测试生成策略枚举
# --------------------------------------------------------------------------- #

class TestStrategy(str, Enum):
    """测试用例的来源策略。

    三种策略对应计划文档里 "TestGenAgent 的三层生成方法"：
      - CONTRACT     : 契约式。直接把策划文档里的不变式映射为断言。
                       覆盖 "设计规范里明说了" 的属性。
      - EXPLORATORY  : 探索式。LLM 模拟真实玩家的行为序列。
                       覆盖 "玩家可能会这样玩" 的属性。
      - PROPERTY     : 属性式 / 对抗式。随机动作序列 + 不变式全量检查。
                       覆盖 "我们没想到玩家会这样玩" 的属性。
                       对标 Hypothesis (Python)、QuickCheck (Haskell)、Jepsen。
      - HANDWRITTEN  : 人工编写。D3 阶段没有 LLM 时先用它走通闭环。
    """

    CONTRACT = "contract"
    EXPLORATORY = "exploratory"
    PROPERTY = "property"
    HANDWRITTEN = "handwritten"

# --------------------------------------------------------------------------- #
# 断言元数据 —— 描述"什么时候检查不变式"
# --------------------------------------------------------------------------- #

class AssertionWhen(str, Enum):
    """不变式的检查时机。

    不变式本身是 "总是成立" 的性质，但在实现里我们分三种检查策略：

      - EVERY_TICK : 每个 tick 都检查。开销最大，但能捕捉瞬态违规。
                     适用：hp_nonneg / mp_nonneg 这种 'ALWAYS' 类。

      - END_OF_RUN : 只在整个测试用例跑完后检查最终状态 + 全 trace。
                     开销最小。适用：cooldown 计算、interrupt 后一切清理、
                     buff 刷新语义等，只要结束时状态正确就行。

      - ON_EVENT   : 当指定事件发生时立即检查（D4 以后启用，预留枚举）。
                     适用：DoT 总伤结算这种事件触发型。
    """

    EVERY_TICK = "every_tick"
    END_OF_RUN = "end_of_run"
    ON_EVENT = "on_event"

class Assertion(BaseModel):
    """一条断言 = 一个 Invariant + 检查时机 + 可选的参数。

    我们把 Invariant 和 Assertion 分开的原因：
      - Invariant 是 '这条性质本身'（模块化、可复用）
      - Assertion 是 '这个测试用例里如何使用它'（检查时机、容忍度）
    同一条不变式可以被不同用例以不同方式检查。
    """

    invariant: Invariant
    when: AssertionWhen = AssertionWhen.END_OF_RUN
    # 当 when=ON_EVENT 时，指定要监听的事件 kind（参见 gameguard.domain.event）
    on_event_kind: str | None = None

# --------------------------------------------------------------------------- #
# TestCase —— 单个测试用例
# --------------------------------------------------------------------------- #

class TestCase(BaseModel):
    """单条测试用例。

    一个用例的"契约"：
      1) reset(seed)
      2) 依次执行 actions
      3) 按 assertions 的 when 策略检查 invariants
      4) 返回 TestResult
    """

    # ---- 标识与元信息 ----
    id: str = Field(
        ...,
        description=(
            "稳定、人类可读的用例 ID，进版本库后应与代码变更一起 review。"
            "命名建议：`<module>-<behavior>-<seed>`，如 "
            "`cooldown-fireball-after-switch-42`。"
        ),
    )
    name: str = Field(..., description="简短描述，进报告标题")
    description: str = Field("", description="长描述，用于面试讲故事 & bug 报告")
    tags: list[str] = Field(default_factory=list, description="如 ['skill', 'cooldown', 'P1']")
    strategy: TestStrategy = TestStrategy.HANDWRITTEN
    # 生成的来源——策划文档的哪一条不变式/章节促成了这条用例。
    # 在 bug 报告里引用回文档，方便追溯"需求/实现/测试"的三角关系。
    derived_from: list[str] = Field(
        default_factory=list,
        description="如 ['docs/example_skill_v1.md#I-04', 'invariant:cooldown-isolation']",
    )

    # ---- 运行配置 ----
    seed: int = Field(42, description="决定 RNG 的随机性；相同 seed -> 相同 trace")
    sandbox: str = Field(
        "pysim:v1",
        description="`<adapter>:<version>` 形式，例 'pysim:v1' / 'pysim:v2' / 'unity:mock'",
    )
    timeout_ticks: int = Field(10_000, description="硬超时，防无限循环")

    # ---- 动作序列 ----
    actions: list[Action] = Field(..., description="按序执行的沙箱动作")

    # ---- 断言 ----
    assertions: list[Assertion] = Field(default_factory=list)

# --------------------------------------------------------------------------- #
# TestPlan —— 一组 TestCase 的"跑批"单位
# --------------------------------------------------------------------------- #

class TestPlan(BaseModel):
    """TestPlan = TestCase 的集合 + 元数据。

    为什么要有 Plan 这一层？
      - 用例本身应可单跑（方便局部调试 / IDE 断点），
        但 CI / 回归要跑一大片 —— 这层就是 "一大片" 的清单。
      - LLM 在生成时是以 "plan" 为单位一次输出多条 case，
        Plan 是自然的产物 boundary。
    """

    id: str = Field(..., description="例 'skill_system.regression.v1'")
    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    cases: list[TestCase] = Field(default_factory=list)

    # 针对全 plan 的"全局"断言——例如 "跑完 plan 后没有任何角色死亡"。
    # 暂留空，D9 做回归对比时会用到。
    plan_assertions: list[Assertion] = Field(default_factory=list)

    def by_id(self, case_id: str) -> TestCase:
        for c in self.cases:
            if c.id == case_id:
                return c
        raise KeyError(case_id)

# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #

class CaseOutcome(str, Enum):
    """测试用例的四种最终状态。

    这四态是 Allure / pytest-html 等主流报告系统的共同抽象：
      - PASSED  : 所有断言通过
      - FAILED  : 至少一条断言违反（= 找到了 bug，或测试用例本身错了）
      - ERROR   : 执行期异常（动作不被接受、沙箱崩溃等，和 FAILED 区分开）
      - SKIPPED : 因为前置条件不满足被跳过（D3 暂不用，先留）
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"

class AssertionOutcome(BaseModel):
    """单条断言的检查结果。"""

    assertion_invariant_id: str
    when: AssertionWhen
    result: InvariantResult

class TestResult(BaseModel):
    """单条 TestCase 的跑后结果。"""

    case_id: str
    case_name: str
    outcome: CaseOutcome
    seed: int
    sandbox: str
    # 执行细节
    ticks_elapsed: int = 0
    sim_time: float = 0.0
    wall_time_ms: float = 0.0
    # 失败 / 错误信息
    error_message: str | None = None  # 当 outcome == ERROR 时填
    assertion_results: list[AssertionOutcome] = Field(default_factory=list)
    # 证据：event log 与最终 state 的指纹（完整 trace 另存文件）
    trace_path: str | None = None
    event_count: int = 0

    @property
    def failing_assertions(self) -> list[AssertionOutcome]:
        """便捷属性：返回所有未通过的断言。"""
        return [a for a in self.assertion_results if not a.result.passed]

class TestSuiteResult(BaseModel):
    """整个 TestPlan 的跑后结果。"""

    plan_id: str
    plan_version: str
    sandbox: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    wall_time_ms: float = 0.0
    cases: list[TestResult] = Field(default_factory=list)

    def summary_line(self) -> str:
        """给 CLI / CI 退出码 / 一行报告用的摘要。"""
        return (
            f"{self.plan_id} @ {self.sandbox}: "
            f"{self.passed}/{self.total} passed, "
            f"{self.failed} failed, {self.errored} errored "
            f"({self.wall_time_ms:.0f} ms)"
        )

    @property
    def has_failures(self) -> bool:
        """CI 友好：有任何失败或错误就返回 True（用于非零退出码）。"""
        return self.failed > 0 or self.errored > 0
