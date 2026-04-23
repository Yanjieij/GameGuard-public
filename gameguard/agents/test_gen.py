"""把 InvariantBundle 编译成 TestPlan 的 Agent。

输入是 DesignDocAgent 给的 InvariantBundle 加上沙箱初始状态（角色、技能），
输出一份 TestPlan——每条 TestCase 的动作序列要能在沙箱里真的触发到对应
不变式的可观测窗口。这一版是 D5 的契约驱动策略（每条不变式至少配一条
focused 用例），D8 会再加 exploratory 和 property-based。

为什么不直接写个 convert 函数？"契约 → 测试"看起来机械，但细节很吃游戏直觉：

  - cooldown_at_least_after_cast(skill_focus)：必须先 cast 一次 Focus，但
    Focus cast_time=2s、CD=20s，动作序列得算得对。
  - interrupt_refunds_mp(skill_focus)：要在 cast 窗口内打断，得知道
    cast_time 还要留余量。
  - buff_refresh_magnitude_stable(buff_burn)：要让 refresh 真的发生，得
    连 Ignite 两次，间隔按 12s CD 算。

这些"同时满足多个约束"的推理是 LLM 比规则引擎强的地方。所以让 LLM 做
这种需要游戏直觉的编译，比死翻译一条条不变式划算。

emit 用例同样走 tool 而不是 structured output：schema 出错能让 LLM 自己修、
trace 里能看每条用例怎么生成、单条 emit 失败不会污染其他用例。

discovery 和 prefetch 两种模式
------------------------------
run_test_gen_agent 有个 prefetch_context 开关：

- discovery（默认）：user message 里不塞静态上下文，让 LLM 按 system
  prompt 的流程去调 list_invariants / list_skills / list_characters 三个工具。
  trace 里能看到 Agent 在"浏览数据"，讲故事好听。代价是多 ~3 步、token +4%、
  wall-clock +30%（DeepSeek 实测）。
- prefetch：把所有静态上下文塞进 user message，跳过 list_* 调用。GLM-4.7
  这类推理型模型在多轮 tool history 下容易静默卡死，这个模式是它们的
  workaround；DeepSeek 上可以用它省 token、加速 batch run。

两种模式共用代码、工具、prompt。prompt 里都写着"先读三个 list_*"，prefetch
下 LLM 发现 prompt 已经给了数据，自然就跳过工具调用——这和 OpenAI / DeepSeek
的 tool-calling 协议语义一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from gameguard.agents.base import AgentLoop, AgentRunStats
from gameguard.domain.character import Character
from gameguard.domain.invariant import InvariantBundle
from gameguard.domain.skill import SkillBook
from gameguard.llm.client import LLMClient
from gameguard.testcase.model import TestPlan
from gameguard.tools.schemas import ToolRegistry
from gameguard.tools.testgen_tools import (
    TestGenContext,
    build_testgen_tools,
    context_to_plan,
)

SYSTEM_PROMPT = """你是 GameGuard 的 TestGenAgent —— 一个资深游戏 QA 工程师，
负责把不变式编译成能在沙箱里真实跑通的测试用例。

## 目标

对 InvariantBundle 里的每一条不变式，设计至少一条测试用例，
使其进入可观测窗口（即执行过对应 skill / buff / interrupt 后再检查）。
一条用例可以覆盖多条不变式 —— 尽量合并相似场景以节省步数。

## 工作流程

1. `list_invariants` → 了解要覆盖什么
2. `list_skills` → 了解技能参数（cast_time/cooldown/mp_cost）
3. `list_characters` → 了解 actor id（p1 / dummy）
4. 依次 `emit_testcase` 提交用例；可以在一个 assistant 轮次里并行多个 emit
5. 全部覆盖后 `finalize`

## 关键约束（基于沙箱规则）

- actor / target 必须是 list_characters 返回的 id（不要写 any）。
- MP 检查：Player 初始 100 MP；连续施法会用光 MP，注意 Focus 20 + Ignite 40 + Fireball 30 = 90，所以一条用例里最多 2-3 次施法。
  - 如果需要多次施法，可加足够长的 `wait` 让 MP 恢复不会发生（本沙箱不自动回蓝）。
  - 或者拆成多条用例，每条不同 seed。
