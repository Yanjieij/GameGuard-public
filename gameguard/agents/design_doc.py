"""把策划文档读成结构化 InvariantBundle 的 Agent。

输入一份策划文档路径，输出一个 InvariantBundle（一批 Invariant 加元数据）。
只干这一件事：不生成测试用例（TestGenAgent 管）、不调沙箱（Executor 管）、
不写报告（Triage 管）。各个 agent 职责分离后改 prompt、换实现都方便。

让 LLM 吐结构化数据有两条路：
  1) 直接让 provider 走 response_format=json_schema 或 tool_choice=required。
     省一次调用，但不同 provider 支持参差，出错了不好 debug。
  2) 单独定义 emit_xxx tool，LLM 把结果当 tool 参数交过来。多一轮调用，
     但复用 tool-calling 协议，schema 校验稳、trace 记录自然。

GameGuard 走第 2 条。好处还有一个：DesignDocAgent 和 TestGenAgent 共用同一
套 AgentLoop 和工具协议，一致性高。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

from gameguard.agents.base import AgentLoop, AgentRunStats
from gameguard.domain.invariant import Invariant, InvariantBundle
from gameguard.llm.client import LLMClient
from gameguard.tools.doc_tools import DocRepository, build_doc_tools
from gameguard.tools.schemas import Tool, ToolRegistry

# --------------------------------------------------------------------------- #
# emit_invariant 工具
# --------------------------------------------------------------------------- #

class EmitInvariantInput(BaseModel):
    """``emit_invariant`` 的参数 schema。

    把 Invariant 作为一整块对象传入，而不是逐字段——因为 Invariant 是一个
    discriminated union，Pydantic 会按 `kind` 自动选中正确的子类型。
    """

    invariant: dict[str, Any] = Field(
        ...,
        description=(
            "一条不变式的完整 JSON 对象。必须含字段："
            "id (str), description (str), kind (str)；"
            "并按 kind 的要求补齐其它字段。"
            "支持的 kind："
            "hp_nonneg(actor), mp_nonneg(actor), "
            "cooldown_at_least_after_cast(actor, skill, expected_cooldown [, tolerance]), "
            "buff_stacks_within_limit(actor, buff, max_stacks), "
            "buff_refresh_magnitude_stable(actor, buff, expected_magnitude [, tolerance]), "
            "interrupt_clears_casting(actor), "
            "interrupt_refunds_mp(actor, skill)."
        ),
    )
    rationale: str = Field(
        "",
        description="为什么从文档中抽出这条不变式，引用哪一节。可选但强烈建议填。",
    )
    source_section: str | None = Field(
        None,
        description="这条不变式对应文档的哪一节（heading 文本）。",
    )

class FinalizeInput(BaseModel):
    """``finalize`` 无参数（用于让 LLM 显式宣告工作结束）。"""

    reason: str = Field(
        "done",
        description="简要说明你为什么认为抽取完成。",
    )

class EmitResult(BaseModel):
    """emit_invariant 的返回值。"""

    ok: bool
    invariant_id: str
    count_so_far: int
    # 如果校验失败，把错误也带回去让 LLM 自修复
    error: str | None = None

class FinalizeResult(BaseModel):
    ok: bool = True
    message: str = "ok"
    count: int = 0

# --------------------------------------------------------------------------- #
# 收集器 —— 承接 emit_invariant 的结果
# --------------------------------------------------------------------------- #

@dataclass
class _Collector:
    """运行期持有 agent 逐条 emit 的不变式。

    collector 不是 tool 的一部分；但 tool 函数会通过闭包捕获它，形成典型
    的"tool 是薄壳，状态在外部"的模式。这样好处：
      - tool 函数可测、纯函数式
      - 一次 run 一个 collector 实例，不会串线
    """

    items: list[Invariant] = field(default_factory=list)
    rationales: dict[str, str] = field(default_factory=dict)
    source_sections: dict[str, str] = field(default_factory=dict)
    finalized: bool = False
    finalize_reason: str = ""

# InvariantBundle 对应的 Pydantic Adapter，用来把单条 dict 转成 Invariant。
_invariant_adapter: TypeAdapter[Invariant] = TypeAdapter(Invariant)

def _build_emit_tool(collector: _Collector) -> Tool:
    def _emit(args: EmitInvariantInput) -> EmitResult:
        try:
            inv = _invariant_adapter.validate_python(args.invariant)
        except Exception as e:  # noqa: BLE001
            return EmitResult(
                ok=False,
                invariant_id=args.invariant.get("id", "?"),
                count_so_far=len(collector.items),
                error=f"invariant schema 校验失败：{e}",
            )
        # 去重（按 id）：LLM 偶尔会重复 emit，同 id 以最后一次为准
        existing = {i.id: idx for idx, i in enumerate(collector.items)}
        if inv.id in existing:
            collector.items[existing[inv.id]] = inv
        else:
            collector.items.append(inv)
        if args.rationale:
            collector.rationales[inv.id] = args.rationale
        if args.source_section:
            collector.source_sections[inv.id] = args.source_section
        return EmitResult(
            ok=True, invariant_id=inv.id, count_so_far=len(collector.items)
        )

    return Tool(
        name="emit_invariant",
        description=(
            "把你从文档里抽取到的一条机器可验证的不变式保存下来。"
            "每条不变式调用一次此工具。id 必须全局唯一（推荐与文档中的"
            "编号一致，如 'I-04'）。可以多次调用把所有不变式逐条提交。"
        ),
        input_model=EmitInvariantInput,
        fn=_emit,
    )

def _build_finalize_tool(collector: _Collector) -> Tool:
    def _finalize(args: FinalizeInput) -> FinalizeResult:
        collector.finalized = True
        collector.finalize_reason = args.reason
        return FinalizeResult(count=len(collector.items), message=args.reason or "ok")

    return Tool(
        name="finalize",
        description=(
            "当你确认已经抽完所有不变式时，调用此工具宣告结束。调用后"
            "系统会认为你可以停止工作；请勿在未 emit 足够多条不变式前调用。"
        ),
        input_model=FinalizeInput,
        fn=_finalize,
    )

# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """你是 GameGuard 的 DesignDocAgent —— 一个资深游戏 QA 工程师。

