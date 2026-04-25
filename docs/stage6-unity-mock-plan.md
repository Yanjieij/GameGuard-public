# Stage 6 · Unity Mock gRPC Server 方案

> **状态（D19 更新）**：**已实施完毕**。本文件保留原始方案作为设计记录，
> 下面 *实施总结* 一节记录实际落地与设计的偏差。对应代码：
> `gameguard/sandbox/unity/{mock_server,adapter,translate}.py` +
> `gameguard/sandbox/unity/generated/` + `gameguard/sandbox/unity/client/`
> + `tests/test_unity_e2e.py`（5 个 E2E 全绿）。

## 实施总结（D19）

- **proto 扩展**：`ResetRequest` 新增 `sandbox_spec`；`Action` oneof 新增
  `GenericAction(kind, payload_json)` 承载 QuestSim 五种 action，避免
  proto 随 action 字段演进而变化。QuestSim 的 scene/quest/entities 用
  `StateResponse.custom_fields` bytes 透传 JSON，proto schema 保持稳定。
- **mock server**：`GameGuardSandboxServicer` 复用 CLI 的
  `resolve_sandbox_factory`，支持 `pysim:v{1,2}` / `questsim:v1[-harbor]`
  所有 backend。gRPC ThreadPoolExecutor 4 workers + 锁串行 backend
  访问。
- **UnityAdapter**：从 `NotImplementedError` 改成真 gRPC client；保留
  `from_mock()` 的 file-trace 回放模式作为 fallback；新增
  `from_channel()` 便于测试注入。`trace()` 通过 `StreamEvents` 增量拉取。
- **CLI**：`unity:headless+<backend>[:<version>]` 语法。端点由环境变量
  `GAMEGUARD_UNITY_ENDPOINT` 控制（默认 `127.0.0.1:50099`）。
- **C# 骨架**：`client/` 下 3 个 .cs + README + packages.lock.sample，
  走 MagicOnion (Cygames · Cysharp 方案) + UniTask + PlayMode hook，
  贴近日系大厂 Unity QA 工具链。
- **E2E 测试**：`tests/test_unity_e2e.py` 5 条 —— `pysim:v1` gRPC 版和
  直跑版 byte-level 一致；`pysim:v2` info；snapshot/restore round-trip；
  `questsim:v1` 基础跑通；未 reset 错误码清晰。全部走真 TCP localhost，
  marker `@pytest.mark.unity` 可独立跑。

**实际工时**：约 3 小时（比原估 6-8h 少，主要是 proto 用 `custom_fields`
bytes 透传省了 scene/quest 的 schema 工作）。

---

---

## 目标

让这行命令从抛异常变成真的能跑：

```bash
gameguard run --plan testcases/skill_system/handwritten.yaml \
              --sandbox unity:headless
```

**具体**：启动一个本地 gRPC server 冒充 Unity headless，实现
`gameguard_v1.proto` 定义的 service，内部用 `PySim` 当后端做真正的仿真。
上层 `UnityAdapter` 真的发 gRPC 请求，收响应，翻译回 `SandboxState` /
`StepResult`。

**效果**：
- 上层任何 Runner / Agent / Eval 都感知不到后面是真 Unity 还是 Python mock
- 证明 `GameAdapter` 抽象真的能跨进程、跨语言（Python ↔ C#）
- 面试第 3 条 JD（跨领域工业化）有实证而不只是"我想过"

**非目标**（刻意不做）：
- 真实 Unity Engine 接入——还是要 2-3 周工作量，本 portfolio 不划算
- QuestSim 的物理场景（pybullet 已经 E2E，接 Unity 场景 value 递减）
- 跨机部署 / TLS / auth（mock 只需要 localhost 单进程）

---

## 架构

```
┌──────────────────────────────────┐
│  Runner / Agent / Eval           │
│  （照旧调 GameAdapter 接口）       │
└──────────────┬───────────────────┘
               │ reset(seed) / step(action) / ...
               ▼
┌──────────────────────────────────┐
│  UnityAdapter (Python)           │
│  gameguard/sandbox/unity/        │
│  adapter.py                      │
│                                  │
│  - 当前: NotImplementedError     │
│  - 改后: gRPC client             │
└──────────────┬───────────────────┘
               │ gRPC (localhost:50051)
               │ gameguard_v1.proto
               ▼
┌──────────────────────────────────┐
│  Mock Unity gRPC Server (Python) │
│  gameguard/sandbox/unity/         │
│  mock_server.py  (新增)          │
│                                  │
│  - grpcio 实现的 servicer         │
│  - 内部持一个 PySim(v1) 实例      │
│  - 把 proto message 翻译成          │
│    Action / 再翻译回 StepResult   │
└──────────────────────────────────┘
```

