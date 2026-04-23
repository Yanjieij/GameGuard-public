"""D3 meta-tests —— 测试 "测试框架" 本身。

在游戏团队里，"给测试框架写测试" 是一个容易被忽视但很关键的习惯：
当 Runner / Loader 有 bug 时，业务用例的 pass/fail 结果都不再可信。
这类 meta-test 存在的价值就是托底 "绿的真的绿，红的真的红"。

本文件覆盖 D3 层：
  - YAML 的 round-trip：dump 后再 load 得到等价对象
  - Runner 对 PASS/FAIL/ERROR 三种结果的正确分类
  - 断言 `when` 时机（EVERY_TICK vs END_OF_RUN）按预期触发
"""
from __future__ import annotations

from pathlib import Path

from gameguard.domain import CastAction, WaitAction
from gameguard.domain.invariant import (
    CooldownAtLeastAfterCastInvariant,
    HpNonnegInvariant,
)
from gameguard.sandbox.pysim.factory import make_sandbox
from gameguard.testcase.loader import (
    dump_plan_to_str,
    load_plan_from_yaml,
    parse_plan,
)
from gameguard.testcase.model import (
    Assertion,
    AssertionWhen,
    CaseOutcome,
    TestCase,
    TestPlan,
)
from gameguard.testcase.runner import run_plan


# --------------------------------------------------------------------------- #
# YAML round-trip
# --------------------------------------------------------------------------- #


def test_yaml_roundtrip_preserves_plan() -> None:
    """构造一个 Plan -> dump -> parse -> 字段完全一致。

    这条测试抓的是：
      - discriminated union 的 kind 字段能被 pydantic 正确回来
      - 枚举值（strategy / when）能正确字符串化与回转
    """
    plan = TestPlan(
        id="meta.roundtrip",
        name="round trip",
        version="0.0.1",
        cases=[
            TestCase(
                id="smoke",
                name="smoke",
                seed=1,
                actions=[
                    CastAction(actor="p1", skill="skill_fireball", target="dummy"),
                    WaitAction(seconds=1.0),
                ],
                assertions=[
                    Assertion(
                        invariant=HpNonnegInvariant(
                            id="I-01", description="", actor="dummy"
                        ),
                        when=AssertionWhen.END_OF_RUN,
                    )
                ],
            )
        ],
    )
    s = dump_plan_to_str(plan)
    plan2 = parse_plan(s)
    assert plan2.model_dump(mode="json") == plan.model_dump(mode="json")


def test_load_handwritten_yaml_from_disk(tmp_path: Path) -> None:
    """已经进 git 的 handwritten.yaml 能被加载且字段非空。"""
    repo_root = Path(__file__).resolve().parents[1]
    plan_path = repo_root / "testcases" / "skill_system" / "handwritten.yaml"
    plan = load_plan_from_yaml(plan_path)
    assert plan.id == "skill_system.handwritten"
    assert len(plan.cases) >= 8
    # 每条用例都要至少一个断言，否则算不上 "测试用例"
    for c in plan.cases:
        assert c.actions, f"{c.id} 缺少动作"
        assert c.assertions, f"{c.id} 缺少断言"


# --------------------------------------------------------------------------- #
# Runner 行为
# --------------------------------------------------------------------------- #


def _factory(spec: str):
    # 本地测试不经过 CLI 解析层，直接绕过 adapter 前缀。
    _, version = spec.split(":", 1)
    return make_sandbox(version)


def _one_case_plan(case: TestCase) -> TestPlan:
    return TestPlan(id="meta.one", cases=[case])


def test_runner_marks_passed_case(tmp_path: Path) -> None:
    case = TestCase(
        id="passed-case",
        name="p",
        sandbox="pysim:v1",
        seed=42,
        actions=[
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.0),
        ],
        assertions=[
            Assertion(
                invariant=HpNonnegInvariant(id="I-01", description="", actor="dummy"),
                when=AssertionWhen.END_OF_RUN,
            )
        ],
    )
    suite = run_plan(_one_case_plan(case), _factory, artifacts_dir=tmp_path)
    assert suite.passed == 1
    assert suite.failed == 0
    assert suite.errored == 0
    # trace 文件应当被写入 tmp 目录（而非污染真实 artifacts/）
    assert (tmp_path / "traces" / "passed-case.jsonl").exists()


def test_runner_marks_failed_case_when_invariant_violated(tmp_path: Path) -> None:
    """故意写一条永远不可能满足的断言，验证 Runner 把它标 FAILED。"""
    case = TestCase(
        id="impossible-case",
        name="impossible",
        sandbox="pysim:v1",
        seed=1,
        actions=[
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.0),
        ],
        assertions=[
            Assertion(
                # Fireball 的实际 CD 是 8s；这里期望 999s 所以必然违反。
                invariant=CooldownAtLeastAfterCastInvariant(
                    id="impossible-cd",
                    description="",
                    actor="p1",
                    skill="skill_fireball",
                    expected_cooldown=999.0,
                    tolerance=0.1,
                ),
                when=AssertionWhen.END_OF_RUN,
            )
        ],
    )
    suite = run_plan(_one_case_plan(case), _factory, artifacts_dir=tmp_path)
    assert suite.passed == 0
    assert suite.failed == 1
    # 失败详情必须带到 case 级
    assert suite.cases[0].outcome == CaseOutcome.FAILED
    assert suite.cases[0].failing_assertions


def test_runner_marks_error_when_sandbox_rejects_action(tmp_path: Path) -> None:
    """同一技能在冷却期内再次施放，沙箱会拒绝；Runner 必须记作 ERROR。"""
    case = TestCase(
        id="double-cast",
        name="double",
        sandbox="pysim:v1",
        seed=1,
        actions=[
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.0),   # Fireball 完成，进入 CD
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
        ],
        assertions=[],
    )
    suite = run_plan(_one_case_plan(case), _factory, artifacts_dir=tmp_path)
    assert suite.errored == 1
    assert suite.cases[0].outcome == CaseOutcome.ERROR
    assert suite.cases[0].error_message is not None
    assert "on cooldown" in suite.cases[0].error_message


def test_suite_summary_line_stable() -> None:
    """summary_line 格式要稳定（被 CI grep / Slack 贴用）。"""
    case = TestCase(
        id="summary",
        name="s",
        sandbox="pysim:v1",
        seed=1,
        actions=[WaitAction(seconds=0.05)],
        assertions=[],
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        suite = run_plan(_one_case_plan(case), _factory, artifacts_dir=td)
    line = suite.summary_line()
    assert "passed" in line and "failed" in line and "errored" in line
    assert "pysim:v1" in line
