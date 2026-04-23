"""LLM 调用的 JSONL Trace。

为什么 Agent 要自带 trace？

每一次 LLM 调用都是"钱 × 随机性"——光看最终产物无法判断 Agent 是否
在原地打转、tool 是否被正确选中、token 花在什么地方。

业界在 agent eval / 可观测性上的常见做法（LangSmith / Langfuse / Phoenix
/ Braintrust）都在做同一件事：把每个 step 以结构化 JSON 落地，支持：

  - 离线回看 "LLM 到底看到了什么 / 回了什么"
  - 统计 token 与成本
  - 跨版本 diff：prompt 改了一个字，看 tool 选择是否变化

我们不直接依赖这些付费平台，但采用兼容它们的 schema——将来想接入
时一行 `--exporter langfuse` 即可。LiteLLM 本身就暴露 OpenTelemetry
GenAI 语义的 callbacks，我们预留一个 ``emit_otel`` 钩子。

输出格式

每一行一个 JSON 对象，对应 LLM 的一次 completion 或 tool 执行：

  {"ts": "...", "session": "...", "agent": "DesignDocAgent",
   "step": 3, "event": "llm_response",
   "model": "...", "prompt_tokens": 123, "completion_tokens": 456,
   "cost_usd": 0.002,
   "tool_calls": [...], "content": "..."}

JSONL 的选择理由和 sandbox trace 一致（见 testcase/runner.py 顶部注释）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

@dataclass
class LLMTrace:
    """单个 session 一个 LLMTrace 实例即可；agent 共享同一份，互不抢占。"""

    path: Path
    session_id: str
    # 调用计数器（便于 LangSmith 风格的 step index）
    _step: int = 0
    # 运行期累加的 token/cost，暴露给 budget guard
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    # 订阅 hook，便于接第三方（Langfuse/Phoenix 等）
    _subscribers: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 新建文件或追加都行；接一个全新 session 时通常会传新路径
        self.path.touch(exist_ok=True)

    # ---- 发射事件 -----------------------------------------------------------

    def emit(self, event: str, agent: str, **fields: Any) -> int:
        """写一行 trace。返回 step 编号。"""
        self._step += 1
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session": self.session_id,
            "agent": agent,
            "step": self._step,
            "event": event,
        }
        record.update(fields)

        # token/cost 自增，便于 AgentLoop 查余额
        self.prompt_tokens += int(fields.get("prompt_tokens") or 0)
        self.completion_tokens += int(fields.get("completion_tokens") or 0)
        self.cost_usd += float(fields.get("cost_usd") or 0.0)

        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")

        for cb in self._subscribers:
            try:
                cb(record)
            except Exception:  # noqa: BLE001  订阅者问题不能拖垮 Agent
                if os.environ.get("GAMEGUARD_DEBUG_LLM") == "1":
                    raise
        return self._step

    def subscribe(self, callback) -> None:
        """订阅 trace 事件。回调签名为 ``(record: dict) -> None``。

        预留接口：D12 时如果要接 Langfuse，实现一个 adapter 往这里 subscribe。
        """
        self._subscribers.append(callback)

    # ---- 统计 ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "steps": self._step,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_usd_running": round(self.cost_usd, 6),
            "trace_path": str(self.path),
        }
