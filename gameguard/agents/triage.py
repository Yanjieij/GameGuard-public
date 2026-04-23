"""把 TestSuiteResult 里的失败聚类成 BugReport 的 Agent。

输入一份 TestSuiteResult（有 N 条 FAILED / ERROR 用例），输出 ≤ N 条
BugReport。不写 markdown/html（reports 负责），不重跑沙箱，不修 bug。

聚类分两阶段：

1) 规则阶段（在 tools/triage_tools.py::cluster_failures 里）：按 (invariant
   kind / id 前缀, actor, skill) 把 N 条失败压成 M 个候选簇；ERROR 按
   error_message 前 80 字符 hash 分组。确定性，不花 LLM 钱。

2) LLM 阶段（这个文件）：让 LLM 看候选簇列表 + 关键证据，决定：
   - size > 1 的簇里是不是混了不同 bug，混了就拆（拆 = 多次 emit）
   - 跨簇失败是不是同根，是就 merge_clusters
   - 给每条最终 bug 起标题、写复现步骤、定严重级

LLM 不直接读完整 trace，按需用 read_trace_tail 拉末尾几行，避免 prompt 爆炸。

为什么一定要 LLM 参与、不能纯规则？纯规则的问题是跨 invariant 的同根 bug
抓不到（比如 BUG-001 会同时让 cooldown 测试 FAIL 和 buff refresh 测试 ERROR），
而且模板化的标题和复现步骤不好读。LLM 在这里的价值是：跨规则关联、把复现
步骤写成能直接进 Jira 的中文、根据场景判严重级（hp_nonneg 跌负是 S0 还是
S1 看具体情况）。

成本上每个 size > 1 的簇跑 1-2 轮 LLM；size = 1 的簇走快路径（也可以让
LLM 加工一下标题）。
"""
from __future__ import annotations

from dataclasses import dataclass

from gameguard.agents.base import AgentLoop, AgentRunStats
from gameguard.llm.client import LLMClient
from gameguard.reports.schema import TriageOutput
from gameguard.testcase.model import TestSuiteResult
from gameguard.tools.schemas import ToolRegistry
from gameguard.tools.triage_tools import (
    TriageContext,
    build_triage_tools,
    cluster_failures,
)

SYSTEM_PROMPT = """你是 GameGuard 的 TriageAgent —— 一个资深 QA 主管，
负责把测试失败列表汇总成可以直接进 Jira / 飞书的 bug 单。

## 工作流程

1. 调用 `list_failures` 看所有候选簇（已经按规则做过初步聚类）。
2. 对每个 cluster：
   - size = 1（孤例）：直接调用 `emit_bug_report` 拼装一条 bug。
   - size > 1（多条相关）：先 `inspect_cluster` 看具体失败；如果都
     是同一现象的不同重现就直接 emit_bug_report；如果觉得里面其实有多个
     bug 混在一起，不要 emit 一条——先用 `inspect_cluster` 拉详情，
     必要时 `read_trace_tail` 看 trace 最后 20 行，然后分别 emit多条。
   - 跨 cluster 同根（罕见）：用 `merge_clusters` 合并后再 emit。
3. 全部 cluster 处理完后调用 `finalize`。

## emit_bug_report 写作规范

- title：中文，动词开头，描述现象而非 invariant 名。
  好例："切换技能时 Fireball 冷却被错误重置（影响连招节奏）"
  差例："I-04-fireball failed"
- component：'X.Y' 格式。`Skill.Cooldown` / `Skill.Buff` / `Skill.StateMachine`
  / `Skill.RNG` / `Skill.DoT` 是常用值。
- repro_steps：3-5 条，每条一个动作或断言。中文。第一步永远是
  "用 seed=N 重置 pysim:vX 沙箱"。最后一步是 "在 t=X 时检查 ..."。
- expected / actual：用一句话写清差异，含具体数值。
- severity：不传时按规则映射；如果你认为现象更严重（比如导致死亡）
  可以显式提一级。
- tags：必含一个 BUG-XXX 风格的 tag（如果用例 derived_from 含 'bug:BUG-XXX'）。
- rationale：(可选) 你为什么把这些用例聚成一条 bug。

## 关键约束

- 每个 cluster 只能 emit 一次（重复会被工具拒绝）
- 宁可漏聚也不要乱聚：合并 cluster 一定要有强证据。
- 不要把 ERROR 当成 bug 直接报——很多 ERROR 是测试用例自己写错了
  （MP 不够、CD 内重复 cast）。在 title 里点明 "Suspected test-case bug"
  并给 severity=S2，让 owner 确认。

现在开始。
"""