- 冷却：同一技能在 CD 内不能再次 cast，沙箱会拒绝、Runner 会报 ERROR。
  - 若要测 "在 CD 期内切其它技能"，先 cast A → wait 1.0 让 A 完成 → cast B（注意 B 的 MP 是否足够）。
  - 若要测 buff refresh：两次施放同一技能之间必须 wait ≥ 该技能的 cooldown。
- cast_time：`wait` 时间要把整个 cast_time 覆盖住，否则伤害/buff 不会结算。
- interrupt 只对长 cast_time 技能有意义。例如 Focus (2s)。对 Ignite (0s 瞬发) 没有意义。

## 动作序列模板

- 测 cast 完成的属性（cd、buff 应用等）：
    [cast(p1, X, target), wait(cast_time + 0.1)]
- 测 cd 独立性：
    [cast(p1, A, t), wait(1.0), cast(p1, B, t), wait(1.5)]   # 期间检查 A 的 CD
- 测 buff refresh：
    [cast(p1, X, t), wait(cast_time), wait(cooldown + 0.1), cast(p1, X, t), wait(cast_time)]
- 测 interrupt 退款：
    [cast(p1, skill_focus, p1), wait(0.5), interrupt(p1)]
- 测 HP/MP 非负：
    任何合法序列都可，只要有动作产生实际数值变动。

## 输出要求

- 每条用例的 `id` 要稳定、kebab-case（例：`cooldown-isolation-fireball-frostbolt`）
- `derived_from` 里引用 invariant id（`invariant:I-04-fireball`）
- `tags` 至少有一个领域标签（skill / buff / interrupt / cooldown）
- 一条 `emit_testcase` 至少 1 个 `assertion_invariant_ids`；尽量把
  同场景下的多条不变式放到同一条用例里断言。

## 效率建议

- 不必每条 invariant 一条用例。把同类 invariant 合并可大幅减少总用例数。
  例如 `I-01-p1` + `I-01-dummy` + `I-02-p1` + `I-02-dummy` 可以由一条
  "smoke-fireball" 用例同时检查。
- 每次 emit 都是一轮 tool call，尽量在同一 assistant 轮次 parallel emit。