**关键设计**：server 和 client 都是 Python，但走真的 gRPC over TCP。
这样证明了协议边界真实可跨进程。日后换成 Unity C# server 只需要保持
proto 不变。

---

## 现有工作（已经有的）

- `gameguard/sandbox/unity/proto/gameguard_v1.proto`  
  已定义 service 和消息结构，是写好了的。扫一遍确认覆盖了 reset / step /
  snapshot / restore / trace / info 六个方法。

- `gameguard/sandbox/unity/adapter.py::UnityAdapter`  
  骨架已就位，继承 `GameAdapter`，方法都是 `NotImplementedError("D11+")`。
  有 `from_mock(trace_path)` 和 `from_endpoint(host, port)` 两个构造器。

- `gameguard/sandbox/unity/client/UnityBridge.cs`  
  C# 端骨架代码（文档级，不编译），说明日后 Unity 侧要怎么接。

- `docs/unity_integration.md`  
  已有的接入指南文档。

所以 Stage 6 的工作是**补实现**，不是从零设计。

---

## 实施步骤（6-8 小时）

### Step 1 · 生成 Python gRPC stub（0.5h）

依赖：`pip install grpcio grpcio-tools`

```bash
python -m grpc_tools.protoc \
  -I gameguard/sandbox/unity/proto \
  --python_out=gameguard/sandbox/unity/gen \
  --grpc_python_out=gameguard/sandbox/unity/gen \
  gameguard/sandbox/unity/proto/gameguard_v1.proto
```

生成：
- `gameguard_v1_pb2.py` — 消息类（ResetRequest / StepRequest / ...）
- `gameguard_v1_pb2_grpc.py` — 服务 stub（GameServiceServicer / GameServiceStub）

**关键**：生成文件加进 `.gitignore`，CI 里重新生成。避免手工修改。

把 protoc 包进 `pyproject.toml` 的 `physics` 旁新 extras：

```toml
grpc = [
    "grpcio>=1.60",
    "grpcio-tools>=1.60",
]
```

### Step 2 · 实现 Mock Server（2-3h）

新增 `gameguard/sandbox/unity/mock_server.py`：

```python
"""假 Unity 的 Python gRPC server。

用一个真正的 PySim 实例做后端，把 gRPC 请求翻译成 PySim 调用，把结果翻译
回 proto 消息。这样上层 UnityAdapter 真的走 gRPC，但不依赖真 Unity。
"""

import pickle
from concurrent import futures

import grpc
from gameguard.sandbox.pysim.factory import make_sandbox
from gameguard.sandbox.unity.gen import gameguard_v1_pb2 as pb
from gameguard.sandbox.unity.gen import gameguard_v1_pb2_grpc as pb_grpc
from gameguard.domain import CastAction, WaitAction, InterruptAction, NoopAction


class MockGameService(pb_grpc.GameServiceServicer):
    """假 Unity 的 servicer 实现，内部是 PySim(v1)。"""

    def __init__(self, pysim_version: str = "v1"):
        self._sandbox = make_sandbox(pysim_version)

    def Reset(self, request: pb.ResetRequest, context):
        state = self._sandbox.reset(seed=request.seed)
        return pb.StateResponse(
            tick=state.tick,
            t=state.t,
            seed=state.seed,
            rng_draws=state.rng_draws,
            characters_json=state.model_dump_json(),  # 懒人方案：状态塞 JSON
        )

    def Step(self, request: pb.StepRequest, context):
        # 把 proto Action 翻译成 Python Action 对象
        action = self._unpack_action(request.action)
        result = self._sandbox.step(action)
        return pb.StepResponse(
            state=pb.StateResponse(...),
            accepted=result.outcome.accepted,
            new_events=result.new_events,
            done=result.done,
        )

    def Snapshot(self, request: pb.SnapshotRequest, context):
        blob = self._sandbox.snapshot()
        return pb.SnapshotResponse(blob=blob)

    def Restore(self, request: pb.RestoreRequest, context):
        self._sandbox.restore(request.blob)
        return pb.AckResponse(ok=True)

    def Trace(self, request: pb.TraceRequest, context):
        # EventLog 流式返回
        for event in self._sandbox.trace().events:
            yield pb.EventMsg(
                tick=event.tick,
                kind=event.kind,
                actor=event.actor or "",
                skill=event.skill or "",
                meta_json=pickle.dumps(event.meta).hex(),
            )

    def Info(self, request: pb.InfoRequest, context):
        info = self._sandbox.info
        return pb.InfoResponse(
            name=info.name,
            version=info.version,
            deterministic=info.deterministic,
        )

    def _unpack_action(self, proto_action):
        """proto Action oneof → domain Action 子类。"""
        kind = proto_action.WhichOneof("action")
        if kind == "cast":
            a = proto_action.cast
            return CastAction(actor=a.actor, skill=a.skill, target=a.target)
        if kind == "wait":
            return WaitAction(seconds=proto_action.wait.seconds)
        if kind == "interrupt":
            return InterruptAction(actor=proto_action.interrupt.actor)
        if kind == "noop":
            return NoopAction()
        raise ValueError(f"unknown action kind: {kind}")


def serve(host: str = "[::]", port: int = 50051, pysim_version: str = "v1") -> grpc.Server:
    """启动 server，返回 Server 对象（调用方负责 wait_for_termination）。"""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb_grpc.add_GameServiceServicer_to_server(
        MockGameService(pysim_version), server
    )
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    return server


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 50051
    version = sys.argv[2] if len(sys.argv) > 2 else "v1"
    server = serve(port=port, pysim_version=version)
    print(f"Mock Unity server listening on :{port} (pysim:{version})")
    server.wait_for_termination()
```