@dataclass
class TriageResult:
    output: TriageOutput
    stats: AgentRunStats
    finalized_by_agent: bool

def run_triage_agent(
    *,
    suite: TestSuiteResult,
    llm: LLMClient,
    max_steps: int = 30,
    tool_choice: str | dict | None = None,
) -> TriageResult:
    """跑 TriageAgent，把 suite.cases 里的失败聚类成 BugReport。

    没有失败时直接返回空 TriageOutput，不调 LLM。
    """
    # --- 规则阶段 ---
    clusters = cluster_failures(suite)
    initial_failure_count = sum(c.size for c in clusters)

    if not clusters:
        # 没有失败，免去一切工作
        return TriageResult(
            output=TriageOutput(
                plan_id=suite.plan_id,
                sandbox=suite.sandbox,
                total_failures=0,
                total_bugs=0,
                bugs=[],
            ),
            stats=AgentRunStats(steps=0, stopped_reason="no_failures"),
            finalized_by_agent=True,
        )

    ctx = TriageContext(suite=suite, clusters=clusters)
    tools = ToolRegistry()
    tools.register_many(build_triage_tools(ctx))

    loop = AgentLoop(
        client=llm,
        tools=tools,
        agent_name="TriageAgent",
        system_prompt=SYSTEM_PROMPT,
        max_steps=max_steps,
        tool_choice=tool_choice,
        stop_when=lambda r: r.ok and r.tool_name == "finalize",
    )
    loop.add_user_message(_build_user_message(suite, clusters))

    stats = loop.run()

    # 记录 LLM 用量
    output = TriageOutput(
        plan_id=suite.plan_id,
        sandbox=suite.sandbox,
        total_failures=initial_failure_count,
        total_bugs=len(ctx.bugs),
        bugs=list(ctx.bugs),
        llm_tokens=llm.used_tokens,
        llm_cost_usd=llm.used_usd,
    )
    return TriageResult(
        output=output,
        stats=stats,
        finalized_by_agent=ctx.finalized,
    )

def _build_user_message(
    suite: TestSuiteResult, clusters: list
) -> str:
    return (
        f"## 待 triage 的测试套件\n"
        f"- plan_id: `{suite.plan_id}`\n"
        f"- sandbox: `{suite.sandbox}`\n"
        f"- 总用例 {suite.total} / 通过 {suite.passed} / "
        f"**失败 {suite.failed}** / **错误 {suite.errored}**\n\n"
        f"规则阶段已经把失败聚成 {len(clusters)} 个候选簇。\n"
        f"请：\n"
        f"1. `list_failures` 看候选簇全貌；\n"
        f"2. 对每个 cluster `inspect_cluster`（或孤例直接 emit）；\n"
        f"3. 必要时 `read_trace_tail` 看 trace；\n"
        f"4. 用 `emit_bug_report` 拼装 bug 单（每簇一次）；\n"
        f"5. 全部完成后 `finalize`。\n"
        f"\n目标：产出 {len(clusters)} 条左右的 BugReport（除非你 merge 了某些簇）。"
    )

# --------------------------------------------------------------------------- #
# 便捷入口：从 suite.json 文件直接 triage
# --------------------------------------------------------------------------- #

def run_triage_from_json(
    suite_json_path: str,
    llm: LLMClient,
    *,
    max_steps: int = 30,
) -> TriageResult:
    """从 ``runner.run_plan(suite_json_path=...)`` 落盘的 JSON 复活 suite，
    然后跑 triage。供 ``gameguard triage --suite suite.json`` 子命令使用。
    """
    from gameguard.testcase.runner import load_suite_from_json

    suite = load_suite_from_json(suite_json_path)
    return run_triage_agent(suite=suite, llm=llm, max_steps=max_steps)
