"""对 TestPlan 做 review 的 Agent（D10 stretch）。

动机来自 D5/D6 实测：TestGenAgent 生成的 8 条用例里 5 条 PASS、2 条 ERROR
（算错 wait 时长 / MP 不够）、1 条 FAIL（引用了不存在的 evaluator kind，
是 LLM 幻觉）。CriticAgent 就是在 plan 阶段剔除或修复这 3 条"Agent 自己
的 bug"，让 Runner 拿到的只剩"沙箱真 bug"和"用例真发现的 v2 bug"。

这是多 agent 流水线里最有价值也最容易被忽略的一环——OpenAI 和 Anthropic
的实践都提到加 review 环节能把质量提升 30% 以上。

设计上几个边界要守：

1. Critic 是 review 不是 author：不新增 case，新增交给 TestGen / Exploratory。
2. 静态校验放工具层：MP / CD / wait 时长这些确定性检查让 Python 算，LLM
   负责决策，别让 LLM 算数。
3. patch 优先于 drop：很多用例只是 wait 时长算错，修一行就能跑，没必要扔。
4. 通过 review_hook 接入：Orchestrator 从 D5 就预留了这个 hook，Critic 走
   hook 注入，不破坏 plan-and-execute 主流程。
"""
from __future__ import annotations

from dataclasses import dataclass

from gameguard.agents.base import AgentLoop, AgentRunStats
from gameguard.domain.character import Character
from gameguard.domain.skill import SkillBook
from gameguard.llm.client import LLMClient
from gameguard.testcase.model import TestPlan
from gameguard.tools.critic_tools import CriticContext, build_critic_tools
from gameguard.tools.schemas import ToolRegistry

SYSTEM_PROMPT = """你是 GameGuard 的 CriticAgent —— 一个严苛的 senior QA，
负责审查 TestGenAgent / ExploratoryAgent 生成的 testcase plan 质量。

## 你的边界

- 不新增 case
- 不重新设计场景
- 只做三件事：accept / patch / drop

## 工作流程

1. `list_cases` 看所有待 review 的 case + 它们的静态校验问题数
2. 对每条 case：
   - 零 issue：直接调 `accept_case`（或不调，视为默认 accept）
   - 有 warn 但能跑通：accept
   - 有 error 但能 patch：用 `inspect_case` 看详情 → 用 `patch_case`
     提供修复后的 actions（典型修复：把 wait 时长拉长让 CD 过、删掉
     超 MP 的连续 cast、把 interrupt 从空 cast 状态去掉）
   - error 太多 / 无法救：调 `drop_case` 并写明 reason
3. 全部 case review 完后调用 `finalize`

## patch 准则

- 保持 case 的测试目的：只要它仍能让其涉及的 invariant 进入可观测窗口，
  改 actions 是 OK 的
- 不要改 assertions —— 那是测试目标，不是错
- 改 actions 时常用模板：
  - CD violation：在两次 cast 之间插入 `wait(cooldown + 0.1)`
  - insufficient MP：删掉一次额外 cast，或在前面加 wait（注意：本沙箱
    不会自动回蓝，所以 wait 不能修 MP）
  - interrupt 无 cast：把 interrupt 那一行删掉
  - 静态分析认为 cast 在 cd_violation 但实际是想测 "在 CD 内施法被拒绝"
    的 case：调 `drop_case`，让用例改为依赖 sandbox 的 ERROR 路径触发的
    可测性而非 wait 时长

## 不要做的事

- 不要 drop 全部用例——除非 plan 真的非常差，否则总能保留至少一半
- 不要 修改 assertion_invariant_ids
- 不要 把 ERROR 用例当成 v2 真 bug 报上去——那是用例自己写错

现在开始。
"""

@dataclass
class CriticResult:
    plan: TestPlan
    stats: AgentRunStats
    finalized_by_agent: bool
    accepted: int
    patched: int
    dropped: int

def run_critic_agent(
    *,
    plan: TestPlan,
    skill_book: SkillBook,
    initial_characters: list[Character],
    llm: LLMClient,
    max_steps: int = 30,
    tool_choice: str | dict | None = None,
) -> CriticResult:
    """对 plan 跑一遍 review，返回（可能被改动的）plan。

    plan 会就地修改（patch_case / drop_case 直接动 plan.cases），
    返回值就是同一个对象。
    """
    ctx = CriticContext(
        plan=plan, skill_book=skill_book, initial_characters=initial_characters
    )
    tools = ToolRegistry()
    tools.register_many(build_critic_tools(ctx))

    loop = AgentLoop(
        client=llm,
        tools=tools,
        agent_name="CriticAgent",
        system_prompt=SYSTEM_PROMPT,
        max_steps=max_steps,
        tool_choice=tool_choice,
        stop_when=lambda r: r.ok and r.tool_name == "finalize",
    )
    loop.add_user_message(
        f"待 review 的 plan 有 {len(plan.cases)} 条 case。\n"
        f"按 system prompt 工作流：list_cases → 对有问题的 inspect/patch/drop → finalize。\n"
        f"目标：保留尽可能多的 case，但确保所有保留下来的能在 sandbox 上跑通"
        f"（不会 ERROR）。"
    )
    stats = loop.run()
    return CriticResult(
        plan=plan,
        stats=stats,
        finalized_by_agent=ctx.finalized,
        accepted=len(ctx.accepted),
        patched=len(ctx.patched),
        dropped=len(ctx.dropped),
    )

def make_critic_review_hook(
    skill_book: SkillBook,
    initial_characters: list[Character],
    llm: LLMClient,
    *,
    max_steps: int = 30,
    tool_choice: str | dict | None = None,
):
    """工厂：把 run_critic_agent 包成 ``orchestrator.review_hook`` 兼容的签名。

    orchestrator.run_plan_pipeline(review_hook=...) 直接接受这个返回值。
    """
    def _hook(plan: TestPlan) -> TestPlan:
        result = run_critic_agent(
            plan=plan,
            skill_book=skill_book,
            initial_characters=initial_characters,
            llm=llm,
            max_steps=max_steps,
            tool_choice=tool_choice,
        )
        return result.plan

    return _hook