你的唯一任务：把一份策划设计文档转化为一组机器可验证的不变式（Invariant），
交给下游的 TestGenAgent 用来生成测试用例。

## 工作流程

1. 调用 `list_docs` 查看可读文档；再用 `list_doc_sections` 看目录。
2. 优先阅读包含这些关键词的章节：数据表、公式、状态机、冷却、buff、
   stack、打断、MP、不变式、Given-When-Then。
3. 对每一条你能从文档里抽出来的性质，调用 `emit_invariant` 提交。
4. 确认自己已经覆盖了文档中明确编号的不变式（通常以 `I-01` / `I-02` … 标注），
   然后调用 `finalize`。不 finalize 会被视为没完成工作。

## 关键：具体的 actor 与 skill 名称

文档中角色表给出的 actor ID 就是唯一可用的名字：
  - `p1`     —— Player（玩家角色）
  - `dummy`  —— Training Dummy（受击目标）

不要使用 `any`、`all`、`character` 等占位符。当某条不变式在文档里说
"所有角色"时，为每个相关角色分别 emit 一条（例如 `I-01-p1` 和 `I-01-dummy`）。

技能 ID 同理：使用文档数据表里的完整 ID（`skill_fireball` / `skill_frostbolt`
/ `skill_ignite` / `skill_focus`），不要缩写。

## 抽取原则

- 只提可机器验证的条款。模糊描述（"玩法要爽快"）不提。
- 每条不变式只对应一种 kind，参数要齐全。如果你不确定 kind 名字，
  阅读 `emit_invariant` 的参数描述。
- id 命名：优先复用文档里的编号（如 `I-04-fireball`），否则用短 kebab-case。
  当同一条文档规则适用于多个角色/技能时，在 id 后缀加上具体对象
  （例如 `I-08-focus` 表示 I-08 用在 skill_focus 上）。
- rationale 字段必填：说明你从哪一段推出来的。

## 关键约束

- 不要重复 emit 同一个 id 的不变式。
- 不要把 v1/v2 差异当作不变式；不变式应在任何正确实现下都成立。
- 打断退款（`interrupt_refunds_mp`）最有价值的验证对象是施法时间长的技能
  （如 `skill_focus`），短施法技能难以被打断。

## 效率建议

- 你最多只能调用 20 步 tool。读文档 4 步足够，剩下用来 emit + finalize。
- 在一个 assistant 轮次里可以并行发起多个 `emit_invariant` 调用（LLM 协议
  支持 parallel tool_calls），这样能显著节省步数。

现在开始吧。用户会告诉你该处理哪份文档。
"""

# --------------------------------------------------------------------------- #
# Agent 的对外入口
# --------------------------------------------------------------------------- #

@dataclass
class DesignDocResult:
    """一次 DesignDocAgent 运行的产物。"""

    bundle: InvariantBundle
    rationales: dict[str, str]
    source_sections: dict[str, str]
    stats: AgentRunStats
    finalized_by_agent: bool

def run_design_doc_agent(
    *,
    doc_paths: list[str | Path],
    llm: LLMClient,
    max_steps: int = 20,
) -> DesignDocResult:
    """读一份或多份策划文档 → 产出 InvariantBundle。

    这是面向调用方的唯一 API。面试 demo 时一行代码就能跑：
      result = run_design_doc_agent(doc_paths=["docs/example_skill_v1.md"], llm=client)
    """
    # 1) 组装文档仓库 + 文档 tools（保留交互式读文档的完整设计）
    repo = DocRepository()
    doc_names: list[str] = []
    for p in doc_paths:
        doc_names.append(repo.register_file(p))

    tools = ToolRegistry()
    tools.register_many(build_doc_tools(repo))

    # 2) 结果收集器 + emit tools
    collector = _Collector()
    tools.register(_build_emit_tool(collector))
    tools.register(_build_finalize_tool(collector))

    # 3) 拉起 AgentLoop
    loop = AgentLoop(
        client=llm,
        tools=tools,
        agent_name="DesignDocAgent",
        system_prompt=SYSTEM_PROMPT,
        max_steps=max_steps,
        # 强制每轮都要调工具，避免推理型模型"只想不做"。
        # 详细原因见 LLMClient.chat 的 tool_choice 注释。
        tool_choice="required",
        stop_when=lambda r: r.ok and r.tool_name == "finalize",
    )
    loop.add_user_message(
        f"请处理以下文档并抽取不变式：{doc_names}\n"
        f"目标产出 ≥ 6 条（策划文档 v1 至少有 8 条显式标注的 I-xx）。"
    )

    stats = loop.run()

    bundle = InvariantBundle(items=list(collector.items))
    return DesignDocResult(
        bundle=bundle,
        rationales=dict(collector.rationales),
        source_sections=dict(collector.source_sections),
        stats=stats,
        finalized_by_agent=collector.finalized,
    )