现在开始。
"""

@dataclass
class TestGenResult:
    plan: TestPlan
    stats: AgentRunStats
    finalized_by_agent: bool

def run_test_gen_agent(
    *,
    bundle: InvariantBundle,
    skill_book: SkillBook,
    initial_characters: list[Character],
    llm: LLMClient,
    plan_id: str = "agent_generated",
    plan_name: str = "Agent 生成的测试套件",
    plan_description: str = "",
    max_steps: int = 25,
    prefetch_context: bool = False,
    tool_choice: str | dict | None = None,
) -> TestGenResult:
    """跑 TestGenAgent，产出一份 TestPlan。

    参数：
        bundle / skill_book / initial_characters: 输入静态上下文
        llm: 已初始化的 ``LLMClient``
        plan_id / plan_name / plan_description: 产物 ``TestPlan`` 的元数据
        max_steps: AgentLoop 步数上限（discovery 模式建议 ≥ 25）
        prefetch_context:
            - ``False`` (默认, demo/recommended): 不嵌入静态上下文，LLM 真的
              调用 list_invariants / list_skills / list_characters 三个工具。
              trace 完整、能讲故事；token + 用 ~4%；步数 + ~3。
            - ``True`` (CI/批跑/GLM-4.7 fallback): 把 invariants / skills /
              characters 全文嵌入 user message，跳过 list_* 工具调用。
        tool_choice:
            - ``None`` (默认): "auto"，LLM 自己决定何时调工具，允许 assistant
              短反思（trace 更可读）
            - ``"required"``: 强制每轮必调工具（GLM-4.7/Claude thinking 等
              推理型模型的对症 workaround；副作用：LLM 永远无法用 content
              做反思，且 finalize 后必须靠 stop_when 收敛）
    """
    ctx = TestGenContext(
        bundle=bundle,
        skill_book=skill_book,
        initial_characters=initial_characters,
    )

    tools = ToolRegistry()
    tools.register_many(build_testgen_tools(ctx))

    loop = AgentLoop(
        client=llm,
        tools=tools,
        agent_name="TestGenAgent",
        system_prompt=SYSTEM_PROMPT,
        max_steps=max_steps,
        tool_choice=tool_choice,
        # finalize 一旦被调用立即结束循环，避免被 tool_choice="required" 逼着多调一次
        stop_when=lambda r: r.ok and r.tool_name == "finalize",
    )

    if prefetch_context:
        loop.add_user_message(
            _build_prefetched_task_message(bundle, skill_book, initial_characters)
        )
    else:
        loop.add_user_message(_build_discovery_task_message(bundle))

    stats = loop.run()
    plan = context_to_plan(
        ctx,
        plan_id=plan_id,
        plan_name=plan_name,
        description=plan_description,
    )
    return TestGenResult(plan=plan, stats=stats, finalized_by_agent=ctx.finalized)

# --------------------------------------------------------------------------- #
# user message 构造器
# --------------------------------------------------------------------------- #

def _build_discovery_task_message(bundle: InvariantBundle) -> str:
    """Discovery 模式：只告诉 LLM 任务和数量，让它自己去调 list_* 工具。

    这是 demo/recommended 模式。trace 里能看到 LLM 走完整的"先看数据再
    生成用例"流程，对面试讲故事 + 体现 Agent 工程价值非常关键。
    """
    return (
        f"本次需要覆盖 {len(bundle.items)} 条 invariants。请按以下顺序完成：\n\n"
        "1. 调用 `list_invariants` 拉取要覆盖的不变式列表（id + kind + 参数）。\n"
        "2. 调用 `list_skills` 了解可用技能的 cast_time / cooldown / mp_cost。\n"
        "3. 调用 `list_characters` 确认 actor / target id（p1 / dummy）。\n"
        "4. 设计 6-10 条测试用例，每条用 `emit_testcase` 提交；同一场景的多条 invariant"
        " 尽量合并到同一条用例的 assertion_invariant_ids 里。\n"
        "5. 全部 emit 完成后调用 `finalize` 结束。\n\n"
        "记得遵循 system prompt 里的 MP/CD/cast_time 约束。"
    )

def _build_prefetched_task_message(
    bundle: InvariantBundle,
    skill_book: SkillBook,
    initial_characters: list[Character],
) -> str:
    """Prefetch 模式：把全部静态上下文嵌入 user message，跳过 list_*。

    用于：
      - GLM-4.7 等推理型模型在多轮 tool-history 触发"静默推理"的 fallback
      - CI / 批量跑 plan 时省 token / 加速
    """
    inv_lines: list[str] = []
    for inv in bundle.items:
        d = inv.model_dump()
        kind = d.pop("kind")
        params = {k: v for k, v in d.items() if k not in ("id", "description")}
        inv_lines.append(f"- `{inv.id}`  kind={kind}  {params}")
    skill_lines = [
        (
            f"- `{sid}` cast={sp.cast_time}s cd={sp.cooldown}s mp={sp.mp_cost} "
            f"dmg={sp.damage_base} target_buffs={sp.target_buffs} self_buffs={sp.self_buffs}"
        )
        for sid, sp in skill_book.specs.items()
    ]
    char_lines = [f"- `{c.id}` ({c.name}) hp={c.hp} mp={c.mp}" for c in initial_characters]

    return (
        f"## 要覆盖的不变式（{len(bundle.items)} 条）\n"
        + "\n".join(inv_lines)
        + "\n\n## 可用技能\n"
        + "\n".join(skill_lines)
        + "\n\n## 角色\n"
        + "\n".join(char_lines)
        + "\n\n## 你的任务\n"
        + "上面已经把 list_invariants / list_skills / list_characters 三个工具的"
        + "结果直接给你了。直接调用 emit_testcase 工具生成 6-10 条测试用例，"
        + "每条覆盖 1-3 条相关不变式。emit 完后调用 finalize 结束。"
    )
