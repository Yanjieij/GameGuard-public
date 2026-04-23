"""UnityAdapter —— ``GameAdapter`` 的 gRPC 客户端（Stage 6 · D19）。

当前状态
========

从 D11 的"纯 stub 抛 NotImplementedError"升级为 D19 的**真 gRPC 客户端**，
可以真的连 mock server（``gameguard.sandbox.unity.mock_server``）或任何实
现了 ``gameguard_v1.proto`` 的 server（未来：真实 Unity headless）。

三种构造方式
============

1. ``from_endpoint(host, port, sandbox_spec=...)``
   连 gRPC server。mock server 由 ``make unity-server`` 起在
   ``127.0.0.1:50099``；生产里换成 Unity Editor PlayMode 进程暴露的端口。

2. ``from_mock(mock_trace_path)``
   **保留**。纯文件 trace 回放模式，不需要 server 进程。适合 CI 没 grpc
   环境时的 fallback，或从历史 trace 复现 bug。

3. ``from_channel(channel, sandbox_spec=...)``
   测试用：允许注入自己的 ``grpc.Channel``（比如 in-process channel）。

架构定位
========

本类对上层（Runner / Agent / Eval）装成一个普通 ``GameAdapter``，所有
RPC 细节（stub 调用、proto 翻译、错误映射）都在本文件里消化。这样
Runner 不用知道 sandbox 是 Python 还是 Unity。

翻译逻辑集中在 ``gameguard/sandbox/unity/translate.py``，mock_server 和
本类共用，保证双向翻译规则一致。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

# grpc 是 [unity] optional dep。本 module 支持两套使用路径：
#   - file-trace 回放（from_mock）: 不需要 grpc
#   - 真 gRPC（from_endpoint / from_channel）: 需要 grpc
# 把 grpc 改成延迟 import，mock 路径在没装 grpcio 的环境（例如默认 CI
# [dev] extras）也能跑；只有真调 from_endpoint/from_channel 或遇到
# RpcError 类型分支时才需要导入。
if TYPE_CHECKING:
    import grpc

from gameguard.domain import Action, ActionOutcome, EventLog
from gameguard.domain.event import Event
from gameguard.sandbox.adapter import (
    AdapterInfo,
    GameAdapter,
    SandboxState,
    StepResult,
)

# 指向 proto 文件的便捷常量（打印报错用）
PROTO_PATH = Path(__file__).parent / "proto" / "gameguard_v1.proto"

# 默认端点：既让 E2E test 能读到一致的地址，又允许 CI 用 env 覆盖
DEFAULT_ENDPOINT = os.environ.get("GAMEGUARD_UNITY_ENDPOINT", "127.0.0.1:50099")


@dataclass
class UnityAdapter(GameAdapter):
    """Unity gRPC 客户端 / mock trace 回放适配器。"""

    # --- 连接配置（from_endpoint / from_channel 时使用） --------------------
    endpoint: str | None = None         # "host:port"
    sandbox_spec: str = "pysim:v1"       # 让 server 端路由到具体 backend
    _channel: Any = None                 # grpc.Channel（None 表示 mock 模式）
    _stub: Any = None                    # GameGuardSandboxStub

    # --- mock 回放配置 ------------------------------------------------------
    mock_trace_path: Path | None = None
    _mock_events: list[Event] = field(default_factory=list)
    _mock_cursor: int = 0

    # --- 通用状态缓存 -------------------------------------------------------
    project_name: str = "unknown"
    engine_version: str = "unknown"
    _state: SandboxState = field(
        default_factory=lambda: SandboxState(t=0, tick=0, seed=0)
    )
    _log: EventLog = field(default_factory=EventLog)
    # QuestSim 后端透传的 custom_state（JSON 反序列化后）
    _custom_state: Any = None

    # ----------------------------------------------------------------- #
    # 构造器
    # ----------------------------------------------------------------- #

    @classmethod
    def from_endpoint(
        cls,
        host: str = "127.0.0.1",
        port: int = 50099,
        sandbox_spec: str = "pysim:v1",
    ) -> "UnityAdapter":
        """连 gRPC server。

        mock server 启动方法：``make unity-server``（监听 127.0.0.1:50099）。
        真实 Unity 接入时换成 Unity PlayMode 暴露的端口即可。
        """
        import grpc  # 延迟 import：[unity] extras 才装
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2_grpc as pb_grpc
        channel = grpc.insecure_channel(f"{host}:{port}")
        adapter = cls(
            endpoint=f"{host}:{port}",
            sandbox_spec=sandbox_spec,
            _channel=channel,
            _stub=pb_grpc.GameGuardSandboxStub(channel),
        )
        return adapter

    @classmethod
    def from_channel(
        cls,
        channel: "grpc.Channel",
        sandbox_spec: str = "pysim:v1",
    ) -> "UnityAdapter":
        """允许注入自定义 channel（测试场景：in-process channel）。"""
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2_grpc as pb_grpc
        return cls(
            endpoint="<injected channel>",
            sandbox_spec=sandbox_spec,
            _channel=channel,
            _stub=pb_grpc.GameGuardSandboxStub(channel),
        )

    @classmethod
    def from_mock(cls, mock_trace_path: str | Path) -> "UnityAdapter":
        """用预录的 JSONL trace 跑 mock 模式（不需要 server）。"""
        return cls(mock_trace_path=Path(mock_trace_path))

    # ----------------------------------------------------------------- #
    # Helpers
    # ----------------------------------------------------------------- #

    def _require_stub(self) -> Any:
        if self._stub is None:
            raise RuntimeError(
                "UnityAdapter 未绑定 gRPC channel。使用 from_endpoint(...) 或 "
                "from_channel(...) 构造；file-trace 回放用 from_mock(...)。"
            )
        return self._stub

    def close(self) -> None:
        """显式关闭 channel。测试 teardown 时用。"""
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None
            self._stub = None

    # ----------------------------------------------------------------- #
    # GameAdapter 接口实现
    # ----------------------------------------------------------------- #

    @property
    def info(self) -> AdapterInfo:
        if self.mock_trace_path is not None:
            return AdapterInfo(name="unity-mock", version="0.1-mock", deterministic=True)
        if self._stub is None:
            return AdapterInfo(name="unity-unbound", version="0.1", deterministic=True)
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb
        from gameguard.sandbox.unity.translate import proto_to_adapter_info
        resp = self._stub.Info(pb.Empty())
        return proto_to_adapter_info(resp)

    def reset(self, seed: int) -> SandboxState:
        if self.mock_trace_path is not None:
            # file-trace 回放模式
            self._state = SandboxState(t=0.0, tick=0, seed=seed)
            self._log = EventLog()
            events_raw = self.mock_trace_path.read_text(encoding="utf-8").splitlines()
            self._mock_events = [
                Event.model_validate_json(line) for line in events_raw if line.strip()
            ]
            self._mock_cursor = 0
            return self._state

        # gRPC 模式
        stub = self._require_stub()
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb
        from gameguard.sandbox.unity.translate import proto_to_state
        resp = stub.Reset(pb.ResetRequest(seed=seed, sandbox_spec=self.sandbox_spec))
        state, custom = proto_to_state(resp)
        self._state = state
        self._custom_state = custom
        self._log = EventLog()
        return state

    def step(self, action: Action) -> StepResult:
        if self.mock_trace_path is not None:
            # mock 模式：吐预录的下一批事件
            before = len(self._log)
            end = min(self._mock_cursor + 5, len(self._mock_events))
            for i in range(self._mock_cursor, end):
                self._log.append(self._mock_events[i])
            self._mock_cursor = end
            self._state = SandboxState(
                t=self._mock_events[end - 1].t if end > 0 else 0.0,
                tick=self._mock_events[end - 1].tick if end > 0 else 0,
                seed=self._state.seed,
            )
            return StepResult(
                state=self._state,
                outcome=ActionOutcome(accepted=True, events=[f"mock applied {action.kind}"]),
                new_events=len(self._log) - before,
                done=self._mock_cursor >= len(self._mock_events),
            )

        # gRPC 模式
        stub = self._require_stub()
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb
        from gameguard.sandbox.unity.translate import (
            action_to_proto,
            proto_to_step_result,
        )
        resp = stub.Step(pb.StepRequest(action=action_to_proto(action)))
        result, custom = proto_to_step_result(resp)
        self._state = result.state
        self._custom_state = custom
        # 拉取 trace 增量（用一次 QueryState+trace 或者 StreamEvents），
        # 简化：每 step 后直接主动拉 events，避免 streaming 协程复杂度
        self._refresh_events_from_server()
        return result

    def _refresh_events_from_server(self) -> None:
        """从 server 拉 trace 增量，累加到本地 ``_log``。

        朴素实现：不管 server 侧 trace 总长度，这里每次取 >= last_known
        的事件。server 端 mock 每次 step 后 trace 都在变长，客户端简单用
        ``len(self._log)`` 当游标。

        真实 Unity 接入时换成 ``StreamEvents`` server-streaming，体验更好；
        这里保持 unary 拉取是为 e2e test 下简单可预测。
        """
        # 最小实现：调用一次 StreamEvents 但只取第一批然后 cancel。
        # （如果 server 的 sleep 间隔是 50ms，我们取一次 batch 就够了。）
        import grpc  # 延迟 import；_refresh_events_from_server 只在 gRPC 路径下被调
        stub = self._require_stub()
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb
        from gameguard.sandbox.unity.translate import proto_to_event
        try:
            it = stub.StreamEvents(pb.StreamEventsRequest())
            batch = next(it, None)
            if batch is not None:
                # server 每次吐的是"从 last_cursor 到当前"的增量，但我们每
                # 次都新开一次 stream，server 的 cursor 是独立维护的——
                # 它会从头吐。因此这里做一下去重：只保留我们 _log 里还没
                # 见过的。
                seen = len(self._log.events)
                for ev in batch.events[seen:]:
                    self._log.append(proto_to_event(ev))
            # 显式 cancel 这条流，避免它 hang
            it.cancel()  # type: ignore[attr-defined]
        except grpc.RpcError:
            # 流尚未准备好 / 已 cancel / 无事件——都不是致命错误
            pass

    def state(self) -> SandboxState:
        return self._state

    def trace(self) -> EventLog:
        return self._log

    def snapshot(self) -> bytes:
        if self.mock_trace_path is not None:
            return json.dumps(
                {"cursor": self._mock_cursor, "seed": self._state.seed}
            ).encode("utf-8")
        stub = self._require_stub()
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb
        resp = stub.Snapshot(pb.Empty())
        return bytes(resp.data)

    def restore(self, snap: bytes) -> None:
        if self.mock_trace_path is not None:
            data = json.loads(snap.decode("utf-8"))
            self.reset(data["seed"])
            self._mock_cursor = data["cursor"]
            for i in range(self._mock_cursor):
                self._log.append(self._mock_events[i])
            return
        stub = self._require_stub()
        from gameguard.sandbox.unity.generated import gameguard_v1_pb2 as pb
        from gameguard.sandbox.unity.translate import proto_to_state
        resp = stub.Restore(pb.SnapshotBlob(data=snap, format_version="v1"))
        state, custom = proto_to_state(resp)
        self._state = state
        self._custom_state = custom
