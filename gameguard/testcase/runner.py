"""本地测试运行器（Test Runner）。

这是 GameGuard "离线模式" 的核心

在 D4/D5 接入 LLM Agent 之前，我们先让没有 LLM 也能跑的闭环成立。
这样有三个好处：
  1) 把 "跑测试" 和 "用 Agent 生成测试" 彻底解耦 —— 符合软件工程常识。
     Agent 产出的只是一份 TestPlan，和人手写的 Plan 在 Runner 看来
     没有任何区别。
  2) 便于调试 Agent。 Agent 跑错了，我们把它生成的 plan 存下来，事后
     单独喂给 Runner 复现，不需要再调 LLM。
  3) 便于压成本。回归 CI 里大部分时间 Agent 应该缓存命中，真正跑沙箱的
     是 Runner；让它独立就是独立的可观测/可限流单元。

Runner 的契约

输入：TestPlan + sandbox 工厂（一个 callable，接收版本字符串返回
     GameAdapter）
输出：TestSuiteResult
副作用：
    - 把每条用例的完整 EventLog 以 JSONL 形式写到
      ``artifacts/traces/<case_id>.jsonl``（Triage 需要）
    - 把每条用例最终的沙箱 snapshot 写到
      ``artifacts/snapshots/<case_id>.bin``（Bug 报告的 "一键复现" 要用）

这两个副作用在计划文档的 "验证计划" 里明确要求（"相同 seed + 确定性
模式下，连续 3 次跑的报告完全一致"）。把 trace/snapshot 写进磁盘是
让 Triage / 回归工具能离线读，而不是把一切揉进内存。

为什么 "断言检查时机" 由 Runner 而非 TestCase 决定？

TestCase 只声明 "this assertion 何时检查"（AssertionWhen），Runner 负责
实现调度。这是经典的 "policy vs mechanism" 分离：
  - policy  = TestCase / AssertionWhen：人/LLM 能改
  - mechanism = Runner：工程师维护，不轻易改
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gameguard.domain.invariant import (
    InvariantResult,
    ReplayDeterministicInvariant,
    SaveLoadRoundTripInvariant,
    StateView,
    evaluate,
)
from gameguard.sandbox.adapter import GameAdapter
from gameguard.testcase.model import (
    Assertion,
    AssertionOutcome,
    AssertionWhen,
    CaseOutcome,
    TestCase,
    TestPlan,
    TestResult,
    TestSuiteResult,
)

# 沙箱工厂签名：传入 "pysim:v1" 这样的字符串，返回一个 GameAdapter 实例。
# 用字符串是为了让 TestCase 能把 "跑在哪个版本" 显式写进 YAML。
SandboxFactory = Callable[[str], GameAdapter]

# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def run_plan(
    plan: TestPlan,
    factory: SandboxFactory,
    *,
    artifacts_dir: str | Path = "artifacts",
    stop_on_error: bool = False,
    suite_json_path: str | Path | None = None,
) -> TestSuiteResult:
    """跑完整个 TestPlan。

    参数:
        plan:             要跑的 TestPlan
        factory:          沙箱工厂，签名 (str) -> GameAdapter
        artifacts_dir:    trace/snapshot 落盘的根目录；runner 会自动创建
                          子目录 ``traces/`` 和 ``snapshots/``。
        stop_on_error:    是否在第一条 ERROR 后中断。CI 里一般设 False
                          （跑完所有用例再汇总）；本地调试可以打开以快速定位。
        suite_json_path:  可选；若给定，就把 TestSuiteResult 序列化到该路径
                          （JSON）。给 D7 的 ``gameguard triage --suite ...``
                          子命令使用，让事后聚类不必再重跑沙箱。
                          默认 None = 不落盘。

    返回:
        TestSuiteResult —— 可以直接喂给 reports 模块生成 md/html。
    """
    artifacts = Path(artifacts_dir)
    (artifacts / "traces").mkdir(parents=True, exist_ok=True)
    (artifacts / "snapshots").mkdir(parents=True, exist_ok=True)

    suite_start = time.monotonic()
    results: list[TestResult] = []

    for case in plan.cases:
        result = run_case(case, factory, artifacts_dir=artifacts)
        results.append(result)
        if stop_on_error and result.outcome == CaseOutcome.ERROR:
            break

    suite = TestSuiteResult(
        plan_id=plan.id,
        plan_version=plan.version,
        # 整个 plan 里所有用例的 sandbox 未必一样，挑第一条作为"代表"；
        # 真实回归里大多数 plan 都会钉死同一个版本。
        sandbox=plan.cases[0].sandbox if plan.cases else "unknown",
        total=len(results),
        passed=sum(1 for r in results if r.outcome == CaseOutcome.PASSED),
        failed=sum(1 for r in results if r.outcome == CaseOutcome.FAILED),
        errored=sum(1 for r in results if r.outcome == CaseOutcome.ERROR),
        skipped=sum(1 for r in results if r.outcome == CaseOutcome.SKIPPED),
        wall_time_ms=(time.monotonic() - suite_start) * 1000.0,
        cases=results,
    )

    # 可选：把 suite 落盘 JSON，给 D7 triage 子命令复用
    if suite_json_path is not None:
        p = Path(suite_json_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            suite.model_dump_json(indent=2, exclude_none=False),
            encoding="utf-8",
        )

    return suite

def load_suite_from_json(path: str | Path) -> TestSuiteResult:
    """从磁盘 JSON 还原 TestSuiteResult；供事后 triage 子命令使用。"""
    raw = Path(path).read_text(encoding="utf-8")
    return TestSuiteResult.model_validate_json(raw)

# --------------------------------------------------------------------------- #
# 单条用例执行
# --------------------------------------------------------------------------- #

def run_case(
    case: TestCase,
    factory: SandboxFactory,
    *,
    artifacts_dir: Path,
) -> TestResult:
    """执行单条用例。永远不 raise —— 异常会被捕获并转成 ERROR 结果。

    运行顺序（和 docstring 顶部描述一致）：
      1. reset(seed)
      2. 逐条提交 action，检查 EVERY_TICK 断言
      3. 全部 action 跑完后，检查 END_OF_RUN 断言
      4. 落盘 trace / snapshot
    """
    wall_start = time.monotonic()
    sandbox = factory(case.sandbox)

    # 单独追踪 EVERY_TICK 类断言，方便边跑边查——我们在每个 action 之后
    # 立刻检查一次，这样哪个 action 触发了违规能被精确定位。
    tick_assertions = [a for a in case.assertions if a.when == AssertionWhen.EVERY_TICK]
    end_assertions = [a for a in case.assertions if a.when == AssertionWhen.END_OF_RUN]
    # ON_EVENT 在 D4 以后实现；当前视为 END_OF_RUN 的子集来保底。
    for a in case.assertions:
        if a.when == AssertionWhen.ON_EVENT:
            end_assertions.append(a)

    assertion_results: list[AssertionOutcome] = []
    error_message: str | None = None
    outcome: CaseOutcome = CaseOutcome.PASSED

    try:
        state = sandbox.reset(case.seed)
        assert state.seed == case.seed, "adapter.reset 必须把 seed 写回 state"

        # 逐条动作执行
        for action_index, action in enumerate(case.actions):
            step = sandbox.step(action)

            # 超时保护（防止某条 wait 把 tick 拉到天上）
            if sandbox.state().tick > case.timeout_ticks:
                raise TimeoutError(
                    f"tick {sandbox.state().tick} exceeded timeout "
                    f"{case.timeout_ticks} on action #{action_index}"
                )

            # 如果 step 拒绝了动作，通常说明用例本身的预期不对
            # （比如用例要求 cast 但沙箱认为技能在冷却），
            # 这 可能是 bug，也可能是用例写错 —— 现在我们统一按
            # "证据附加到 ERROR" 处理，让 Triage 再分辨。
            if not step.outcome.accepted:
                raise RuntimeError(
                    f"sandbox rejected action #{action_index} "
                    f"({action.kind}): {step.outcome.reason!r}"
                )

            # 每个 action 后刷一次 EVERY_TICK（粗粒度近似：实际是 every-action；
            # D3 版本够用。后面需要 tick 级粒度时，再在 PySim 上加回调钩子）。
            for a in tick_assertions:
                ao = _check_one(a, sandbox)
                assertion_results.append(ao)
                # 第一次失败就记住，但不中断 —— 先跑完拿到完整 trace，
                # Triage 才能做聚类（同一个 bug 常触发多条失败）。
                if not ao.result.passed and outcome == CaseOutcome.PASSED:
                    outcome = CaseOutcome.FAILED

        # 最终状态下检查 END_OF_RUN 断言
        for a in end_assertions:
            # replay_deterministic 是 meta-assertion，需要把 case 重跑一次
            # 比对——走单独的双跑路径而非 view-based evaluator。
            if isinstance(a.invariant, ReplayDeterministicInvariant):
                ao = _check_replay_determinism(a, case, factory)
            elif isinstance(a.invariant, SaveLoadRoundTripInvariant):
                ao = _check_save_load_round_trip(a, sandbox)
            else:
                ao = _check_one(a, sandbox)
            assertion_results.append(ao)
            if not ao.result.passed and outcome == CaseOutcome.PASSED:
                outcome = CaseOutcome.FAILED

    except Exception as exc:  # noqa: BLE001 —— 这里我们就是要兜底
        outcome = CaseOutcome.ERROR
        # 记完整异常文本，便于 bug 报告定位
        error_message = f"{type(exc).__name__}: {exc}"

    # ---- 落盘证据（即使 ERROR 也要写，方便复现）----
    trace_path = Path(artifacts_dir) / "traces" / f"{case.id}.jsonl"
    snap_path = Path(artifacts_dir) / "snapshots" / f"{case.id}.bin"
    try:
        _write_trace(sandbox, trace_path)
        snap_path.write_bytes(sandbox.snapshot())
    except Exception as io_exc:  # noqa: BLE001
        # 落盘失败不影响结果，但要记一笔
        error_message = (error_message or "") + f"\n[artifact_io] {io_exc}"

    wall_ms = (time.monotonic() - wall_start) * 1000.0
    state = sandbox.state()

    return TestResult(
        case_id=case.id,
        case_name=case.name,
        outcome=outcome,
        seed=case.seed,
        sandbox=case.sandbox,
        ticks_elapsed=state.tick,
        sim_time=state.t,
        wall_time_ms=wall_ms,
        error_message=error_message,
        assertion_results=assertion_results,
        trace_path=str(trace_path),
        event_count=len(sandbox.trace()),
    )

# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _check_one(a: Assertion, sandbox: GameAdapter) -> AssertionOutcome:
    """在当前沙箱状态下评估一条断言。

    我们把 `Character` 对象直接放进 StateView —— invariant 评估器只读，
    已在 StateView 的 docstring 里承诺不改；Pydantic 模型本身支持
    attribute 访问，不需要再转一次。

    QuestSim 的 evaluator 需要额外的 scene/quest/entities/dialogues 上下文，
    我们在构建 StateView 时通过 duck-typing 从 sandbox 读取（pysim 没有
    这些属性则保持 None）。
    """
    s = sandbox.state()
    view = StateView(t=s.t, tick=s.tick, characters=dict(s.characters))
    # QuestSim 上下文透传（只读；若 sandbox 没有这些 property 则保持 None）
    cfg = getattr(sandbox, "config", None)
    if cfg is not None:
        view.scene = getattr(cfg, "scene", None)
        view.quest = getattr(cfg, "quest", None)
        view.entities = getattr(cfg, "entities", None)
        # dialogue_no_dead_branch 需要 dialogues dict，通过 dynamic attr 塞进去
        view.dialogues = getattr(cfg, "dialogues", None)
    try:
        result = evaluate(a.invariant, view, sandbox.trace())
    except Exception as exc:  # noqa: BLE001
        # 评估器自身炸了 —— 这是"框架 bug"而非"产品 bug"，
        # 单独标记便于与真实失败区分。
        result = InvariantResult(
            invariant_id=a.invariant.id,
            passed=False,
            message=f"evaluator crashed: {type(exc).__name__}: {exc}",
        )
    return AssertionOutcome(
        assertion_invariant_id=a.invariant.id,
        when=a.when,
        result=result,
    )

def _check_save_load_round_trip(
    a: Assertion, sandbox: GameAdapter
) -> AssertionOutcome:
    """save 当前状态 → 改变 sandbox 状态 → load 回来 → 比较 state.

    依赖 sandbox 支持 SaveAction/LoadAction（QuestSim 有）。对 PySim 会抛
    `accepted=False`，evaluator 返回失败。

    这是 Q-BUG-004 的 oracle：v2 用 LossyJsonSaveCodec 时 load 回来的
    state 类型被降级，pos 不再是 Vec3 而是 tuple/list，比较时 != initial。
    """
    from gameguard.domain.action import LoadAction, SaveAction
    inv = a.invariant
    slot = getattr(inv, "slot", "auto")

    # 提取"save 时"状态的参考（用 make_save_payload 的逻辑，但不走 v2 codec）
    # 简化版：在 SandboxState 和 entities.pos/state 两个维度比较。
    cfg = getattr(sandbox, "config", None)
    entities = getattr(cfg, "entities", None) if cfg else None

    try:
        # 1. 存档
        save_result = sandbox.step(SaveAction(slot=slot))
        if not save_result.outcome.accepted:
            return AssertionOutcome(
                assertion_invariant_id=inv.id, when=a.when,
                result=InvariantResult(
                    invariant_id=inv.id, passed=False,
                    message=f"SaveAction 被拒绝：{save_result.outcome.reason}",
                ),
            )

        # 2. 记录 save 时的快照（深拷贝 entities）
        import copy
        if entities is None:
            return AssertionOutcome(
                assertion_invariant_id=inv.id, when=a.when,
                result=InvariantResult(
                    invariant_id=inv.id, passed=False,
                    message="sandbox 无 entities，无法做 save/load round-trip",
                ),
            )
        baseline_entities = copy.deepcopy(entities.all())
        baseline_flags = None
        if getattr(cfg, "quest", None) is not None:
            baseline_flags = copy.deepcopy(cfg.quest.flags.values)

        # 3. 改变状态：瞬移 player 到不同位置（作为 "advance"）
        from gameguard.domain import MoveToAction, Vec3
        for e in entities.all():
            if e.kind.value == "player":
                sandbox.step(MoveToAction(
                    actor=e.id, pos=Vec3(x=e.pos.x + 10, y=e.pos.y + 10, z=e.pos.z),
                    mode="teleport",
                ))
                break

        # 4. Load 回来
        load_result = sandbox.step(LoadAction(slot=slot))
        if not load_result.outcome.accepted:
            return AssertionOutcome(
                assertion_invariant_id=inv.id, when=a.when,
                result=InvariantResult(
                    invariant_id=inv.id, passed=False,
                    message=f"LoadAction 被拒绝：{load_result.outcome.reason}",
                ),
            )

        # 5. 比较 entities：每个 entity 的 pos 应该跟 baseline 一致
        for baseline_e in baseline_entities:
            now_e = entities.get_optional(baseline_e.id)
            if now_e is None:
                return AssertionOutcome(
                    assertion_invariant_id=inv.id, when=a.when,
                    result=InvariantResult(
                        invariant_id=inv.id, passed=False,
                        message=f"load 后丢失 entity {baseline_e.id!r}",
                    ),
                )
            # pos 比较允许 1e-6 浮点误差
            if abs(now_e.pos.x - baseline_e.pos.x) > 1e-6 \
                    or abs(now_e.pos.y - baseline_e.pos.y) > 1e-6 \
                    or abs(now_e.pos.z - baseline_e.pos.z) > 1e-6:
                return AssertionOutcome(
                    assertion_invariant_id=inv.id, when=a.when,
                    result=InvariantResult(
                        invariant_id=inv.id, passed=False,
                        message=(
                            f"entity {baseline_e.id!r} 在 load 后 pos={now_e.pos} "
                            f"≠ save 时 {baseline_e.pos}"
                        ),
                        actual=str(now_e.pos), expected=str(baseline_e.pos),
                    ),
                )
            # state 比较：key/value 完全相等
            if dict(now_e.state) != dict(baseline_e.state):
                return AssertionOutcome(
                    assertion_invariant_id=inv.id, when=a.when,
                    result=InvariantResult(
                        invariant_id=inv.id, passed=False,
                        message=(
                            f"entity {baseline_e.id!r} state {now_e.state} "
                            f"≠ save 时 {baseline_e.state}"
                        ),
                    ),
                )

        # 6. 比较 quest flags
        if baseline_flags is not None:
            now_flags = cfg.quest.flags.values
            if dict(now_flags) != dict(baseline_flags):
                return AssertionOutcome(
                    assertion_invariant_id=inv.id, when=a.when,
                    result=InvariantResult(
                        invariant_id=inv.id, passed=False,
                        message=(
                            f"quest flags 在 load 后 {now_flags} "
                            f"≠ save 时 {baseline_flags}"
                        ),
                    ),
                )

        return AssertionOutcome(
            assertion_invariant_id=inv.id, when=a.when,
            result=InvariantResult(invariant_id=inv.id, passed=True),
        )
    except Exception as exc:  # noqa: BLE001
        return AssertionOutcome(
            assertion_invariant_id=inv.id, when=a.when,
            result=InvariantResult(
                invariant_id=inv.id, passed=False,
                message=f"save_load 检测异常：{type(exc).__name__}: {exc}",
            ),
        )

def _check_replay_determinism(
    a: Assertion, case: TestCase, factory: SandboxFactory
) -> AssertionOutcome:
    """重跑同 seed 同 actions，断言两次 trace 严格一致 + rng_draws 一致。

    这是 D8 的 I-10 评估器；BUG-005（v2 暴击 RNG 用 global random()）会被
    这条抓到——两次跑产生不同 crit 序列 → 两次 damage_dealt 事件的 meta.crit
    不同 → 序列不等。
    """
    inv = a.invariant
    try:
        sb1 = factory(case.sandbox)
        sb1.reset(case.seed)
        for action in case.actions:
            sb1.step(action)
        sb2 = factory(case.sandbox)
        sb2.reset(case.seed)
        for action in case.actions:
            sb2.step(action)
        log1 = sb1.trace().events
        log2 = sb2.trace().events
        rng1 = sb1.state().rng_draws
        rng2 = sb2.state().rng_draws

        if len(log1) != len(log2):
            return AssertionOutcome(
                assertion_invariant_id=inv.id,
                when=a.when,
                result=InvariantResult(
                    invariant_id=inv.id, passed=False,
                    message=f"两次重放事件数不一致：{len(log1)} vs {len(log2)}",
                    actual=len(log2), expected=len(log1),
                ),
            )
        if rng1 != rng2:
            return AssertionOutcome(
                assertion_invariant_id=inv.id,
                when=a.when,
                result=InvariantResult(
                    invariant_id=inv.id, passed=False,
                    message=f"两次重放 rng_draws 不一致：{rng1} vs {rng2}",
                    actual=rng2, expected=rng1,
                ),
            )
        # 关键智能检测（BUG-005 指纹）：有暴击/RNG 相关事件存在但 rng_draws == 0，
        # 说明 sandbox 内有代码在用全局 random.random() 而非 sim.rng。
        # 这是确定性破坏的根因——即使两次跑序列一致只是巧合（非 0.2 概率两次都没暴击），
        # 不修复后必然在更多 case 里破坏 replay。
        if rng1 == 0 and any(
            "crit" in (e.meta or {}) for e in log1
        ):
            return AssertionOutcome(
                assertion_invariant_id=inv.id,
                when=a.when,
                result=InvariantResult(
                    invariant_id=inv.id, passed=False,
                    message=(
                        "trace 中有 crit 相关事件，但 sim.rng_draws=0；"
                        "说明 RNG 走了全局 random 而非 sandbox 注入的 seed RNG —— "
                        "这是 replay 不可重放的根因（典型 BUG-005）"
                    ),
                    actual=0,
                    expected=">0（应通过 sim.rng 抽取）",
                ),
            )
        # 比较关键字段（忽略 wall_time/t 微差）
        for i, (e1, e2) in enumerate(zip(log1, log2)):
            sig1 = (e1.kind, e1.actor, e1.target, e1.skill, e1.buff,
                    _round(e1.amount), _normalize_meta(e1.meta))
            sig2 = (e2.kind, e2.actor, e2.target, e2.skill, e2.buff,
                    _round(e2.amount), _normalize_meta(e2.meta))
            if sig1 != sig2:
                return AssertionOutcome(
                    assertion_invariant_id=inv.id,
                    when=a.when,
                    result=InvariantResult(
                        invariant_id=inv.id, passed=False,
                        message=(
                            f"事件 #{i} 在两次重放中不一致："
                            f"{sig1} vs {sig2}"
                        ),
                        witness_tick=e1.tick,
                        witness_t=e1.t,
                        actual=str(sig2),
                        expected=str(sig1),
                    ),
                )
        return AssertionOutcome(
            assertion_invariant_id=inv.id,
            when=a.when,
            result=InvariantResult(invariant_id=inv.id, passed=True),
        )
    except Exception as exc:  # noqa: BLE001
        return AssertionOutcome(
            assertion_invariant_id=inv.id,
            when=a.when,
            result=InvariantResult(
                invariant_id=inv.id, passed=False,
                message=f"replay 评估器异常：{type(exc).__name__}: {exc}",
            ),
        )

def _round(v):
    """浮点字段比较时四舍五入到 6 位，避免 wall-clock 引起的微抖动。"""
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, 6)
    return v

def _normalize_meta(m):
    """meta dict 的浮点也做四舍五入处理。"""
    if not m:
        return ()
    out = []
    for k in sorted(m.keys()):
        v = m[k]
        if isinstance(v, float):
            v = round(v, 6)
        out.append((k, v))
    return tuple(out)

def _write_trace(sandbox: GameAdapter, path: Path) -> None:
    """把 EventLog 以 JSON Lines 形式落盘。

    为什么用 JSONL 而不是一整个 JSON 数组？
      - 大 trace 流式写入/读取更友好（OpenTelemetry、ELK 均用 JSONL）
      - 增量 diff 对齐行级更容易
      - `grep`/`jq` 直接查某个事件很方便
    """
    log = sandbox.trace()
    with path.open("w", encoding="utf-8") as f:
        for e in log.events:
            # ``mode="json"`` 让 Pydantic 做 enum -> str 之类的规范化
            f.write(json.dumps(e.model_dump(mode="json"), ensure_ascii=False))
            f.write("\n")

def read_trace(path: str | Path) -> list[dict[str, Any]]:
    """从磁盘读回一个 JSONL trace（供回归 diff 用）。"""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
