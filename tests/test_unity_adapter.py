"""D11/D19 meta-tests —— UnityAdapter 接口与 mock 模式。

验证项：
  1. proto 文件存在且包含关键 message 定义
  2. UnityAdapter 实现 GameAdapter ABC
  3. from_endpoint 能构造（lazy channel，不需要 server 在线）
  4. from_mock 用预录 trace 能跑 reset → step → trace
  5. CLI 路由提到 unity:mock / unity:headless

真实 gRPC 链路 (client ↔ mock_server) 的 E2E 验证在
tests/test_unity_e2e.py，需要 server 进程，默认跑。
"""
from __future__ import annotations

from pathlib import Path

from gameguard.domain import NoopAction
from gameguard.sandbox.adapter import GameAdapter
from gameguard.sandbox.unity import PROTO_PATH, UnityAdapter


def test_proto_file_exists_and_defines_key_messages() -> None:
    """proto 文件应当存在且包含核心 RPC 与 message 定义。"""
    assert PROTO_PATH.exists(), f"proto 文件应在 {PROTO_PATH}"
    text = PROTO_PATH.read_text(encoding="utf-8")
    # 关键服务
    assert "service GameGuardSandbox" in text
    # 关键 RPCs
    for rpc in ("Reset", "Step", "QueryState", "StreamEvents", "Snapshot", "Restore", "Info"):
        assert f"rpc {rpc}" in text, f"proto 缺 RPC: {rpc}"
    # 关键 message
    for msg in ("Action", "CastAction", "WaitAction", "InterruptAction",
                "StateResponse", "Character", "BuffInstance", "EventBatch",
                "AdapterInfo"):
        assert f"message {msg}" in text, f"proto 缺 message: {msg}"


def test_unity_adapter_is_game_adapter() -> None:
    """UnityAdapter 必须实现 GameAdapter ABC（接口契约）。"""
    assert issubclass(UnityAdapter, GameAdapter)


def test_unity_adapter_from_endpoint_constructs_lazily() -> None:
    """D19：from_endpoint 现在创建 gRPC channel，不再抛 NotImplementedError。

    grpc.insecure_channel 本身是 lazy 的（连接发生在第一次 RPC），所以
    无 server 在线也能构造成功。真正的连上 / 跑通在 E2E test 里验。
    """
    adapter = UnityAdapter.from_endpoint("127.0.0.1", 50099, sandbox_spec="pysim:v1")
    try:
        assert adapter.endpoint == "127.0.0.1:50099"
        assert adapter.sandbox_spec == "pysim:v1"
        # 没调 reset 时 info 拿到的是 unbound 回退值
        # （真 reset + RPC 在 E2E test）
    finally:
        adapter.close()


def test_unity_adapter_mock_round_trip(tmp_path: Path) -> None:
    """用预录 trace 跑 mock 模式：reset → step → trace 全流程通。"""
    # 准备一份最小预录 trace（5 个事件）
    mock_path = tmp_path / "mock_trace.jsonl"
    mock_path.write_text(
        "\n".join(
            f'{{"tick": {i}, "t": {i*0.05}, "kind": "tick", "amount": 0.05}}'
            for i in range(1, 6)
        ),
        encoding="utf-8",
    )

    adapter = UnityAdapter.from_mock(mock_path)
    info = adapter.info
    assert info.name == "unity-mock"
    assert info.deterministic is True

    # reset
    state = adapter.reset(seed=42)
    assert state.seed == 42
    assert state.tick == 0

    # step（mock 模式吃掉预录事件）
    result = adapter.step(NoopAction())
    assert result.outcome.accepted is True
    assert result.new_events == 5     # 我们 mock 了 5 个事件
    assert len(adapter.trace()) == 5

    # snapshot/restore round-trip（mock 模式实现的简化版）
    snap = adapter.snapshot()
    assert snap, "snapshot 不应为空"
    adapter.restore(snap)
    # restore 后状态应当一致
    assert adapter.state().seed == 42


def test_unity_cli_routing_mentioned_in_info() -> None:
    """`gameguard info` 应提到 unity 子状态。"""
    # 直接验证模板字符串（避免实际 typer 调用的复杂性）
    # 此处只读取 cli.py 源码看关键路由词在不在
    cli_src = Path(__file__).resolve().parents[1] / "gameguard" / "cli.py"
    src = cli_src.read_text(encoding="utf-8")
    assert "unity:mock" in src, "CLI 应路由 unity:mock"
    assert "unity:headless" in src, "CLI 应路由 unity:headless"
    assert "UnityAdapter" in src, "CLI 应导入 UnityAdapter"
