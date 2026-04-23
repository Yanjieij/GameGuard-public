"""GameGuard 命令行入口。

本文件在整个系统的位置

    cli.py   ← 用户入口 / CI 入口 / demo 入口
       │
       ▼
    testcase.loader  ← 读 YAML
    testcase.runner  ← 跑 Plan
    reports.markdown ← 渲染报告

CLI 不持有任何"业务逻辑"：它只做参数解析、IO 编排、退出码映射。
这是符合经典的 "thin controller, fat service" 模式——下沉逻辑到可测试
的库层，CLI 层只负责黏合和界面。

为什么选 typer

  - 基于 Python 类型注解，命令行参数从函数签名自动生成，省掉 argparse
    的样板代码。
  - 天然支持子命令（run / regress / repro ...），D9 做回归子命令时
    添加成本极低。
  - 和 rich 天生集成，彩色输出 + 进度条开箱即用。

如果不熟 typer：一条 `@app.command()` 装饰的函数就是一个子命令，
形参 = 参数 + 选项，Annotated[... , typer.Option/Argument] 提供元信息。

沙箱选择

TestCase 的 `sandbox` 字段字符串格式为 `<adapter>:<version>`，例：
  pysim:v1 / pysim:v2 / questsim:v1-harbor / unity:mock / unity:headless[+<backend>]
本文件里的 ``resolve_sandbox_factory`` 把这一层字符串路由到对应的
Python 工厂函数。当我们要接入真 Unity 时，只需换 ``unity:headless`` 背后
的 gRPC server 实现，Python 侧代码零改动——这就是计划文档里的
"Adapter 抽象隔离沙箱" 原则。
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gameguard.reports.markdown import write_bug_reports, write_suite_report
from gameguard.reports.schema import SuiteReport
from gameguard.sandbox.adapter import GameAdapter
from gameguard.sandbox.pysim.factory import default_characters, make_sandbox
from gameguard.sandbox.pysim.v1 import build_buff_book, build_skill_book
from gameguard.testcase.loader import dump_plan_to_yaml, load_plan_from_yaml
from gameguard.testcase.runner import load_suite_from_json, run_plan

app = typer.Typer(
    add_completion=False,
    help="LLM Agent 驱动的 Unity 游戏自动化测试系统。",
    no_args_is_help=True,
)
console = Console()

# --------------------------------------------------------------------------- #
# 沙箱路由
# --------------------------------------------------------------------------- #

def resolve_sandbox_factory(spec: str) -> GameAdapter:
    """把形如 "pysim:v1" 的字符串解析成具体 GameAdapter 实例。

    TestCase.sandbox 字段最终都会经过这里；这是唯一的路由点，扩展
    新 adapter 时只改这里。

    支持的字符串：
      - ``pysim:v1`` / ``pysim:v2`` —— Python 技能系统沙箱
      - ``questsim:v1`` / ``questsim:v1-harbor`` —— 任务 + 3D 场景沙箱
      - ``unity:mock`` —— 预录 trace 回放（D11；不需要 server）
      - ``unity:headless`` —— 真 gRPC 连 mock server，默认后端 pysim:v1
      - ``unity:headless+pysim`` / ``unity:headless+questsim`` —— 指定 backend
      - ``unity:headless+pysim:v2`` —— 指定 backend 版本
      端点由环境变量 ``GAMEGUARD_UNITY_ENDPOINT`` 控制（默认 127.0.0.1:50099）。
    """
    if ":" not in spec:
        raise typer.BadParameter(f"sandbox 规格必须是 '<adapter>:<version>' 格式，收到 {spec!r}")
    adapter, version = spec.split(":", 1)
    if adapter == "pysim":
        return make_sandbox(version)
    if adapter == "questsim":
        from gameguard.sandbox.questsim.factory import (
            make_harbor_sandbox,
            make_questsim_sandbox,
        )
        # 版本字符串中含 "-harbor" 后缀就加载 harbor scene 套件
        if version.endswith("-harbor"):
            base = version.replace("-harbor", "")
            return make_harbor_sandbox(base)
        return make_questsim_sandbox(version)
    if adapter == "unity":
        import os
        from gameguard.sandbox.unity import UnityAdapter
        if version == "mock":
            mock_path = Path("artifacts/unity_mock_trace.jsonl")
            if not mock_path.exists():
                raise typer.BadParameter(
                    f"unity:mock 模式需要预录 trace；请先生成 {mock_path}\n"
                    "（提示：可用 pysim:v1 跑一次再 cp 过来作为 mock 数据）"
                )
            return UnityAdapter.from_mock(mock_path)
        # D19：unity:headless / unity:headless+pysim / unity:headless+questsim 等。
        # "+" 后缀明示后端；无后缀默认走 pysim:v1（mock server 认这个）。
        if version.startswith("headless"):
            backend_spec = "pysim:v1"
            if "+" in version:
                _head, backend_short = version.split("+", 1)
                # backend_short 形如 "pysim" / "questsim" / "pysim:v2" / "questsim:v1-harbor"
                if ":" in backend_short:
                    backend_spec = backend_short
                else:
                    # 省略版本时给个合理默认
                    default_ver = {"pysim": "v1", "questsim": "v1"}.get(backend_short, "v1")
                    backend_spec = f"{backend_short}:{default_ver}"
            endpoint = os.environ.get("GAMEGUARD_UNITY_ENDPOINT", "127.0.0.1:50099")
            host, port_str = endpoint.split(":", 1)
            return UnityAdapter.from_endpoint(
                host=host, port=int(port_str), sandbox_spec=backend_spec
            )
        raise typer.BadParameter(
            f"unity 子版本只支持 mock / headless / headless+<backend>，收到 {version!r}"
        )
    raise typer.BadParameter(f"未知 adapter: {adapter!r}（支持 pysim, questsim, unity）")

# --------------------------------------------------------------------------- #
# 子命令：run
# --------------------------------------------------------------------------- #

@app.command("run")
def cmd_run(
    plan_path: Annotated[
        Path,
        typer.Option(
            "--plan",
            "-p",
            exists=True,
            dir_okay=False,
            readable=True,
            help="要执行的 TestPlan YAML 路径。",
        ),
    ],
    report_path: Annotated[
        Path,
        typer.Option(
            "--report",
            "-r",
            help="Markdown 报告输出路径。",
        ),
    ] = Path("artifacts/reports/suite.md"),
    artifacts_dir: Annotated[
        Path,
        typer.Option(
            "--artifacts",
            "-a",
            help="trace / snapshot 落盘根目录（默认 artifacts/）。",
        ),
    ] = Path("artifacts"),
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="只输出报告路径和退出码，不打印表格。"),
    ] = False,
    sandbox_override: Annotated[
        str | None,
        typer.Option(
            "--sandbox",
            "-s",
            help=(
                "覆盖 plan 中所有用例的 sandbox 字段。例：'pysim:v2' 把"
                "整个 plan 切到 v2 跑（差分回归常用）。不传则尊重 YAML。"
            ),
        ),
    ] = None,
    do_triage: Annotated[
        bool,
        typer.Option(
            "--triage/--no-triage",
            help=(
                "有失败时是否自动调 TriageAgent 产出 Jira-compatible bug 报告。"
                "默认 --triage（仅当有失败/错误时真的调 LLM）。CI 想跳过用 --no-triage。"
            ),
        ),
    ] = True,
    bugs_path: Annotated[
        Path,
        typer.Option(
            "--bugs",
            help="bug 报告 markdown 输出路径（仅当 --triage 且有失败时生成）。",
        ),
    ] = Path("artifacts/reports/bugs.md"),
    suite_json: Annotated[
        Path,
        typer.Option(
            "--suite-json",
            help="TestSuiteResult JSON 落盘路径（事后 `gameguard triage --suite ...` 复用）。",
        ),
    ] = Path("artifacts/suite.json"),
) -> None:
    """跑一条 TestPlan 并输出 Markdown 报告。

    退出码（与 CI 约定对齐）：
        0  —— 全部通过
        1  —— 有断言失败
        2  —— 有执行异常
    """
    plan = load_plan_from_yaml(plan_path)
    if sandbox_override:
        for case in plan.cases:
            case.sandbox = sandbox_override
        plan.id = f"{plan.id}@{sandbox_override}"

    if not quiet:
        console.print(
            Panel.fit(
                f"[bold]{plan.name or plan.id}[/]\n{plan.description.strip()}",
                title=f"[cyan]Plan {plan.id}[/] · v{plan.version}",
                border_style="cyan",
            )
        )
        console.print(
            f"共 [bold]{len(plan.cases)}[/] 条用例；"
            f"工件目录：[dim]{artifacts_dir}[/]"
        )

    # 真正的跑批；同时落盘 suite.json 供 D7 triage 复用
    suite = run_plan(
        plan,
        factory=resolve_sandbox_factory,
        artifacts_dir=artifacts_dir,
        suite_json_path=suite_json,
    )

    # 渲染套件报告
    report = SuiteReport.from_suite_result(suite)
    written = write_suite_report(report, report_path)

    if not quiet:
        _print_summary_table(suite)
        console.print(f"\n报告已写入：[green]{written}[/]")
        console.print(f"[dim]{suite.summary_line()}[/]")

    # ---- D7：自动 triage（仅在有失败 / 错误时） ----
    if do_triage and suite.has_failures:
        if not quiet:
            console.print("\n[cyan]检测到失败/错误 → 自动调 TriageAgent 产出 bug 报告...[/]")
        try:
            from dotenv import load_dotenv

            from gameguard.agents.triage import run_triage_agent
            from gameguard.llm.client import LLMClient

            load_dotenv()
            triage_trace = Path(artifacts_dir) / "traces" / f"triage-{plan.id}.jsonl"
            llm = LLMClient.from_env(
                trace_path=triage_trace,
                session_id=f"triage-{plan.id}",
                default_agent="TriageAgent",
            )
            tr = run_triage_agent(suite=suite, llm=llm)
            bugs_written = write_bug_reports(tr.output, bugs_path)
            if not quiet:
                console.print(
                    f"Bug 报告已写入：[green]{bugs_written}[/] "
                    f"({tr.output.total_bugs} 条 bug，"
                    f"steps={tr.stats.steps}, tokens={llm.used_tokens})"
                )
        except Exception as e:  # noqa: BLE001
            # triage 失败不应影响 run 的退出码，但要明确告知
            if not quiet:
                console.print(f"[yellow]⚠ TriageAgent 执行失败：{type(e).__name__}: {e}[/]")
                console.print("[yellow]  套件报告仍可用；可事后 `gameguard triage --suite ...` 重试[/]")

    # 映射退出码（不受 triage 影响）
    if suite.errored > 0:
        raise typer.Exit(code=2)
    if suite.failed > 0:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)

# --------------------------------------------------------------------------- #
# 子命令：triage —— 事后 triage（无需重跑沙箱）
# --------------------------------------------------------------------------- #

@app.command("triage")
def cmd_triage(
    suite_json: Annotated[
        Path,
        typer.Option(
            "--suite",
            "-s",
            exists=True,
            dir_okay=False,
            readable=True,
            help="之前 `gameguard run` 落盘的 suite.json 路径。",
        ),
    ],
    bugs_path: Annotated[
        Path,
        typer.Option("--bugs", help="bug 报告 markdown 输出路径。"),
    ] = Path("artifacts/reports/bugs.md"),
    artifacts_dir: Annotated[
        Path, typer.Option("--artifacts", "-a"),
    ] = Path("artifacts"),
) -> None:
    """对一个已有 suite.json 跑 TriageAgent，产出 Jira-compatible bug 报告。

    用途：失败已经发生（CI 跑过了），事后想换个 LLM / 调 prompt 重新 triage
    时不必重跑沙箱。
    """
    from dotenv import load_dotenv

    from gameguard.agents.triage import run_triage_from_json
    from gameguard.llm.client import LLMClient

    load_dotenv()
    suite = load_suite_from_json(suite_json)
    console.print(
        Panel.fit(
            f"[bold]suite[/]：{suite_json}\n"
            f"failed={suite.failed} · errored={suite.errored} · sandbox=`{suite.sandbox}`",
            title="[cyan]gameguard triage[/]",
            border_style="cyan",
        )
    )

    if not suite.has_failures:
        console.print("[green]suite 全过，无需 triage。[/]")
        raise typer.Exit(code=0)

    triage_trace = artifacts_dir / "traces" / f"triage-{suite.plan_id}.jsonl"
    llm = LLMClient.from_env(
        trace_path=triage_trace,
        session_id=f"triage-{suite.plan_id}",
        default_agent="TriageAgent",
    )
    console.print(f"LLM: [green]{llm.model}[/]")

    tr = run_triage_from_json(str(suite_json), llm=llm)
    written = write_bug_reports(tr.output, bugs_path)
    console.print(
        f"\n[green]✓[/] 产出 [bold]{tr.output.total_bugs}[/] 条 bug "
        f"(从 {tr.output.total_failures} 条失败聚类得出)；"
        f"steps={tr.stats.steps}, tokens={llm.used_tokens}"
    )
    console.print(f"Bug 报告：[green]{written}[/]")

# --------------------------------------------------------------------------- #
# 子命令：regress —— 差分回归对比（D9）
# --------------------------------------------------------------------------- #

@app.command("regress")
def cmd_regress(
    plan_path: Annotated[
        Path,
        typer.Option("--plan", "-p", exists=True, dir_okay=False, readable=True),
    ],
    baseline: Annotated[
        str, typer.Option("--baseline", "-b", help="基线 sandbox，例 'pysim:v1'"),
    ] = "pysim:v1",
    candidate: Annotated[
        str, typer.Option("--candidate", "-c", help="候选 sandbox，例 'pysim:v2'"),
    ] = "pysim:v2",
    artifacts_dir: Annotated[
        Path, typer.Option("--artifacts", "-a"),
    ] = Path("artifacts/regress"),
    html_path: Annotated[
        Path, typer.Option("--html", help="HTML 回归报告输出路径"),
    ] = Path("artifacts/reports/regress.html"),
    do_triage: Annotated[
        bool,
        typer.Option(
            "--triage/--no-triage",
            help="对 NEW failures 自动调 TriageAgent 产出 BugReport（嵌入 HTML）",
        ),
    ] = True,
) -> None:
    """对比同一 plan 在 baseline / candidate 两个沙箱的结果，输出回归报告。

    退出码：
        0 — 没有 NEW failures（可发布）
        1 — 有 NEW failures（regression，不可发布）
        2 — 任何沙箱跑出 ERROR
    """
    from dotenv import load_dotenv

    from gameguard.reports.html import write_regress_html
    from gameguard.reports.regress import compute_regress_diff

    plan = load_plan_from_yaml(plan_path)
    console.print(
        Panel.fit(
            f"[bold]Plan[/]: {plan.id} ({len(plan.cases)} cases)\n"
            f"[bold]Baseline[/]: {baseline}\n"
            f"[bold]Candidate[/]: {candidate}",
            title="[cyan]gameguard regress[/]",
            border_style="cyan",
        )
    )

    baseline_dir = artifacts_dir / baseline.replace(":", "_")
    candidate_dir = artifacts_dir / candidate.replace(":", "_")

    # ---- 1) 跑 baseline ----
    plan_b = plan.model_copy(deep=True)
    for c in plan_b.cases:
        c.sandbox = baseline
    plan_b.id = f"{plan.id}@{baseline}"
    console.print(f"\n[bold cyan]→ baseline[/] {baseline} ...")
    suite_b = run_plan(
        plan_b,
        factory=resolve_sandbox_factory,
        artifacts_dir=baseline_dir,
        suite_json_path=baseline_dir / "suite.json",
    )
    console.print(f"  {suite_b.summary_line()}")

    # ---- 2) 跑 candidate ----
    plan_c = plan.model_copy(deep=True)
    for c in plan_c.cases:
        c.sandbox = candidate
    plan_c.id = f"{plan.id}@{candidate}"
    console.print(f"\n[bold cyan]→ candidate[/] {candidate} ...")
    suite_c = run_plan(
        plan_c,
        factory=resolve_sandbox_factory,
        artifacts_dir=candidate_dir,
        suite_json_path=candidate_dir / "suite.json",
    )
    console.print(f"  {suite_c.summary_line()}")

    # ---- 3) 计算 diff ----
    diff = compute_regress_diff(
        baseline=suite_b, candidate=suite_c, plan_id=plan.id
    )

    # ---- 4) 可选 triage（仅 NEW failures） ----
    triage_output = None
    if do_triage and diff.has_regression:
        console.print(
            f"\n[cyan]检测到 {diff.new_count} 条 NEW failures → 自动 triage...[/]"
        )
        try:
            from gameguard.agents.triage import run_triage_agent
            from gameguard.llm.client import LLMClient

            load_dotenv()
            # 把 candidate suite 里的 NEW 用例摘出来单独 triage
            new_case_ids = {e.case_id for e in diff.filter_new()}
            new_cases = [r for r in suite_c.cases if r.case_id in new_case_ids]
            from gameguard.testcase.model import TestSuiteResult
            new_only_suite = TestSuiteResult(
                plan_id=f"{plan.id}@{candidate}-NEW",
                plan_version=suite_c.plan_version,
                sandbox=candidate,
                total=len(new_cases),
                passed=sum(1 for r in new_cases if r.outcome.value == "passed"),
                failed=sum(1 for r in new_cases if r.outcome.value == "failed"),
                errored=sum(1 for r in new_cases if r.outcome.value == "error"),
                wall_time_ms=suite_c.wall_time_ms,
                cases=new_cases,
            )
            triage_trace = artifacts_dir / "traces" / f"triage-regress-{plan.id}.jsonl"
            llm = LLMClient.from_env(
                trace_path=triage_trace,
                session_id=f"regress-{plan.id}",
                default_agent="TriageAgent",
            )
            tr = run_triage_agent(suite=new_only_suite, llm=llm)
            triage_output = tr.output
            console.print(
                f"  → {tr.output.total_bugs} bugs (steps={tr.stats.steps}, "
                f"tokens={llm.used_tokens})"
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]⚠ TriageAgent 失败：{type(e).__name__}: {e}[/]")

    # ---- 5) 渲染 HTML ----
    written = write_regress_html(diff, html_path, triage=triage_output)

    # ---- 6) 打印总结 ----
    console.print(
        f"\n[bold]差分结果：[/]"
        f"NEW=[red]{diff.new_count}[/] · "
        f"FIXED=[green]{diff.fixed_count}[/] · "
        f"STABLE_PASS={diff.stable_pass_count} · "
        f"STABLE_FAIL=[yellow]{diff.stable_fail_count}[/] · "
        f"MISSING={diff.missing_count}"
    )
    console.print(f"\nHTML 报告：[green]{written}[/]")

    # 退出码
    if suite_b.errored > 0 or suite_c.errored > 0:
        raise typer.Exit(code=2)
    if diff.has_regression:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)

# --------------------------------------------------------------------------- #
# 子命令：generate —— 用 Agent 从策划文档生成 TestPlan
# --------------------------------------------------------------------------- #

@app.command("generate")
def cmd_generate(
    doc_path: Annotated[
        Path,
        typer.Option(
            "--doc",
            "-d",
            exists=True,
            dir_okay=False,
            readable=True,
            help="策划设计文档路径（Markdown）。",
        ),
    ],
    out_path: Annotated[
        Path,
        typer.Option(
            "--out",
            "-o",
            help="生成的 TestPlan YAML 输出路径。",
        ),
    ] = Path("testcases/skill_system/agent_generated.yaml"),
    plan_id: Annotated[str, typer.Option("--plan-id")] = "skill_system.agent_generated",
    plan_name: Annotated[str, typer.Option("--plan-name")] = "Agent 生成 · 技能系统回归",
    max_steps_design: Annotated[int, typer.Option("--max-steps-design")] = 25,
    max_steps_testgen: Annotated[int, typer.Option("--max-steps-testgen")] = 30,
    prefetch: Annotated[
        bool,
        typer.Option(
            "--prefetch/--no-prefetch",
            help=(
                "TestGenAgent 是否把 invariants/skills/characters 预先嵌入 user message。"
                "默认 --no-prefetch（discovery 模式，让 LLM 真的调 list_* 工具，trace 完整）。"
                "--prefetch 跳过 list_* 调用，省 token 也是 GLM-4.7 等推理型模型的 fallback。"
            ),
        ),
    ] = False,
    tool_choice: Annotated[
        str | None,
        typer.Option(
            "--tool-choice",
            help=(
                "TestGenAgent 的 tool_choice。默认 auto；'required' 强制每轮必调工具"
                "（推理型模型 workaround）。'none' 禁止调工具（不要在生产用）。"
            ),
        ),
    ] = None,
    critic: Annotated[
        bool,
        typer.Option(
            "--critic/--no-critic",
            help=(
                "是否在 TestGenAgent 后接 CriticAgent 做 plan review（D10）。"
                "Critic 会 patch/drop 那些 LLM 算错 MP/CD 的 case，"
                "代价是多一轮 LLM 调用（约 +30-50k tokens）。"
            ),
        ),
    ] = False,
) -> None:
    """跑 DesignDocAgent + TestGenAgent 的完整 plan 管线，落盘 YAML。"""
    # 延迟导入：避免不跑 generate 时也加载 LLM 相关依赖
    from dotenv import load_dotenv

    from gameguard.agents.orchestrator import run_plan_pipeline
    from gameguard.llm.client import LLMClient

    load_dotenv()

    console.print(
        Panel.fit(
            f"[bold]输入文档[/]：{doc_path}\n"
            f"[bold]输出 Plan[/]：{out_path}",
            title="[cyan]gameguard generate[/]",
            border_style="cyan",
        )
    )

    # 装配 LLM 客户端（trace 路径带上时间戳避免覆盖）
    trace_path = Path(f"artifacts/traces/agents-{plan_id}.jsonl")
    llm = LLMClient.from_env(
        trace_path=trace_path,
        session_id=plan_id,
        default_agent="Orchestrator",
    )
    console.print(
        f"LLM: [green]{llm.model}[/] · USD budget=${llm.usd_budget or 'unlimited'} · "
        f"trace=[dim]{trace_path}[/]"
    )

    # 装配沙箱上下文（只用到 SkillBook / 初始角色；不真跑沙箱）
    skill_book = build_skill_book()
    _buffs = build_buff_book()        # 让 LLM 注释里提过的 buff 名可引用
    characters = default_characters()

    console.print(
        f"TestGen 模式：[bold]{'prefetch' if prefetch else 'discovery'}[/] · "
        f"tool_choice=[bold]{tool_choice or 'auto'}[/] · "
        f"critic=[bold]{'on' if critic else 'off'}[/]"
    )

    review_hook = None
    if critic:
        from gameguard.agents.critic import make_critic_review_hook
        review_hook = make_critic_review_hook(
            skill_book=skill_book,
            initial_characters=characters,
            llm=llm,
        )

    result = run_plan_pipeline(
        doc_paths=[doc_path],
        skill_book=skill_book,
        initial_characters=characters,
        llm=llm,
        plan_id=plan_id,
        plan_name=plan_name,
        max_steps_design=max_steps_design,
        max_steps_testgen=max_steps_testgen,
        prefetch_context=prefetch,
        tool_choice=tool_choice,
        review_hook=review_hook,
    )

    # 打印简报
    t = Table(title="管线摘要", show_header=True, header_style="bold magenta")
    t.add_column("Agent", style="cyan")
    t.add_column("steps", justify="right")
    t.add_column("stopped", style="yellow")
    t.add_row(
        "DesignDocAgent",
        str(result.design_doc_stats.steps),
        result.design_doc_stats.stopped_reason,
    )
    t.add_row(
        "TestGenAgent",
        str(result.test_gen_stats.steps),
        result.test_gen_stats.stopped_reason,
    )
    console.print(t)
    console.print(
        f"不变式：[bold]{len(result.invariants.items)}[/] 条；"
        f"生成用例：[bold]{len(result.plan.cases)}[/] 条"
    )
    console.print(
        f"LLM 累计：tokens={llm.used_tokens} · cost=${llm.used_usd:.4f} · "
        f"cache {llm.cache.summary()}"
    )

    # 落盘 TestPlan YAML
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump_plan_to_yaml(result.plan, out_path)
    console.print(f"\nTestPlan 已写入：[green]{out_path}[/]")
    console.print(
        f"下一步：[bold]gameguard run --plan {out_path}[/] 把 Agent 生成的"
        f"用例跑在 pysim:v1 上验证。"
    )

# --------------------------------------------------------------------------- #
# 子命令：info
# --------------------------------------------------------------------------- #

@app.command("info")
def cmd_info() -> None:
    """列出已支持的 sandbox 与能力清单（方便面试时介绍）。"""
    t = Table(title="GameGuard 能力清单", show_header=True, header_style="bold magenta")
    t.add_column("模块", style="cyan")
    t.add_column("当前能力", style="white")
    t.add_row("Sandbox.pysim:v1", "[green]✓[/] 确定性 Python 模拟 + 4 技能 + 3 buff + 暴击（黄金实现）")
    t.add_row("Sandbox.pysim:v2", "[green]✓[/] 植入 5 类 bug（cooldown/buff/state/DoT/RNG）")
    t.add_row("Sandbox.questsim:v1", "[green]✓[/] Quest/3D/寻路/对话/物理 骨架（D12）")
    t.add_row("Sandbox.questsim:v2", "[yellow]D18 植入 5 类 Quest bug[/]")
    t.add_row("Sandbox.unity:mock", "[green]✓[/] 用预录 trace 跑 mock；接入测试免 server")
    t.add_row("Sandbox.unity:headless", "[green]✓[/] 真 gRPC 通路 ↔ mock server（D19），E2E 验证通过")
    t.add_row("TestCase YAML", "[green]✓[/] 手写与 LLM 产出共用同一数据结构")
    t.add_row("Runner", "[green]✓[/] 本地跑批 + trace/snapshot 落盘 + suite.json")
    t.add_row("Reports.markdown", "[green]✓[/] 套件级 + bug 级")
    t.add_row("Reports.html", "[green]✓[/] gameguard regress 输出含折叠 BugReport")
    t.add_row("Agents.DesignDoc", "[green]✓[/] 18 invariants from real designer doc")
    t.add_row("Agents.TestGen", "[green]✓[/] discovery + prefetch 双模式")
    t.add_row("Agents.Triage", "[green]✓[/] 两阶段聚类 + Jira-compatible BugReport")
    t.add_row("Agents.Exploratory", "[green]✓[/] 对抗式 prompt 复用 testgen 工具")
    t.add_row("Agents.Critic", "[green]✓[/] 静态校验 + LLM patch/drop 决策（D10）")
    console.print(t)

# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _print_summary_table(suite) -> None:
    """把 TestSuiteResult 渲染成 rich 表，和 reports.markdown 相互独立。"""
    t = Table(show_header=True, header_style="bold magenta", title="用例结果")
    t.add_column("ID", style="cyan", no_wrap=True)
    t.add_column("名称", style="white")
    t.add_column("结果", justify="center")
    t.add_column("ticks", justify="right")
    t.add_column("sim (s)", justify="right")
    t.add_column("wall (ms)", justify="right")
    t.add_column("失败不变式", style="red")

    style_map = {
        "passed": "[green]✅ passed[/]",
        "failed": "[red]❌ failed[/]",
        "error": "[yellow]⚠ error[/]",
        "skipped": "[dim]⏭ skipped[/]",
    }
    for c in suite.cases:
        t.add_row(
            c.case_id,
            c.case_name,
            style_map.get(c.outcome.value, c.outcome.value),
            str(c.ticks_elapsed),
            f"{c.sim_time:.2f}",
            f"{c.wall_time_ms:.1f}",
            ", ".join(ao.result.invariant_id for ao in c.failing_assertions) or "—",
        )
    console.print(t)

if __name__ == "__main__":
    app()
