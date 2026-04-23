"""YAML ↔ TestCase/TestPlan 互转。

为什么用 YAML 而不是 JSON？

  1) 可读性。策划/QA 不看 Python 也能 review YAML；这符合"测试用例要
     进版本库、像代码一样 review"的行业实践（TestRail 导出格式、Xray
     JSON fields、Jepsen 的 edn 都是这条路）。
  2) 支持注释。我们用 ``ruamel.yaml`` 而不是 ``PyYAML``：前者**保留注释
     和顺序**，后者会丢。让策划在用例里写 `# 为什么加这条`，回归时
     还在，这是很宝贵的上下文。
  3) LLM 生成兼容。Claude / GPT 输出 YAML 的质量比自己臆造 DSL 好。

Pydantic 与 discriminated union 的兼容细节

``Action`` 和 ``Invariant`` 都是联合类型（union），每个分支有 ``kind``
字段作为判别器（discriminator）。Pydantic v2 的 ``TypeAdapter`` 能正确
用 ``kind`` 把字典自动还原成具体子类型，前提是：

  - YAML 里每个 action / invariant 字典必须包含 ``kind`` 字段。
  - 写 YAML 的人（或 LLM）用的是我们定义的枚举值字符串（如 ``"cast"``、
    ``"hp_nonneg"``）。

这也是为什么我们在 domain/action.py 和 domain/invariant.py 里，给每个
分支都显式写了 ``kind: Literal["..."] = "..."``：让联合类型对
judgmental parser 友好，写 YAML 的时候可省略。
"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from ruamel.yaml import YAML

from gameguard.testcase.model import (
    Assertion,
    AssertionWhen,
    TestCase,
    TestPlan,
    TestStrategy,
)

# 全局一个 YAML 实例即可——它是有状态的（保留顺序/注释的状态），但对
# 我们的 dump/load 用法是安全的。
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.default_flow_style = False
# 避免 PyYAML 风格的 "..." 结尾
_yaml.explicit_end = False

# Pydantic 的 TypeAdapter：把 dict 转成 TestPlan/TestCase 时，负责处理
# 我们的 discriminated union。TypeAdapter 是 Pydantic v2 推荐的"非模型
# 类型的反序列化入口"，比 model_validate 更灵活。
_plan_adapter: TypeAdapter[TestPlan] = TypeAdapter(TestPlan)
_case_adapter: TypeAdapter[TestCase] = TypeAdapter(TestCase)

# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def load_plan_from_yaml(path: str | Path) -> TestPlan:
    """从磁盘加载一个 TestPlan。

    YAML 的顶层形状有两种被接受：
      1) 直接就是 TestPlan（有 ``id`` / ``cases`` 字段）
      2) 一个仅含 ``plan:`` 子键的包装。为了让 LLM 在某些情况下的
         "顶层一定要是 dict key" 习惯也能 work，留一个 fallback。
    """
    raw = _yaml.load(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "plan" in raw and "cases" not in raw:
        raw = raw["plan"]
    # ruamel 返回 CommentedMap/CommentedSeq，需要转普通 dict/list 才能喂 Pydantic。
    normalized = _to_plain(raw)
    return _plan_adapter.validate_python(normalized)

def load_case_from_yaml(path: str | Path) -> TestCase:
    """加载单条用例（当 YAML 的顶层是一个 TestCase 时）。"""
    raw = _yaml.load(Path(path).read_text(encoding="utf-8"))
    return _case_adapter.validate_python(_to_plain(raw))

def parse_plan(text: str) -> TestPlan:
    """从字符串（比如 LLM 返回）解析 TestPlan。"""
    raw = _yaml.load(text)
    if isinstance(raw, dict) and "plan" in raw and "cases" not in raw:
        raw = raw["plan"]
    return _plan_adapter.validate_python(_to_plain(raw))

# --------------------------------------------------------------------------- #
# 写出
# --------------------------------------------------------------------------- #

def dump_plan_to_yaml(plan: TestPlan, path: str | Path) -> None:
    """把 TestPlan 持久化到磁盘。

    ``model_dump(mode='json')`` 的作用：
      - Enum -> str
      - 其它不可直接 YAML 化的类型（如 datetime）-> 字符串
    之后由 ruamel 负责最终格式化（缩进/换行）。
    """
    buf = StringIO()
    _yaml.dump(plan.model_dump(mode="json"), buf)
    Path(path).write_text(buf.getvalue(), encoding="utf-8")

def dump_plan_to_str(plan: TestPlan) -> str:
    buf = StringIO()
    _yaml.dump(plan.model_dump(mode="json"), buf)
    return buf.getvalue()

# --------------------------------------------------------------------------- #
# 工具：方便 LLM/人工构造用例
# --------------------------------------------------------------------------- #

def make_assertion(invariant: dict | Any, when: AssertionWhen = AssertionWhen.END_OF_RUN) -> Assertion:
    """辅助：允许传字典（LLM 输出场景）或 Invariant 实例。"""
    if isinstance(invariant, dict):
        # 借用 Assertion 的 Pydantic 校验来自动选中联合分支
        return Assertion.model_validate({"invariant": invariant, "when": when})
    return Assertion(invariant=invariant, when=when)

# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _to_plain(obj: Any) -> Any:
    """把 ruamel 的 CommentedMap / CommentedSeq 递归转成普通 dict/list。

    Pydantic v2 对 Mapping/Sequence 协议的实现已经相当宽容，
    但保险起见我们在边界处显式转换，避免少见的校验失败。
    """
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj

__all__ = [
    "dump_plan_to_str",
    "dump_plan_to_yaml",
    "load_case_from_yaml",
    "load_plan_from_yaml",
    "make_assertion",
    "parse_plan",
    # 下面两个为了减少调用方导入
    "TestPlan",
    "TestCase",
    "TestStrategy",
    "AssertionWhen",
]
