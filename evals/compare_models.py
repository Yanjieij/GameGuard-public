"""LLM 模型对比实验（Stage 3）。

跑同一份 DesignDoc + TestGen eval 在多个 provider 上，输出对比数据。

跑法：
    python -m evals.compare_models --runs 3
    python -m evals.compare_models --runs 1 --models deepseek,glm-5.1
    python -m evals.compare_models --runs 1 --models gpt-4.1      # 单跑 OpenAI

输出：
    evals/compare_models/results.md
    EVAL.md 的一个新 section（被 rollup.py 自动拼进去）

环境变量 / API keys（按 --models 选择按需配置 .env）：
    DEEPSEEK_API_KEY   ← deepseek
    ZAI_API_KEY        ← glm-4.6 / glm-4.7 / glm-5.1
    OPENAI_API_KEY     ← gpt-4.1
    （Gemini 曾在池中，已下线；见 EVAL.md 负面结果章节）

注意：
- 每个 provider 的 LLM cache 是独立的（cache key 包含 model 字符串），
  所以即使 DeepSeek 已跑过缓存，切 GLM 还会真调 API
- GLM-4.7 / GLM-5.1 默认开启 disable_thinking；其他模型不开
- gpt-4.1 非推理型，不需要 disable_thinking
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from evals.common import confirm_real_run
from evals.design_doc.eval_design_doc import DEFAULT_CASE_NAMES, EVAL_CASES, load_golden, score_bundle
from evals.test_gen.eval_test_gen import (
    ALL_BUG_IDS,
    evaluate_plan,
)
from gameguard.agents.design_doc import run_design_doc_agent
from gameguard.agents.test_gen import run_test_gen_agent
from gameguard.llm.client import LLMClient
from gameguard.sandbox.pysim.factory import default_characters
from gameguard.sandbox.pysim.v1 import build_skill_book


@dataclass
class ModelSpec:
    """一个参赛 provider 的定义。"""

    name: str              # 展示用，例 "deepseek"
    model: str             # LiteLLM model 字符串，例 "deepseek/deepseek-chat"
    disable_thinking: bool = False
    temperature: float = 0.0  # 0=默认确定性；部分模型只支持默认值1.0
    reasoning_effort: str | None = None  # OpenAI reasoning effort: "none" 关闭思考
    thinking_mode: str | None = None  # DeepSeek V4: "non-thinking" | "thinking" | "thinking_max"
    note: str = ""         # 备注，例 "推理型，默认 disable_thinking"


REGISTRY = {
    "deepseek": ModelSpec(
        name="DeepSeek-chat",
        model="deepseek/deepseek-chat",
        disable_thinking=False,
        note="非推理型，tool-calling 最稳",
    ),
    # DeepSeek V4 (2026-04-24 发布) 当前 API 过渡期：
    #   不带 disable_thinking → API 路由到旧的 deepseek-reasoner 端点
    #   → reasoner 不支持 tool_choice → BadRequestError。
    #   已尝试过 4 种方案（见 2026-04-26 实验记录）：
    #     1. disable_thinking=False                         → ❌
    #     2. + thinking_mode="non-thinking" (extra_body)    → ❌
    #     3. 换用 deepseek/ provider 绕过 _PROVIDER_MAP     → ❌
    #     4. 原生 V4 参数 thinking=enabled + reasoning_effort=high → ❌
    #   全部失败，根因在 DeepSeek API 端——模型名路由到 reasoner。
    #   disable_thinking=True 发 GLM 语法的 thinking: {type:"disabled"}
    #   被 DeepSeek API 兼容识别 → 路由到 chat 变体 → 100% DD / 80% TG。
    #   等 2026-07-24 旧端点停用后，可切为 reasoning_effort="high"
    #   + thinking_mode="thinking"（原生参数，基础设施已就绪）。
    "deepseek-v4-flash": ModelSpec(
        name="DeepSeek-V4-Flash",
        model="deepseek-v4/deepseek-v4-flash",
        disable_thinking=True,
        note="V4 快速档；API 过渡期必须 disable_thinking，否则路由到 reasoner",
    ),
    "deepseek-v4-pro": ModelSpec(
        name="DeepSeek-V4-Pro",
        model="deepseek-v4/deepseek-v4-pro",
        disable_thinking=True,
        note="V4 高质量档；API 过渡期必须 disable_thinking，否则路由到 reasoner",
    ),
    "glm-4.6": ModelSpec(
        name="GLM-4.6",
        model="zai/glm-4.6",
        disable_thinking=False,
        note="非推理型，智谱当家",
    ),
    "glm-4.7": ModelSpec(
        name="GLM-4.7",
        model="zai/glm-4.7",
        disable_thinking=True,
        note="推理型，需要 disable_thinking 防静默",
    ),
    "glm-5.1": ModelSpec(
        name="GLM-5.1",
        model="zai/glm-5.1",
        disable_thinking=False,
        note="Z.AI GLM-5.1 推理型；开推理后 v1 pass 77.8%→100%，Token 57k→30k",
    ),
    "gpt-4.1": ModelSpec(
        name="GPT-4.1",
        model="openai/gpt-4.1",
        disable_thinking=False,
        note="OpenAI 2025 年 tool-use 优化款，function-calling 原产地",
    ),
    "gpt-5.4": ModelSpec(
        name="GPT-5.4",
        model="openai/gpt-5.4",
        disable_thinking=False,
        note="OpenAI flagship model for complex reasoning and coding",
    ),
    "gpt-5.5": ModelSpec(
        name="GPT-5.5",
        model="openai/gpt-5.5",
        disable_thinking=False,
        temperature=1.0,
        reasoning_effort="none",
        note="OpenAI latest flagship; reasoning_effort=none 关闭思考以稳定 tool-calling",
    ),
    "mimo-v2.5-pro": ModelSpec(
        name="MiMo-V2.5-Pro",
        model="mimo/mimo-v2.5-pro",
        disable_thinking=False,
        note="小米 MiMo，Token Plan 接入",
    ),
    # Gemini 2.5 Flash / Pro 已从 REGISTRY 下线：
    #   - LiteLLM 1.83 未翻译 tool_choice="required" 到 FunctionCallingConfig.mode="ANY"
    #   - 我们曾绕开 LiteLLM 直调 google-genai SDK（gemini_native.py），
    #     但 mode="ANY" 又触发 Gemini 单轮 28 并发 tool_call 且不收敛 finalize
    #     的 infinite emit，几秒内撞 1M tokens/min 限流
    #   - 问题在 Gemini 侧，边际价值低于维护成本 → 整体下线
    #   - 详见 EVAL.md "负面结果 · Gemini 调研" 小节
}


@dataclass
class RunRecord:
    """单次 (provider, eval) 的测量结果。"""

    provider: str
    eval_name: str          # "design_doc" | "test_gen"
    recall: float = 0.0
    precision: float = 0.0  # 对 test_gen 是 v1 pass rate
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    wall_seconds: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# --- LLMClient 构造（直接构造不走 from_env，方便 per-model 配置） -------------

def _make_client_for_model(spec: ModelSpec, session_id: str):
    """构造一个走 LiteLLM 网关的 LLMClient。

    参赛池目前是 DeepSeek + GLM 系列（非推理 + 推理混合）；Gemini 曾在池中，
    因协议适配问题下线（见 REGISTRY 注释）。
    """
    from gameguard.llm.cache import LLMCache
    from gameguard.llm.client import _resolve_provider
    from gameguard.llm.trace import LLMTrace

    trace_dir = Path("artifacts/traces/compare_models")
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{session_id}.jsonl"

    cache_dir = Path(os.environ.get("GAMEGUARD_CACHE_DIR", ".cache/llm"))
    cache = LLMCache(root=cache_dir, strict=False)
    trace = LLMTrace(path=trace_path, session_id=session_id)

    resolved_model, extra = _resolve_provider(spec.model)
    # _PROVIDER_MAP 途径的 model（当前仅 deepseek-v4）：
    # reasoning_effort + thinking 须放入 extra_body，因为 openai/ 兼容路由
    # 会拒绝顶层参数。OpenAI 原生（GPT-5.5）不触发此分支，走顶层参数。
    #
    # 注：DeepSeek V4 目前 disable_thinking=True 即可工作（GLM 兼容参数），
    # thinking_mode + reasoning_effort 是预留的原生切换路径，
    # 待 2026-07 旧端点停用后启用。
    is_custom_provider = resolved_model.startswith("openai/") and resolved_model != spec.model
    if is_custom_provider:
        existing_body = extra.get("extra_body") or {}
        if spec.reasoning_effort is not None:
            existing_body["reasoning_effort"] = spec.reasoning_effort
        if spec.thinking_mode is not None:
            if spec.thinking_mode == "non-thinking":
                existing_body["thinking"] = {"type": "disabled"}
            else:
                existing_body["thinking"] = {"type": "enabled"}
        if existing_body:
            extra["extra_body"] = existing_body
    else:
        if spec.reasoning_effort is not None:
            extra["reasoning_effort"] = spec.reasoning_effort
    return LLMClient(
        model=resolved_model,
        cache=cache,
        trace=trace,
        usd_budget=None,
        token_budget=None,
        default_agent=session_id,
        temperature=spec.temperature,
        extra_kwargs=extra,
        disable_thinking=spec.disable_thinking,
    )


def _usage_from_client(client) -> tuple[int, int, float]:
    """从 client 的累计统计里取 (tokens_in, tokens_out, usd)。"""
    used_tokens = getattr(client, "used_tokens", 0)
    used_usd = getattr(client, "used_usd", 0.0)
    return 0, used_tokens, used_usd


# --- 单次 run --------------------------------------------------------------

def run_design_doc_once(spec: ModelSpec, run_index: int) -> RunRecord:
    """跑一次 DesignDoc suite on 指定 model，回一份 RunRecord。"""
    session_id = f"cmp-design-{spec.name.lower().replace('.','_')}-r{run_index}"
    client = _make_client_for_model(spec, session_id)
    record = RunRecord(provider=spec.name, eval_name="design_doc")

    t0 = time.perf_counter()
    try:
        total_required = 0
        total_hit_required = 0
        total_accepted = 0
        total_extracted = 0
        total_steps = 0
        case_scores: dict[str, dict[str, Any]] = {}

        for case_name in DEFAULT_CASE_NAMES:
            case = EVAL_CASES[case_name]
            required, optional = load_golden(case.golden_path)
            result = run_design_doc_agent(doc_paths=[case.doc_path], llm=client)
            scored = score_bundle(result.bundle, required, optional)
            total_required += scored["required_count"]
            total_hit_required += scored["hit_required_count"]
            total_accepted += scored["accepted_count"]
            total_extracted += scored["total_extracted"]
            total_steps += result.stats.steps
            case_scores[case_name] = {
                "recall": scored["recall"],
                "precision": scored["precision"],
                "hit_required": scored["hit_required_count"],
                "required": scored["required_count"],
                "missed": scored["missed"],
                "novel": scored["novel"],
            }

        record.wall_seconds = time.perf_counter() - t0
        record.recall = (
            total_hit_required / total_required if total_required else 0.0
        )
        record.precision = (
            total_accepted / total_extracted if total_extracted else 0.0
        )
        record.steps = total_steps
        record.extras = {
            "cases": case_scores,
            "total_extracted": total_extracted,
            "hit_required": total_hit_required,
            "required": total_required,
            "missed_count": sum(
                len(case["missed"]) for case in case_scores.values()
            ),
        }
    except Exception as e:
        record.wall_seconds = time.perf_counter() - t0
        record.error = f"{type(e).__name__}: {e}"

    record.tokens_in, record.tokens_out, record.usd = _usage_from_client(client)
    return record


def run_test_gen_once(spec: ModelSpec, run_index: int) -> RunRecord:
    """跑一次 DesignDoc → TestGen 流水线，评测 TestGen 产物的 v2 召回。"""
    session_id = f"cmp-testgen-{spec.name.lower().replace('.','_')}-r{run_index}"
    client = _make_client_for_model(spec, session_id)
    record = RunRecord(provider=spec.name, eval_name="test_gen")

    t0 = time.perf_counter()
    try:
        dd_result = run_design_doc_agent(
            doc_paths=[Path("docs/example_skill_v1.md")],
            llm=client,
        )
        tg_result = run_test_gen_agent(
            bundle=dd_result.bundle,
            skill_book=build_skill_book(),
            initial_characters=default_characters(),
            llm=client,
            plan_id=session_id,
        )
        record.wall_seconds = time.perf_counter() - t0
        # 跑 plan 在 v1 / v2 上
        stats = evaluate_plan(tg_result.plan, f"cmp-{session_id}")
        v1 = stats["pysim:v1"]
        v2 = stats["pysim:v2"]
        record.recall = len(v2.caught_bugs) / len(ALL_BUG_IDS)
        record.precision = v1.n_passed / v1.n_cases if v1.n_cases else 0.0
        record.steps = tg_result.stats.steps
        record.extras = {
            "n_cases": v1.n_cases,
            "v1_passed": v1.n_passed,
            "v2_caught_bugs": sorted(v2.caught_bugs),
        }
    except Exception as e:
        record.wall_seconds = time.perf_counter() - t0
        record.error = f"{type(e).__name__}: {e}"

    record.tokens_in, record.tokens_out, record.usd = _usage_from_client(client)
    return record


# --- 主流程 -------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1,
                        help="每个 provider 跑几次（temperature=0，主要看 variance / token 波动）")
    parser.add_argument("--models", type=str, default="deepseek,glm-4.6",
                        help="要对比的 provider 名，逗号分隔。可选：deepseek,glm-4.6,glm-4.7")
    parser.add_argument("--evals", type=str, default="design_doc,test_gen")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=str, default="evals/compare_models/results.md")
    args = parser.parse_args()

    model_names = [m.strip() for m in args.models.split(",") if m.strip()]
    eval_names = [e.strip() for e in args.evals.split(",") if e.strip()]

    for m in model_names:
        if m not in REGISTRY:
            print(f"未知 provider {m!r}。可选：{', '.join(REGISTRY)}")
            return 1

    specs = [REGISTRY[m] for m in model_names]
    print("[cmp] 对比配置：")
    for s in specs:
        thinking = "disable_thinking=ON" if s.disable_thinking else ""
        print(f"  - {s.name}  ({s.model})  {thinking}")
    print(f"  evals: {eval_names}")
    print(f"  runs : {args.runs}")

    # 成本估算：每次 DesignDoc ~$0.05, 每次 TestGen ~$0.10（DeepSeek）
    # GLM 可能 2-5× 贵。保守估 $0.15/run.
    n_total = len(specs) * len(eval_names) * args.runs
    est_usd = n_total * 0.15
    print(f"\n估计 {n_total} 个 run，约 ${est_usd:.2f}")

    if args.dry_run:
        return 0

    confirm_real_run(est_usd, f"跑 {n_total} 个模型对比 run")

    records: list[RunRecord] = []
    for spec in specs:
        for eval_name in eval_names:
            for run_idx in range(1, args.runs + 1):
                print(f"\n[{spec.name}] {eval_name} run {run_idx}/{args.runs}...")
                if eval_name == "design_doc":
                    rec = run_design_doc_once(spec, run_idx)
                elif eval_name == "test_gen":
                    rec = run_test_gen_once(spec, run_idx)
                else:
                    print(f"  ⚠ 未知 eval_name: {eval_name}")
                    continue
                records.append(rec)
                if rec.error:
                    print(f"  ✗ 失败: {rec.error}")
                else:
                    print(
                        f"  recall={rec.recall:.2%} precision={rec.precision:.2%} "
                        f"steps={rec.steps} tokens={rec.tokens_out:,} "
                        f"usd=${rec.usd:.4f} wall={rec.wall_seconds:.1f}s"
                    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render(records, specs), encoding="utf-8")
    print(f"\n[cmp] 结果已写入 {out_path}")
    return 0


def _render(records: list[RunRecord], specs: list[ModelSpec]) -> str:
    """渲染对比 markdown。"""
    # 按 (provider, eval_name) 分组取平均
    grouped: dict[tuple[str, str], list[RunRecord]] = {}
    for r in records:
        grouped.setdefault((r.provider, r.eval_name), []).append(r)

    lines = [
        "# LLM Provider 对比实验",
        "",
        f"- 参赛 provider：{len(specs)} 个",
        "- 评估任务：design_doc + test_gen",
        f"- 总 run 数：{len(records)}",
        "",
        "## 参赛 provider 说明",
        "",
        "| Provider | Model | disable_thinking | 备注 |",
        "|---|---|---|---|",
    ]
    for s in specs:
        lines.append(
            f"| {s.name} | `{s.model}` | {'✓' if s.disable_thinking else '—'} | {s.note} |"
        )
    lines.append("")

    # --- DesignDoc 对比 ---
    lines.append("## DesignDoc 任务对比")
    lines.append("")
    lines.append(
        "| Provider | recall | precision | steps | tokens | USD | wall (s) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")

    for s in specs:
        records_for_this = grouped.get((s.name, "design_doc"), [])
        if not records_for_this:
            lines.append(f"| {s.name} | — | — | — | — | — | — |")
            continue
        valid = [r for r in records_for_this if not r.error]
        if not valid:
            err = records_for_this[0].error or "unknown error"
            lines.append(f"| {s.name} | ERROR | — | — | — | — | {err[:40]} |")
            continue
        n = len(valid)
        avg_recall = sum(r.recall for r in valid) / n
        avg_prec = sum(r.precision for r in valid) / n
        avg_steps = sum(r.steps for r in valid) / n
        avg_tokens = sum(r.tokens_out for r in valid) / n
        avg_usd = sum(r.usd for r in valid) / n
        avg_wall = sum(r.wall_seconds for r in valid) / n
        lines.append(
            f"| {s.name} | {avg_recall:.2%} | {avg_prec:.2%} | {avg_steps:.0f} | "
            f"{avg_tokens:,.0f} | ${avg_usd:.4f} | {avg_wall:.1f} |"
        )
    lines.append("")

    # --- TestGen 对比 ---
    lines.append("## TestGen 任务对比（含上游 DesignDoc）")
    lines.append("")
    lines.append(
        "| Provider | v2 bug recall | v1 pass% | 用例数 | steps | tokens | USD | wall (s) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")

    for s in specs:
        records_for_this = grouped.get((s.name, "test_gen"), [])
        if not records_for_this:
            lines.append(f"| {s.name} | — | — | — | — | — | — | — |")
            continue
        valid = [r for r in records_for_this if not r.error]
        if not valid:
            err = records_for_this[0].error or "unknown error"
            lines.append(f"| {s.name} | ERROR | — | — | — | — | — | {err[:40]} |")
            continue
        n = len(valid)
        avg_recall = sum(r.recall for r in valid) / n
        avg_prec = sum(r.precision for r in valid) / n
        avg_cases = sum(r.extras.get("n_cases", 0) for r in valid) / n
        avg_steps = sum(r.steps for r in valid) / n
        avg_tokens = sum(r.tokens_out for r in valid) / n
        avg_usd = sum(r.usd for r in valid) / n
        avg_wall = sum(r.wall_seconds for r in valid) / n
        lines.append(
            f"| {s.name} | {avg_recall:.2%} | {avg_prec:.2%} | {avg_cases:.1f} | "
            f"{avg_steps:.0f} | {avg_tokens:,.0f} | ${avg_usd:.4f} | {avg_wall:.1f} |"
        )
    lines.append("")

    # --- 结论 ---
    lines.append("## 结论")
    lines.append("")
    lines.append("基于上面的数字：")
    lines.append("")
    lines.append("- **性价比**：每条成功抽到的 invariant 平均花多少 USD")
    lines.append("- **稳定性**：同 provider 多次跑的 variance（当前 temperature=0，大多数情况是 0）")
    lines.append("- **绝对质量**：哪个 provider 召回最高")
    lines.append("")
    lines.append("一句话总结将根据实际数字填入（见上表）。")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
