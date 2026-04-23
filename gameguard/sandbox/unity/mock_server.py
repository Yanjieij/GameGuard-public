"""Mock Unity gRPC server（Stage 6 · D19）。

目的
====

让 ``unity:headless`` sandbox spec 真能跑起来，而不是 ``NotImplementedError``：

    ┌─────────────────────────┐   gRPC (:50099)    ┌──────────────────────────┐
    │ UnityAdapter (客户端)   │ ─────────────────→ │ GameGuardSandboxServicer │
    │ gameguard/sandbox/unity │                     │ 内部 hold PySim/QuestSim │
    │ /adapter.py             │                     │ 实例作为 real backend    │
    └─────────────────────────┘                     └──────────────────────────┘

mock server 和真实 Unity server 的唯一区别是：后者的 ``_backend`` 是 Unity
Editor PlayMode 进程，前者是 Python 实例。Proto 协议完全一致，换 backend
时上层代码零改动——这正是 Stage 6 要证明的"架构可工业化落地"论点。

跑法
====

手动：
    conda activate gameguard
    python -m gameguard.sandbox.unity.mock_server --port 50099

作为 make target：
    make unity-server

测试：
    make test-unity       # pytest tests/test_unity_e2e.py -v

后端选择
========

``ResetRequest.sandbox_spec`` 字符串由 server 复用 ``resolve_sandbox_factory``
解析（避免路由逻辑 drift）。支持：
    - ``pysim:v1`` / ``pysim:v2``
    - ``questsim:v1`` / ``questsim:v1-harbor`` (需要预装 pybullet)

空串默认 ``pysim:v1``。

PySim 走全字段强类型 proto；QuestSim 场景对象通过 ``custom_fields`` bytes
以 JSON 透传（translate.py 的设计说明）。
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from concurrent import futures

import grpc

from gameguard.sandbox.adapter import GameAdapter
from gameguard.sandbox.unity.generated import (
    gameguard_v1_pb2 as pb,
)
from gameguard.sandbox.unity.generated import (
    gameguard_v1_pb2_grpc as pb_grpc,
)
from gameguard.sandbox.unity.translate import (
    adapter_info_to_proto,
    event_to_proto,
    proto_to_action,
    state_to_proto,
    step_result_to_proto,
)

logger = logging.getLogger("gameguard.unity.mock_server")


# =============================================================================
# Servicer
# =============================================================================

class GameGuardSandboxServicer(pb_grpc.GameGuardSandboxServicer):
    """gRPC 服务实现。按 sandbox_spec 路由到 PySim / QuestSim backend。

    线程安全：gRPC server 用 ThreadPoolExecutor，多个 RPC 可能并发。本
    servicer 的所有状态改动都走 ``self._lock`` 串行化；backend 本身非线程
    安全，但 reset/step 本来就是有序调用模型，并发只会出现在 StreamEvents
    和其他 RPC 之间（只读查询）。
    """

    def __init__(self) -> None:
        self._backend: GameAdapter | None = None
        self._spec: str = ""
        self._project_name: str = "gameguard-mock"
        self._engine_version: str = "mock-0.1"
        self._lock = threading.Lock()
        # 记录每次 step 后的"已推送事件数"，用于 streaming 增量推送
        self._stream_cursor: int = 0

    # ---- 内部工具 ----------------------------------------------------------

    def _require_backend(self, context: grpc.ServicerContext) -> GameAdapter:
        if self._backend is None:
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("sandbox 未 reset，请先调用 Reset(seed=..., sandbox_spec=...)")
            raise RuntimeError("backend not initialized")
        return self._backend

    def _build_backend(self, spec: str) -> GameAdapter:
        """通过 CLI 的 resolve_sandbox_factory 路由，避免两套规则不一致。"""
        # 晚导入：CLI 依赖 typer，放 module top 会把 typer 变成 mock_server 的
        # 强依赖。gRPC 服务端不需要 typer 直到实际调到这里。
        from gameguard.cli import resolve_sandbox_factory
        return resolve_sandbox_factory(spec)

    # ---- RPC 实现 ----------------------------------------------------------

    def Reset(self, request: pb.ResetRequest, context: grpc.ServicerContext) -> pb.StateResponse:
        spec = request.sandbox_spec or "pysim:v1"
        logger.info("Reset(seed=%d, sandbox_spec=%s)", request.seed, spec)
        with self._lock:
            try:
                self._backend = self._build_backend(spec)
                self._spec = spec
                state = self._backend.reset(request.seed)
                self._stream_cursor = 0
            except Exception as e:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(f"构造 sandbox 失败（spec={spec!r}）：{e}")
                raise
        return state_to_proto(state)

    def Step(self, request: pb.StepRequest, context: grpc.ServicerContext) -> pb.StepResponse:
        backend = self._require_backend(context)
        action = proto_to_action(request.action)
        with self._lock:
            result = backend.step(action)
        return step_result_to_proto(result)

    def QueryState(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.StateResponse:
        backend = self._require_backend(context)
        with self._lock:
            state = backend.state()
        return state_to_proto(state)

    def StreamEvents(
        self,
        request: pb.StreamEventsRequest,
        context: grpc.ServicerContext,
    ) -> object:  # Iterator[pb.EventBatch]
        """server-streaming：每 50ms 吐一次 trace 增量。

        简单实现：轮询 backend.trace() 的 events 列表，对比游标推新的。
        生产里 Unity 侧会用 event bus 的 push 机制，没有轮询。但对 mock
        server 够用——单元测试里 client 拉几次就 cancel 了。
        """
        backend = self._require_backend(context)
        allowed = set(request.kind_filter) if request.kind_filter else None
        last_cursor = 0
        while context.is_active():
            with self._lock:
                events = list(backend.trace().events)
            if len(events) > last_cursor:
                new = events[last_cursor:]
                last_cursor = len(events)
                if allowed is not None:
                    new = [e for e in new if e.kind in allowed]
                if new:
                    yield pb.EventBatch(events=[event_to_proto(e) for e in new])
            time.sleep(0.05)

    def Snapshot(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.SnapshotBlob:
        backend = self._require_backend(context)
        with self._lock:
            data = backend.snapshot()
        return pb.SnapshotBlob(data=data, format_version="v1")

    def Restore(self, request: pb.SnapshotBlob, context: grpc.ServicerContext) -> pb.StateResponse:
        backend = self._require_backend(context)
        with self._lock:
            backend.restore(request.data)
            state = backend.state()
        return state_to_proto(state)

    def Info(self, request: pb.Empty, context: grpc.ServicerContext) -> pb.AdapterInfo:
        if self._backend is None:
            # 未 reset 前也允许查 Info，返回 server 元数据
            return pb.AdapterInfo(
                name=f"mock-server(unconfigured,spec={self._spec or 'none'})",
                version="0.1",
                deterministic=True,
                engine_version=self._engine_version,
                project_name=self._project_name,
            )
        with self._lock:
            info = self._backend.info
        return adapter_info_to_proto(
            info, engine_version=self._engine_version, project_name=self._project_name
        )


# =============================================================================
# 启动入口
# =============================================================================

def serve(host: str = "127.0.0.1", port: int = 50099, max_workers: int = 4) -> grpc.Server:
    """构造并启动一个 gRPC server，返回未阻塞的 ``server`` 对象。

    调用方负责：
      - ``server.wait_for_termination()`` 阻塞
      - 或 ``server.stop(grace=...)`` 关停

    端口 50099 是项目自订的"mock 默认"，避开 50051（Unity com.unity.grpc
    默认端口）便于和真实 Unity 并行跑。
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb_grpc.add_GameGuardSandboxServicer_to_server(GameGuardSandboxServicer(), server)
    addr = f"{host}:{port}"
    server.add_insecure_port(addr)
    server.start()
    logger.info("GameGuard mock gRPC server listening on %s", addr)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="GameGuard mock Unity gRPC server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50099)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="打开 INFO 日志（默认 WARNING）"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    server = serve(args.host, args.port, args.workers)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl-C，server stopping...")
        server.stop(grace=2)


if __name__ == "__main__":
    main()
