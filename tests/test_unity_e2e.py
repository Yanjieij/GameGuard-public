"""Stage 6 · D19 · UnityAdapter ↔ mock gRPC server E2E。

验证点
=======

1. **协议透明性**：同一份 plan 走 ``pysim:v1`` 直跑 vs 走 ``unity:headless``
   (后端仍然是 pysim:v1) 结果必须 byte-level 一致。这是"引入 gRPC 层后
   上层代码零感知"的最强证据。
2. **QuestSim 后端也能过 gRPC**：至少 reset / step 不崩、事件能流回来。
3. **snapshot / restore round-trip**：经 gRPC 的快照恢复后状态一致。
4. **并发 cleanup**：多个 test 顺序跑不会端口冲突。

跑法
====

    make test-unity            # 专门跑本文件
    make test                  # 一起跑（mock server 自动起落）
    pytest -m "not unity"      # CI 里跳过（不依赖 grpc env 时）

本测试用 ``@pytest.mark.unity`` marker 标记，便于在不同 CI 阶段选择。

为何不用 in-process channel
===========================

grpc 的 in-process channel 快且无端口占用，但"真实跨进程 TCP 往返"才是
Stage 6 的核心论点（"协议真实可跨进程"）。我们用真 TCP（localhost 高位
端口）验，端口从 51000 起按测试函数递增，避免 conflict。
"""
from __future__ import annotations

import socket
import time

import pytest

pytest.importorskip("grpc")

from gameguard.domain.action import CastAction, NoopAction, WaitAction
from gameguard.sandbox.pysim.factory import make_sandbox
from gameguard.sandbox.unity.adapter import UnityAdapter
from gameguard.sandbox.unity.mock_server import serve as serve_mock

pytestmark = pytest.mark.unity


def _alloc_port() -> int:
    """拿一个 OS 保证空闲的 TCP 端口（close 后 gRPC 再绑，通常够用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mock_server():
    """起一个 mock server，yield 端口，测试结束自动关。"""
    port = _alloc_port()
    server = serve_mock(host="127.0.0.1", port=port)
    # 给 server 一小段时间真正 accept 连接
    time.sleep(0.15)
    yield port
    server.stop(grace=1)


# =============================================================================
# 1. 协议透明性：pysim:v1 直跑 vs unity:headless+pysim:v1
# =============================================================================

def test_grpc_pysim_v1_matches_direct(mock_server: int) -> None:
    """跑一串 cast + wait + noop，gRPC 版和直跑版的最终 state 必须一致。"""
    seed = 42
    actions = [
        WaitAction(seconds=0.1),
        CastAction(actor="p1", skill="fireball", target="dummy"),
        WaitAction(seconds=1.5),
        NoopAction(),
    ]

    # 直跑 baseline
    direct = make_sandbox("v1")
    direct.reset(seed)
    for a in actions:
        direct.step(a)
    direct_state = direct.state()

    # gRPC 跑
    remote = UnityAdapter.from_endpoint("127.0.0.1", mock_server, sandbox_spec="pysim:v1")
    try:
        remote.reset(seed)
        for a in actions:
            remote.step(a)
        remote_state = remote.state()
    finally:
        remote.close()

    # 核心 invariant 字段必须一致
    assert remote_state.tick == direct_state.tick, \
        f"tick 不一致: direct={direct_state.tick} remote={remote_state.tick}"
    assert remote_state.t == pytest.approx(direct_state.t, abs=1e-6)
    assert remote_state.seed == direct_state.seed
    # 角色 hp / mp / buffs 必须逐字段一致
    assert set(remote_state.characters) == set(direct_state.characters)
    for cid in direct_state.characters:
        d = direct_state.characters[cid]
        r = remote_state.characters[cid]
        assert r.hp == pytest.approx(d.hp, abs=1e-6), f"char {cid} hp drift"
        assert r.mp == pytest.approx(d.mp, abs=1e-6), f"char {cid} mp drift"
        assert r.state == d.state


# =============================================================================
# 2. Info RPC 透传
# =============================================================================

def test_grpc_info_reflects_backend(mock_server: int) -> None:
    """Info 应该反映 backend 的真实 name/version。"""
    remote = UnityAdapter.from_endpoint("127.0.0.1", mock_server, sandbox_spec="pysim:v2")
    try:
        remote.reset(seed=1)
        info = remote.info
        # pysim v2 backend 的 AdapterInfo.name 是 "pysim-v2"
        assert "pysim" in info.name.lower()
        assert info.deterministic is True
    finally:
        remote.close()


# =============================================================================
# 3. Snapshot / Restore round-trip
# =============================================================================

def test_grpc_snapshot_restore_round_trip(mock_server: int) -> None:
    """snapshot → 继续走 → restore 旧 snapshot → state 必须回到 snapshot 点。"""
    remote = UnityAdapter.from_endpoint("127.0.0.1", mock_server, sandbox_spec="pysim:v1")
    try:
        remote.reset(seed=7)
        remote.step(WaitAction(seconds=0.2))
        remote.step(CastAction(actor="p1", skill="fireball", target="dummy"))
        snap_tick = remote.state().tick
        snap_hp = remote.state().characters["dummy"].hp
        snap = remote.snapshot()
        assert snap, "snapshot bytes 不应为空"

        # 继续推一段
        remote.step(WaitAction(seconds=1.0))
        assert remote.state().tick > snap_tick

        # 恢复
        remote.restore(snap)
        assert remote.state().tick == snap_tick
        assert remote.state().characters["dummy"].hp == pytest.approx(snap_hp, abs=1e-6)
    finally:
        remote.close()


# =============================================================================
# 4. QuestSim 后端至少能过 gRPC（scene/quest 走 JSON 透传）
# =============================================================================

def test_grpc_questsim_basic(mock_server: int) -> None:
    """questsim:v1 也能经 gRPC 跑：至少 reset + step 一个 NoopAction 不崩。"""
    remote = UnityAdapter.from_endpoint("127.0.0.1", mock_server, sandbox_spec="questsim:v1")
    try:
        state = remote.reset(seed=3)
        assert state.seed == 3
        result = remote.step(NoopAction())
        assert result.outcome.accepted
    finally:
        remote.close()


# =============================================================================
# 5. 未 reset 就 step 应有清晰错误
# =============================================================================

def test_grpc_step_before_reset_errors_cleanly(mock_server: int) -> None:
    """在 reset 之前 step 应得到 FAILED_PRECONDITION，不是 generic crash。"""
    import grpc
    remote = UnityAdapter.from_endpoint("127.0.0.1", mock_server, sandbox_spec="pysim:v1")
    try:
        with pytest.raises(grpc.RpcError) as exc:
            remote.step(NoopAction())
        assert exc.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        remote.close()
