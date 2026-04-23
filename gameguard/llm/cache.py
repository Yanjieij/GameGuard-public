"""LLM 响应的磁盘缓存（content-addressed）。

为什么必须有缓存？

GameGuard 里 LLM 调用的典型模式是：
  - 读同一份策划文档
  - 发同一组 tool schema
  - 在 deterministic 模式下 temperature=0

在这种设定下，理论上同一输入 => 同一输出。没有缓存意味着：
  1) CI 每次跑都重新调 API，钱白花
  2) 每次回归报告不完全一致（LLM 仍有细微抖动），破坏 "可重放"
  3) 本地调 agent 调了半天，改一行代码就把所有响应都再跑一遍

所以缓存不是"性能优化"——它是让 Agent 系统像引擎一样可回放的必要
基础设施。LangSmith / Langfuse / Braintrust 这些产品级 eval 平台里都把
"deterministic replay" 列为核心能力。

实现要点

- content-addressed：缓存 key = hash(model + messages + tools + temperature + ...)，
  和时间、session 无关；这样不同次 run 对同一请求自然命中。
- 只缓存 deterministic 调用：temperature > 0 的调用直接跳过缓存，
  否则缓存会把 "故意的随机性" 固化，违反设计意图。
- 写 before 读 after 的原子性：先写 `.tmp`，再 `os.replace`。避免并发
  写坏缓存文件。
- miss-when-required 模式：`GAMEGUARD_DETERMINISTIC=1` 时，若 key miss
  就抛 `CacheMissInStrictMode`。这是面试的一个加分点：生产 CI 永远不应
  在 determiniistic 模式下意外花钱。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

class CacheMissInStrictMode(RuntimeError):
    """determiniistic 模式下发生缓存未命中。"""

@dataclass
class LLMCache:
    """一个朴素的磁盘 KV 缓存，key = content hash, value = JSON 响应。

    目录结构：
        <root>/<first-2-hex>/<full-hex>.json
    分两级是为了避免单目录下文件过多（MacOS HFS+ / ext4 在 >10k 文件的
    单目录下寻址会变慢）。
    """

    root: Path
    strict: bool = False
    # 运行时统计，暴露给 AgentLoop 以便打印"cache 命中率"
    hits: int = 0
    misses: int = 0
    bypassed: int = 0   # 因为 temperature>0 而被跳过的调用数
    _seen_keys: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 核心 API -----------------------------------------------------------

    def make_key(self, payload: dict[str, Any]) -> str:
        """把 LLM 请求 dict 规范化 + 哈希。

        规范化点：
          - 所有 dict 按 key 排序（``sort_keys=True``）
          - 用 utf-8 + no-ascii-escape 一致性编码
        """
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return h

    def get(self, key: str, *, temperature: float) -> dict[str, Any] | None:
        """命中则返回响应 dict；未命中返回 None。

        temperature > 0 时视为 "显式要求随机性"，跳过缓存并计数。
        """
        self._seen_keys.add(key)
        if temperature > 0:
            self.bypassed += 1
            return None
        path = self._path_for(key)
        if not path.exists():
            self.misses += 1
            if self.strict:
                raise CacheMissInStrictMode(
                    f"deterministic 模式但缓存未命中 key={key[:12]}…\n"
                    f"（禁用 deterministic 或手动 warm cache 后重试）"
                )
            return None
        self.hits += 1
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 缓存文件坏了就当作 miss，方便自愈
            self.misses += 1
            if self.strict:
                raise
            return None

    def put(self, key: str, value: dict[str, Any], *, temperature: float) -> None:
        """只缓存 deterministic 请求。"""
        if temperature > 0:
            return
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ---- 统计 ---------------------------------------------------------------

    @property
    def total(self) -> int:
        return self.hits + self.misses + self.bypassed

    def summary(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "bypassed_random_temperature": self.bypassed,
            "hit_rate": (self.hits / self.total) if self.total else 0.0,
            "strict": self.strict,
            "root": str(self.root),
        }

    # ---- 私有 ---------------------------------------------------------------

    def _path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"
