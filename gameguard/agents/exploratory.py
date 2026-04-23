"""模拟"好奇玩家"，主动尝试奇怪组合让 invariant 红的 Agent。

和 TestGenAgent 的区别在思路：TestGen 是契约驱动（每条不变式至少配一条
focused 用例），这个是对抗驱动（故意挑边缘组合，找还没被发现的 bug）。
两者共用同一批工具（list_invariants / list_skills / list_characters /
emit_testcase / finalize），只在 system prompt 上分道扬镳——基础设施相同，
思考方式不同。

类比行业：渗透测试 vs 单元测试。都基于已知 invariant，但视角对立。NetEase
Wuji 论文里 exploratory testing 是 RL Agent 的强项，我们用 LLM + tool-calling
做出等价能力。

为什么不给 TestGenAgent 加个开关？因为契约和对抗的思考方式完全不同，塞开关
会让 prompt 又长又割裂。两个 agent 文件更清爽。
"""
from __future__ import annotations

from dataclasses import dataclass

from gameguard.agents.base import AgentLoop, AgentRunStats
from gameguard.domain.character import Character
from gameguard.domain.invariant import InvariantBundle
from gameguard.domain.skill import SkillBook
from gameguard.llm.client import LLMClient
from gameguard.testcase.model import TestPlan, TestStrategy
from gameguard.tools.schemas import ToolRegistry
from gameguard.tools.testgen_tools import (
    TestGenContext,
    build_testgen_tools,
    context_to_plan,
)

SYSTEM_PROMPT = """你是 GameGuard 的 ExploratoryAgent —— 一个对抗心态的资深
游戏 QA。你的任务不是写覆盖契约的测试，而是主动尝试让 invariant 红。

## 心态

把自己当成一个"好奇又恶意"的玩家：
  - 故意做奇怪的事：在 cast 期间立刻打断、踩 buff 刷新边界、cast 完就切技能
  - 探索系统的"角落"：MP 接近 0、CD 刚好结束、buff 刚好过期
  - 利用资源约束：连打到 MP 用光、连续 interrupt 测试状态机
  - 不要只测 happy path——testcase 的目标是触发异常

## 工作流程

1. 调用 `list_invariants` 看现有 invariants（你要尝试违反它们）
2. 调用 `list_skills` 了解技能参数
3. 调用 `list_characters` 看角色 id
4. 设计 5-8 条 对抗式 用例，每条用 `emit_testcase` 提交
5. 全部完成后 `finalize`

## 用例设计模板（对抗思路）

A. 状态机边界：`cast(p1, focus, p1) → wait(0.05) → interrupt(p1) → cast(...)`
   立即打断后立刻再开新 cast，看状态机是否清理干净。
B. 资源耗尽：连续 cast 到 MP 不足（无 wait），看沙箱是否优雅拒绝、是否产
   异常副作用。
C. buff 刷新边界：在 buff 即将过期的最后一个 tick 重新施加。
D. CD 边界：cast→wait(cooldown - tick_dt)→cast（应被拒）；wait(cooldown +
   tick_dt)→cast（应通过）。
E. 多 buff 叠加：同时积累 chilled / burn / arcane_power 三个 buff 看
   交互。
F. 打断后冷却：cast 长技能 → 0.5s 后打断 → 立刻 cast 同技能（应被允许，
   v1 黄金 = 不进 CD）。

## 强约束

- actor / target 必须是 list_characters 返回的 id。
- assertion_invariant_ids 仍然要引用真实 invariant id（用例红 = invariant
  被违反）。注意：你设计的用例应当预期通过（即不变式不会被违反）；
  如果它在 v1 上意外红了，那就是真的发现了 bug 或 invariant 写错。
- tags 必须含 `'exploratory'`，便于报告里区分。

## 与 TestGenAgent 的协作

ExploratoryAgent 是 补充 而非替代。我们假设 TestGenAgent 已经覆盖了
契约用例；你只需要补 5-8 条对抗式用例即可，不必重复 contract 路径。

现在开始。
"""

@dataclass
class ExploratoryResult:
    plan: TestPlan
    stats: AgentRunStats
    finalized_by_agent: bool

def run_exploratory_agent(
    *,
    bundle: InvariantBundle,
    skill_book: SkillBook,
    initial_characters: list[Character],
    llm: LLMClient,
    plan_id: str = "exploratory_generated",
    plan_name: str = "Exploratory · 对抗式回归",
    plan_description: str = "",
    max_steps: int = 25,
    prefetch_context: bool = False,
    tool_choice: str | dict | None = None,
) -> ExploratoryResult:
    """跑 ExploratoryAgent，产出一份 对抗式 TestPlan。

    参数与 ``run_test_gen_agent`` 几乎一致；区别在 system prompt 与 strategy
    标记。所有产出 case 的 ``strategy`` 都被打成 ``EXPLORATORY``。
    """
    ctx = TestGenContext(
        bundle=bundle, skill_book=skill_book, initial_characters=initial_characters
    )

    tools = ToolRegistry()
    tools.register_many(build_testgen_tools(ctx))

    loop = AgentLoop(
        client=llm,
        tools=tools,
        agent_name="ExploratoryAgent",
        system_prompt=SYSTEM_PROMPT,
        max_steps=max_steps,
        tool_choice=tool_choice,
        stop_when=lambda r: r.ok and r.tool_name == "finalize",
    )

    if prefetch_context:
        from gameguard.agents.test_gen import _build_prefetched_task_message
        loop.add_user_message(
            _build_prefetched_task_message(bundle, skill_book, initial_characters)
        )
    else:
        loop.add_user_message(
            f"现在有 {len(bundle.items)} 条 invariants 已被 contract 测试覆盖。\n"
            f"请按 system prompt 的对抗思路，补充 5-8 条 **exploratory** 用例。\n"
            f"按以下顺序：list_invariants → list_skills → list_characters → "
            f"emit_testcase × N → finalize。"
        )

    stats = loop.run()
    plan = context_to_plan(
        ctx, plan_id=plan_id, plan_name=plan_name, description=plan_description
    )
    # 把所有 case 的 strategy 标为 EXPLORATORY（覆盖 emit_testcase 默认的 CONTRACT）
    for case in plan.cases:
        case.strategy = TestStrategy.EXPLORATORY
    return ExploratoryResult(
        plan=plan, stats=stats, finalized_by_agent=ctx.finalized
    )
