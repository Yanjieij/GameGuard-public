"""串联 DesignDocAgent 和 TestGenAgent 的工作流编排层。

D5 版本 Orchestrator 只是 ~50 行串行 pipeline，单看似乎多余——但它存在
是为了给 D7-D10 扩展留位：

  - D7 Triage：跑完 plan 如果有失败，自动喂给 TriageAgent 产 bug 单
  - D9 回归 diff：跑 v1/v2 两次再比较，这是 Orchestrator 级别的职责
  - D10 Critic（stretch）：在 emit_testcase 之后插 review 节点评测用例质量
  - 预留 review_hook：Critic 不用时保留空实现，接入时零改动

把这些职责放进 Orchestrator 比堆进 cli.py 健康——CLI 是 UI，Orchestrator
才是工作流。

整体是 plan-and-execute 模式：

  1) plan 阶段：DesignDocAgent 读文档 → InvariantBundle；TestGenAgent 用
     invariants 生成 TestPlan。
  2) execute 阶段：Runner 跑 TestPlan → TestSuiteResult；有失败就由 Triage
     聚类成 BugReport。

每个阶段内部是短 ReAct 循环，阶段之间是确定性管线。好处是 plan 阶段产物
可缓存复用，execute 阶段没有 LLM、seed 决定结果所以可复现，trace 分段也
好观测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gameguard.agents.base import AgentRunStats
from gameguard.agents.design_doc import (
    DesignDocResult,
    run_design_doc_agent,
)
from gameguard.agents.test_gen import TestGenResult, run_test_gen_agent
from gameguard.agents.triage import TriageResult, run_triage_agent
from gameguard.domain.character import Character
from gameguard.domain.invariant import InvariantBundle
from gameguard.domain.skill import SkillBook
from gameguard.llm.client import LLMClient
from gameguard.reports.schema import TriageOutput
from gameguard.testcase.model import TestPlan, TestSuiteResult
from gameguard.testcase.runner import run_plan

# 预留 Critic hook（D10 stretch 实现时接入，不来时空操作）
ReviewHook = Callable[[TestPlan], TestPlan] | None

@dataclass
class PipelineResult:
    """Orchestrator 走完 plan 阶段的整体产物。"""

    invariants: InvariantBundle
    plan: TestPlan
    design_doc_stats: AgentRunStats
    test_gen_stats: AgentRunStats
    # 便于外部复盘
    design_doc_rationales: dict[str, str] = field(default_factory=dict)

def run_plan_pipeline(
    *,
    doc_paths: list[str | Path],
    skill_book: SkillBook,
    initial_characters: list[Character],
    llm: LLMClient,
    plan_id: str = "agent_generated",
    plan_name: str = "Agent 生成的测试套件",
    plan_description: str = "",
    review_hook: ReviewHook = None,
    max_steps_design: int = 25,
    max_steps_testgen: int = 30,
    prefetch_context: bool = False,
    tool_choice: str | dict | None = None,
) -> PipelineResult:
    """完整的 "plan 阶段" 管线：文档 → invariants → testplan。

    不跑执行阶段 —— 让 CLI 去做，因为 execute 阶段可能有很多跑法
    （单次 run / 回归 diff / 不同沙箱版本）。

    参数（在 D5 默认基础上增加）：
        prefetch_context: 透传给 TestGenAgent；False=discovery 模式（默认），
            True=把静态上下文嵌入 user message（GLM-4.7 fallback / CI 省 token）
        tool_choice: 透传给 TestGenAgent 的 LLM 调用；None=auto，"required"=
            强制每轮调工具（推理型模型 workaround）
    """
    # ---- 1) DesignDocAgent ----
    dd: DesignDocResult = run_design_doc_agent(
        doc_paths=doc_paths,
        llm=llm,
        max_steps=max_steps_design,
    )

    # ---- 2) TestGenAgent ----
    tg: TestGenResult = run_test_gen_agent(
        bundle=dd.bundle,
        skill_book=skill_book,
        initial_characters=initial_characters,
        llm=llm,
        plan_id=plan_id,
        plan_name=plan_name,
        plan_description=plan_description,
        max_steps=max_steps_testgen,
        prefetch_context=prefetch_context,
        tool_choice=tool_choice,
    )

    # ---- 3) 可选 Critic review（D10 stretch）----
    plan = tg.plan
    if review_hook is not None:
        plan = review_hook(plan)

    return PipelineResult(
        invariants=dd.bundle,
        plan=plan,
        design_doc_stats=dd.stats,
        test_gen_stats=tg.stats,
        design_doc_rationales=dd.rationales,
    )

# --------------------------------------------------------------------------- #
# Execute 阶段管线（D7 接入 Triage）
# --------------------------------------------------------------------------- #

@dataclass
class ExecuteResult:
    """跑完一个 plan + 可选 triage 的整体产物。"""

    suite: TestSuiteResult
    triage: TriageOutput | None  # None 表示没失败 / triage 关闭
    triage_stats: AgentRunStats | None = None

def run_execute_pipeline(
    *,
    plan: TestPlan,
    factory,
    llm: LLMClient | None = None,
    artifacts_dir: str = "artifacts",
    suite_json_path: str | None = None,
    do_triage: bool = True,
    triage_tool_choice: str | dict | None = None,
) -> ExecuteResult:
    """跑一个 TestPlan，可选自动 triage 失败用例。

    参数：
        plan:               已加载的 TestPlan（YAML 加载或 plan 阶段产物）
        factory:            sandbox 工厂，签名 ``(spec) -> GameAdapter``
        llm:                triage 用的 LLM client。do_triage=True 时必须给。
        artifacts_dir:      trace/snapshot/suite.json 落盘根目录
        suite_json_path:    可选；显式指定 suite.json 路径。默认
                            ``<artifacts_dir>/suite.json``
        do_triage:          True 时遇到失败自动调 TriageAgent；False 跳过。
        triage_tool_choice: 透传给 TriageAgent 的 LLM tool_choice

    返回：
        ExecuteResult —— suite 与（可选）triage 输出。
    """
    from pathlib import Path as _P

    if suite_json_path is None:
        suite_json_path = str(_P(artifacts_dir) / "suite.json")

    suite = run_plan(
        plan,
        factory=factory,
        artifacts_dir=artifacts_dir,
        suite_json_path=suite_json_path,
    )

    if not do_triage or not suite.has_failures:
        return ExecuteResult(suite=suite, triage=None)

    if llm is None:
        raise ValueError("do_triage=True 但 llm=None，无法做 triage")

    tr: TriageResult = run_triage_agent(
        suite=suite, llm=llm, tool_choice=triage_tool_choice
    )
    return ExecuteResult(
        suite=suite,
        triage=tr.output,
        triage_stats=tr.stats,
    )
