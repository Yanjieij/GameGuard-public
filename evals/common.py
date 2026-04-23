"""各 eval 共用的 helper 函数。

主要封装三件事：

1. 统一构造 LLMClient（从 env 读 provider + 走 cache）
2. 用 mean / stdev 这类小工具描述 variance
3. 统一的 markdown 渲染（precision / recall 表格模板）
"""
from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gameguard.llm.client import LLMClient


# --- LLM client 构造 ----------------------------------------------------------

def make_llm_client(
    *,
    session_id: str,
    trace_dir: str | Path = "artifacts/traces",
    model_override: str | None = None,
) -> LLMClient:
    """给 eval 脚本一个统一的 LLMClient 构造入口。

    - 走磁盘缓存：相同请求不重复调 API
    - trace 落盘到 artifacts/traces/{session_id}.jsonl，方便事后看 LLM 怎么想的
    - model_override 允许 Stage 3（模型对比）一次性跑多个 provider
    """
    trace_dir = Path(trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{session_id}.jsonl"

    return LLMClient.from_env(
        trace_path=trace_path,
        session_id=session_id,
        model=model_override,
        default_agent=session_id,
    )


# --- 指标数据类 ---------------------------------------------------------------

@dataclass
class RunMetrics:
    """单次跑的核心数字。所有 eval 都用这个结构。"""

    # 核心指标（具体含义各 eval 自己定义）
    recall: float = 0.0
    precision: float = 0.0
    # 资源消耗
    steps: int = 0
    tokens: int = 0
    usd: float = 0.0
    wall_seconds: float = 0.0
    # 自由形式的额外信息（eval 可以往里塞自定义字段）
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AggregatedMetrics:
    """多次跑取平均后的结果。"""

    n_runs: int
    recall_mean: float
    recall_stdev: float
    precision_mean: float
    precision_stdev: float
    tokens_mean: float
    usd_mean: float
    wall_seconds_mean: float
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_runs(cls, runs: list[RunMetrics]) -> "AggregatedMetrics":
        if not runs:
            return cls(0, 0, 0, 0, 0, 0, 0, 0)
        recalls = [r.recall for r in runs]
        precisions = [r.precision for r in runs]
        return cls(
            n_runs=len(runs),
            recall_mean=statistics.mean(recalls),
            recall_stdev=statistics.stdev(recalls) if len(recalls) > 1 else 0.0,
            precision_mean=statistics.mean(precisions),
            precision_stdev=(
                statistics.stdev(precisions) if len(precisions) > 1 else 0.0
            ),
            tokens_mean=statistics.mean(r.tokens for r in runs),
            usd_mean=statistics.mean(r.usd for r in runs),
            wall_seconds_mean=statistics.mean(r.wall_seconds for r in runs),
        )


# --- Markdown 渲染 ------------------------------------------------------------

def render_metrics_table(title: str, runs: list[RunMetrics]) -> str:
    """把 N 次跑渲染成一张 Markdown 表 + 一行汇总。"""
    lines = [
        f"### {title}",
        "",
        "| # | recall | precision | steps | tokens | USD | wall (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(runs, 1):
        lines.append(
            f"| {i} | {r.recall:.2%} | {r.precision:.2%} | {r.steps} | "
            f"{r.tokens:,} | ${r.usd:.4f} | {r.wall_seconds:.1f} |"
        )
    agg = AggregatedMetrics.from_runs(runs)
    lines.append(
        f"| **mean** | **{agg.recall_mean:.2%}** "
        f"(σ={agg.recall_stdev:.2%}) | "
        f"**{agg.precision_mean:.2%}** (σ={agg.precision_stdev:.2%}) | — | "
        f"{agg.tokens_mean:,.0f} | ${agg.usd_mean:.4f} | "
        f"{agg.wall_seconds_mean:.1f} |"
    )
    return "\n".join(lines) + "\n"


# --- 成本保护：真跑前提醒 + 等 3 秒 -----------------------------------------

def confirm_real_run(estimated_usd: float, operation: str) -> None:
    """真跑 LLM 前打印成本估算，让用户有 3 秒反应。

    如果 GAMEGUARD_DETERMINISTIC=1（缓存必须命中模式），跳过提醒。
    """
    if os.environ.get("GAMEGUARD_DETERMINISTIC", "0") == "1":
        return
    print(f"\n[eval] 即将 {operation}，估计花费 ~${estimated_usd:.2f}")
    print("[eval] 3 秒后开始。Ctrl+C 可中止。")
    for i in range(3, 0, -1):
        print(f"  {i}...", end="", flush=True)
        time.sleep(1)
    print(" 开始")
