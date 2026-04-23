# Unity 集成指南（D11 stub）

> **本文档目标**：说明 GameGuard 当前已完成的 Unity 集成**架构**与
> 真实接入需要补的工作量。**当前不要求 Unity 编译跑通**，但所有接入点
> 已就位，未来 2-3 周内可上手实现。

---

## 1. 已完成的部分（可立即使用）

### 1.1 协议定义
- [`gameguard/sandbox/unity/proto/gameguard_v1.proto`](../gameguard/sandbox/unity/proto/gameguard_v1.proto)
  —— gRPC 服务定义，包含：
  - `Reset` / `Step` / `QueryState` / `Snapshot` / `Restore` —— 与
    `GameAdapter` ABC 一一对应
  - `StreamEvents` —— server-streaming，Unity PlayMode 实时推事件
  - `AdapterInfo` / 元数据 RPC

### 1.2 Python 客户端骨架
- [`gameguard/sandbox/unity/adapter.py`](../gameguard/sandbox/unity/adapter.py)
  —— `UnityAdapter(GameAdapter)`：
  - `from_endpoint(host, port)`：将来连真实 Unity（暂抛 `NotImplementedError`
    指向接入清单）
  - `from_mock(trace_path)`：**当前可用**，用预录 JSONL trace 跑 mock 模式

### 1.3 Unity C# bridge 骨架（伪代码）
- [`gameguard/sandbox/unity/client/UnityBridge.cs`](../gameguard/sandbox/unity/client/UnityBridge.cs)
  —— 文档级 C# 文件，展示：
  - `[InitializeOnLoad]` + `EditorApplication.playModeStateChanged` 启动
    gRPC server
  - `GameGuardBridgeService` 实现 proto 定义的 6 个 RPC
  - `EventBus` 把 Unity 内部事件转 protobuf 推送
  - 用 `UnitySynchronizationContext` 把 RPC 调度到主线程

### 1.4 CLI 路由
```bash
gameguard run --plan ... --sandbox unity:mock      # ← 立即可用
gameguard run --plan ... --sandbox unity:headless  # ← 抛 NotImplementedError
```

### 1.5 GameGuard `info` 自报
```bash
$ gameguard info
... Sandbox.unity:mock      ✓  用预录 trace 跑 mock
    Sandbox.unity:headless  proto 就绪（gRPC server 待实现）
```

---

## 2. 完整接入需要补的工作（预计 2-3 周）

### 第 1 周：Python 端
1. `pip install grpcio-tools` 后编译 proto：
   ```bash
   python -m grpc_tools.protoc \
       -I gameguard/sandbox/unity/proto \
       --python_out=gameguard/sandbox/unity/_generated \
       --grpc_python_out=gameguard/sandbox/unity/_generated \
       gameguard/sandbox/unity/proto/gameguard_v1.proto
   ```
2. 把生成的 `gameguard_v1_pb2.py` / `gameguard_v1_pb2_grpc.py` 接入 `UnityAdapter`：
   ```python
   from gameguard.sandbox.unity._generated import gameguard_v1_pb2 as pb
   from gameguard.sandbox.unity._generated import gameguard_v1_pb2_grpc as pb_grpc
   ...
   self._stub = pb_grpc.GameGuardSandboxStub(grpc.insecure_channel(self.endpoint))
   ```
3. 实现各 `from_endpoint` 路径下的 RPC 调用（替换 `NotImplementedError`）。
4. 加 unit test 用 grpcio 内置的 `aio.server` mock server 跑通往返。

### 第 2 周：Unity C# 端
1. Unity 项目里 PackageManager 装 `com.unity.grpc`（需 Unity 2022.3+）。
2. 用 `protoc + grpc_csharp_plugin` 编译同一份 proto 到 `Assets/Generated/`。
3. 把 [`UnityBridge.cs`](../gameguard/sandbox/unity/client/UnityBridge.cs)
   复制到 Unity 项目的 `Assets/Editor/`，按伪代码实现：
   - 替换 `BuildStateResponse()` 为真实的状态收集
   - 替换 `ApplyCommandOnMainThread()` 为通过你的 SkillSystem 单例派发
   - 把游戏内 `OnCastStart` / `OnDamageDealt` 等事件接入 `EventBus`
4. 在 Unity 里跑 `Unity.exe -batchmode -nographics` 启动 PlayMode，server 起在 :50051。

### 第 3 周：联调与 CI
1. `gameguard run --sandbox unity:headless` 在本地跑通一个简单技能用例。
2. 加到 CI（Jenkins/GitLab）：
   - 启动 Unity headless 进程作为 sidecar
   - GameGuard runner 连过去跑
   - 退出码与 sandbox 保持一致
3. 加 e2e 集成 test：起 Unity → 跑 plan → 比对 trace。

---

## 3. 设计权衡说明

### 3.1 为什么是 gRPC？
- **双向流**：StreamEvents 让事件实时推送，避免轮询延迟
- **强类型**：protobuf 跨 Python/C# 一致 schema
- **业内默认**：Unity 官方有 `com.unity.grpc` 包，腾讯/网易内部 QA 桥接
  也走 gRPC

### 3.2 为什么 mock 模式？
- 上层 Agent / Runner 集成测试不能等 Unity 编译跑起来（CI 慢得不可接受）
- mock = 用预录 JSONL trace 当 "tape recording"，可在 Python 单元测试里
  直接复用真实 trace 数据
- 这是**测试金字塔**的实践：单元测试用 mock、集成测试用真 Unity

### 3.3 `SandboxState.custom_fields` 是干嘛的？
- proto 已定义的字段是 GameGuard 关心的最小集（HP/MP/buff/CD/状态机）
- Unity 项目肯定还有 GameGuard 看不见的状态（位置、动画帧、UI 状态等）
- `custom_fields` (bytes) 让 Unity 端可以塞额外 protobuf-序列化的对象，
  给特定 invariant evaluator 解读 —— **不强迫 GameGuard 知道一切**

### 3.4 不接 ML-Agents 的理由
ML-Agents 是 Unity 官方的 RL 环境抽象，更偏"训练 RL agent 玩游戏"，对
我们"运行 spec-driven QA"的场景不必要。我们的 Adapter 比 ML-Agents 更
轻量、更聚焦 QA 用例。

---

## 4. 面试讲故事

JD 第 3 条："与研发团队合作，定制引擎和工具流"——这是面试官最关心的
"跨领域能力"。本目录的存在让讲述非常具体：

> "我没有真接入 Unity，因为那需要 2-3 周编译/调试。但我的架构层面已为
> 接入做好准备——proto 文件 12 个 RPC、Python 客户端骨架、C# bridge 伪代码、
> mock 模式让上层测试解耦。如果给我两周窗口，我能跑出第一版联调。"

打开三个文件：
1. `proto/gameguard_v1.proto` —— "我的协议设计：snapshot/restore round-trip
   是为了'一键复现 bug'，server-streaming 是为了不变式 EVERY_TICK 检查"
2. `client/UnityBridge.cs` —— "我知道 PlayMode 状态机、EditorApplication
   生命周期、UnitySynchronizationContext 这些坑"
3. `adapter.py` —— "上层代码完全无修改，换 factory 一行就切换"