**关键设计决策**：

1. **State/Event 里复杂字段塞 JSON 字符串** — 不完全照搬 proto 强类型。
   proto 里定义 `characters_json: string`，服务端 / 客户端各自 parse。
   这避免给每个 domain 字段都画 proto，工作量从"一周"降到"一天"。
2. **Trace 是 server-streaming RPC** — EventLog 可能很长，流式比一次性返
   大 message 友好。
3. **Snapshot 是 bytes blob** — proto 不知道 pickle 格式，但 bytes 字段
   透明透传没问题。
4. **v2 通过构造参数指定** — 调用 `serve(pysim_version="v2")` 起 v2 后端，
   即可做 unity:headless 差分测试。

### Step 3 · UnityAdapter 真实现（1.5h）

重写 `gameguard/sandbox/unity/adapter.py::UnityAdapter` 的方法，调 gRPC：

```python
import grpc
import pickle
from gameguard.sandbox.unity.gen import gameguard_v1_pb2 as pb
from gameguard.sandbox.unity.gen import gameguard_v1_pb2_grpc as pb_grpc


class UnityAdapter(GameAdapter):
    def __init__(self, channel: grpc.Channel):
        self._channel = channel
        self._stub = pb_grpc.GameServiceStub(channel)

    @classmethod
    def from_endpoint(cls, host: str, port: int) -> "UnityAdapter":
        channel = grpc.insecure_channel(f"{host}:{port}")
        # 5 秒超时 probe，防配置错误死等
        try:
            grpc.channel_ready_future(channel).result(timeout=5.0)
        except grpc.FutureTimeoutError:
            raise RuntimeError(
                f"Unity gRPC server at {host}:{port} not reachable. "
                f"Did you `python -m gameguard.sandbox.unity.mock_server` first?"
            )
        return cls(channel)

    def reset(self, seed: int) -> SandboxState:
        resp = self._stub.Reset(pb.ResetRequest(seed=seed))
        return SandboxState.model_validate_json(resp.characters_json)

    def step(self, action: Action) -> StepResult:
        proto_action = self._pack_action(action)
        resp = self._stub.Step(pb.StepRequest(action=proto_action))
        state = SandboxState.model_validate_json(resp.state.characters_json)
        return StepResult(
            state=state,
            outcome=ActionOutcome(accepted=resp.accepted),
            new_events=resp.new_events,
            done=resp.done,
        )

    # ... snapshot / restore / trace / info / state 类似
```

### Step 4 · CLI 路由（0.5h）

改 `gameguard/cli.py::resolve_sandbox_factory` 的 `unity:headless` 分支：

```python
if version == "headless":
    # 以前：raise NotImplementedError
    # 现在：UnityAdapter.from_endpoint("localhost", 50051)
    host = os.environ.get("GAMEGUARD_UNITY_HOST", "localhost")
    port = int(os.environ.get("GAMEGUARD_UNITY_PORT", "50051"))
    return UnityAdapter.from_endpoint(host, port)
```

### Step 5 · E2E 测试（1h）

新增 `tests/test_unity_e2e.py`：

