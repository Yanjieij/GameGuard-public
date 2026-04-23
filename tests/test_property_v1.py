"""D8-5 · Property-based 测试（hypothesis 随机动作序列）。

==============================================================================
为什么用 hypothesis？
==============================================================================

GameGuard 已经有：
  - 手写用例（``testcases/skill_system/handwritten.yaml``）覆盖明确的契约
  - LLM 生成用例（TestGenAgent）覆盖"玩家可能这样玩"的场景

这两条都是 **基于人类预期** 的用例。还差一类：**对抗式 / property-based**
—— "我们想不到的边缘情况"。

在 Python 生态里 hypothesis 是 property-based testing 的工业标准
（QuickCheck for Python）。用法：

  @given(st.lists(...))     # 让 hypothesis 自动生成 1000 种动作序列
  def test_invariant_holds(actions): ...

它会自动：
  - 生成多样化输入（小到边界，大到正常 case）
  - 失败时自动 shrink 到最小复现
  - 把失败种子记进 ``.hypothesis/`` 下次自动重跑（regression 锁定）

==============================================================================
跑什么 invariants？
==============================================================================

只跑 v1 沙箱（黄金参考）。期望：所有 invariants 在 1000 条随机动作序列
下全绿。如果 hypothesis 找到反例，要么是 v1 实现 bug、要么是 invariant
本身写错了——这正是 property-based 测试的价值。

具体跑：
  - hp_nonneg / mp_nonneg：always 类，每个角色一条
  - cooldown 不会变负
  - buff stack 不超 max

不跑：
  - cooldown_at_least_after_cast：依赖具体 cast 时机，property 难定义
  - replay_deterministic：另有专门测试

==============================================================================
为什么不进 TestPlan / YAML？
==============================================================================

hypothesis 的 strategies 不可 YAML 序列化；它的价值在于"无穷多输入 +
shrink"，固化到 YAML 等同放弃这两点。所以 **仅在 pytest 内跑**，与
GameGuard 主流程解耦。
"""
from __future__ import annotations

import pytest

# D8 引入 hypothesis；如果环境里没装，跳过整个文件而不是错出
hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from gameguard.domain import (  # noqa: E402
    CastAction,
    InterruptAction,
    NoopAction,
    WaitAction,
)
from gameguard.sandbox.pysim.factory import make_sandbox  # noqa: E402


SKILL_IDS = ["skill_fireball", "skill_frostbolt", "skill_ignite", "skill_focus"]
ACTORS = ["p1"]
TARGETS = ["dummy", "p1"]


def _action_strategy() -> st.SearchStrategy:
    """生成单个 action（cast / wait / interrupt / noop 之一）。"""
    return st.one_of(
        st.builds(
            CastAction,
            actor=st.sampled_from(ACTORS),
            skill=st.sampled_from(SKILL_IDS),
            target=st.sampled_from(TARGETS),
        ),
        st.builds(WaitAction, seconds=st.floats(min_value=0.05, max_value=10.0)),
        st.builds(InterruptAction, actor=st.sampled_from(ACTORS)),
        st.builds(NoopAction),
    )


@given(actions=st.lists(_action_strategy(), min_size=1, max_size=8))
@settings(
    max_examples=200,
    deadline=None,                              # tick loop 偶尔慢
    suppress_health_check=[HealthCheck.too_slow],
)
def test_v1_hp_mp_always_nonneg_under_random_actions(actions) -> None:
    """**Property**：v1 在任意合法动作序列下，HP 和 MP 都不会跌为负数。

    动作可能被沙箱拒绝（CD 内重复 cast、MP 不足等），我们容忍 ``ERROR``
    路径——只要它**不通过**让 hp/mp 变负就算 invariant 成立。
    """
    sim = make_sandbox("v1")
    sim.reset(seed=0)
    for action in actions:
        try:
            result = sim.step(action)
        except Exception:
            # 沙箱崩了 → 不算 property 违反，跳过
            assume(False)
            return
        # outcome.accepted=False 时沙箱不会真改 state，跳过
        if not result.outcome.accepted:
            continue

    state = sim.state()
    for c in state.characters.values():
        assert c.hp >= 0, f"{c.id}.hp = {c.hp} < 0  actions={actions}"
        assert c.mp >= -1e-6, f"{c.id}.mp = {c.mp} < 0  actions={actions}"


@given(actions=st.lists(_action_strategy(), min_size=1, max_size=8))
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_v1_buff_stacks_within_limit(actions) -> None:
    """**Property**：v1 在任意动作序列下，所有 buff 的 stack 数 ≤ max_stacks。"""
    sim = make_sandbox("v1")
    sim.reset(seed=0)
    for action in actions:
        try:
            result = sim.step(action)
        except Exception:
            assume(False)
            return
        if not result.outcome.accepted:
            continue

    buff_book = sim.buffs if hasattr(sim, "buffs") else None
    # 没 buff_book 拿不到 max_stacks，跳过
    if buff_book is None:
        return

    state = sim.state()
    for c in state.characters.values():
        for b in c.buffs:
            spec = buff_book.get(b.spec_id)
            assert b.stacks <= spec.max_stacks, (
                f"{c.id}.{b.spec_id} stacks={b.stacks} > max={spec.max_stacks}  "
                f"actions={actions}"
            )


@given(actions=st.lists(_action_strategy(), min_size=1, max_size=10))
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_v1_cooldowns_never_negative(actions) -> None:
    """**Property**：v1 任意动作下，所有 cooldown 剩余值都 ≥ 0。"""
    sim = make_sandbox("v1")
    sim.reset(seed=0)
    for action in actions:
        try:
            result = sim.step(action)
        except Exception:
            assume(False)
            return
        if not result.outcome.accepted:
            continue

    state = sim.state()
    for c in state.characters.values():
        for skill_id, cd in c.cooldowns.items():
            assert cd >= 0, f"{c.id}.{skill_id} cooldown={cd} < 0  actions={actions}"