```python
"""Unity adapter E2E：起 mock server + 走真 gRPC + 对比 pysim 直跑结果。

关键断言：相同 plan 在 unity:headless 上跑出的 suite 和 pysim:v1 直跑完全
等价（除了 wall clock 差异）。证明 gRPC 这一跳是透明的。
"""

import threading
import time
import pytest
import grpc

from gameguard.sandbox.unity.mock_server import serve
from gameguard.cli import resolve_sandbox_factory


@pytest.fixture
def unity_mock_server():
    """起 mock server，测试结束自动关。"""
    pytest.importorskip("grpc")  # 没 grpcio 就 skip
    server = serve(port=50099, pysim_version="v1")  # 测试专用端口
    time.sleep(0.1)  # 给 server 一点启动时间
    yield "localhost", 50099
    server.stop(grace=0.5)


def test_unity_e2e_reset_and_step(unity_mock_server):
    host, port = unity_mock_server
    # 临时改 env
    os.environ["GAMEGUARD_UNITY_HOST"] = host
    os.environ["GAMEGUARD_UNITY_PORT"] = str(port)

    unity = resolve_sandbox_factory("unity:headless")
    state = unity.reset(seed=42)
    assert state.seed == 42
    assert state.tick == 0
    # p1 存在且 MP 满
    assert state.characters["p1"].mp == state.characters["p1"].mp_max


def test_unity_equivalent_to_pysim_v1(unity_mock_server):
    """同一份 plan 在 unity:headless 上跑，suite 和直跑 pysim:v1 等价。"""
    plan = load_plan_from_yaml("testcases/skill_system/handwritten.yaml")
    
    # 直跑 pysim:v1
    pysim_plan = plan.model_copy(deep=True)
    for c in pysim_plan.cases:
        c.sandbox = "pysim:v1"
    pysim_suite = run_plan(pysim_plan, resolve_sandbox_factory)
    
    # 走 gRPC 到 mock server（后端也是 pysim:v1）
    unity_plan = plan.model_copy(deep=True)
    for c in unity_plan.cases:
        c.sandbox = "unity:headless"
    unity_suite = run_plan(unity_plan, resolve_sandbox_factory)
    
    # 对比 case 级 outcome
    for p_case, u_case in zip(pysim_suite.cases, unity_suite.cases):
        assert p_case.outcome == u_case.outcome, (
            f"outcome diff on {p_case.case_id}: "
            f"pysim={p_case.outcome} vs unity={u_case.outcome}"
        )
```

### Step 6 · 更新文档（0.5h）

- README 的 Sandbox 表格把 `unity:headless` 从"proto 就绪"改成"mock server ready"
- `DEMO.md` 加一条新场景："启 mock server + 同 plan 跑 pysim:v1 和 unity:headless
  证明行为等价"
- `docs/unity_integration.md` 加"Step-by-step 如何起 server"段

---

## 风险与陷阱

| 风险 | 影响 | 对策 |
|---|---|---|
| grpcio 依赖较重（~50MB） | pip install 变慢 | 作为 `[grpc]` extras 隔离，不装不用 |
| proto 字段和 Pydantic model 漂变 | 加新字段要同时改两边 | 加 CI lint 步骤 - check proto 生成文件和 domain model 一致性 |
| gRPC 端口占用 | 测试并发失败 | 用 port 0 让 OS 分配，server.start() 后读实际 port |
| pickle 跨版本不兼容 | Snapshot 在不同 Python 版本之间失效 | pickle protocol=4 + 文档标注"仅同版本 Python 可 round-trip" |
| LLM trace 里 unity:headless 的事件 kind 和 pysim 不一致 | eval 错判 | 保持 EventKind 完全一致（EventKind.Literal 就是做这个的） |
| Server 启动竞争 | 测试偶发连不上 | channel_ready_future 5s timeout + retry 1 次 |

---

## 面试价值

做完 Stage 6 后面试能讲的**新话点**：

1. **"我接口抽象是真的跨进程能用"** — 不只是"想过"或"proto 骨架"，E2E
   测试跑在 gRPC 上跑绿，pytest 里有 `test_unity_equivalent_to_pysim_v1`
   作证。

2. **"GameAdapter 的契约设计经得起验证"** — reset / step / snapshot / restore
   六个方法走完 gRPC 后 suite 级等价，说明抽象设计没漏。

3. **"跨语言边界想清了"** — proto 有 oneof（Action）+ bytes（Snapshot）+
   JSON string（复杂 domain）三种策略组合，说明能处理"强类型 vs
   灵活字段"的权衡。

4. **"离真 Unity 就差一步"** — 把 `mock_server.py` 里的 PySim 换成 Unity
   C# server（`UnityBridge.cs` 已经有骨架），其余代码零改动。

---

## 什么时候做 Stage 6

按 plan 推荐顺序是 Stage 5 之后；当前状态：
- Stage 1 ✅ Agent eval harness
- Stage 2 ✅ Prompt iteration doc
- Stage 3 ✅ LLM provider 对比
- Stage 4 ✅ CI
- Stage 5 ✅ DEMO.md
- Stage 6 ⏭ 本文档即方案

**建议**：如果还有 6-8 小时，就做。做完 `gameguard info` 里的 "Sandbox.unity:
headless" 能从 "proto 就绪" 变成 "✓ mock server ready"，加分点直接 visible。

如果时间紧，这份方案文档就够——面试可以说 "Stage 6 我把实施路径都写清楚了
（见 docs/stage6-unity-mock-plan.md），实施周末一天能做完，只是当前优先
级在 eval 数据和 prompt 迭代上"，完全合理。

---

*方案撰写：2026-04-18*
