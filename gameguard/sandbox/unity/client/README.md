# GameGuard Unity Bridge · C# 端最小骨架

这里是"真实 Unity 侧要怎么接 GameGuard"的最小可编译骨架。**不要求**直接
在 Unity 项目编译通过（本 repo 没有 Unity 依赖），但每个文件的 using /
package reference / 方法签名都参照真实 game studio 的接入方式写，让
Unity 开发者看到骨架就能估工作量。

## 假设的技术栈

- **Unity 2022.3 LTS**（.NET Standard 2.1，支持 async/await）
- **MagicOnion 5.x**（Cysharp · Cygames 等日系大厂 Unity gRPC 方案，在
  grpc-dotnet 上包 MessagePack + async streaming）
- **Grpc.Core 2.46 fallback**（旧项目还在用 C-core；本骨架 #if 分支兜底）
- **UniTask 2.x**（MagicOnion 依赖，Unity 协程⇄async 桥）

> **为什么 MagicOnion 而非手写 Grpc.Core？**
> Cygames 披露过多款亿级流水手游的 QA / 热更新通道走 MagicOnion（Unity
> 2020 起对 grpc-dotnet 的 HTTP/2 兼容成熟）。米哈游 / 网易也有团队在用。
> 手写 Grpc.Core 在 Unity 里要自己封 RpcException ↔ UniTask，而
> MagicOnion 把这些都做了。

## 文件布局

```
client/
├── README.md                          ← 本文件
├── UnityBridge.cs                     ← MonoBehaviour 启动入口 + PlayMode hook
├── GameGuardSandboxService.cs         ← MagicOnion service 实现
├── MainThreadDispatcher.cs            ← RPC 线程 → Unity main thread 桥
└── packages.lock.sample               ← Unity PackageManager 依赖示例
```

## 集成步骤（面向 Unity 开发者）

1. 在 Unity 项目里装包：
   ```jsonc
   // Packages/manifest.json
   {
     "dependencies": {
       "com.cysharp.unitask": "2.5.0",
       "com.cysharp.magiconion": "5.1.9",
       "com.cysharp.magiconion.client.unity": "5.1.9"
     }
   }
   ```
2. 把 `gameguard_v1.proto` 通过 `protoc + grpc_csharp_plugin + MagicOnion.Generator`
   生成 C# 代码到 `Assets/Generated/GameGuard/`
3. 把本目录的 3 个 .cs 文件复制到 `Assets/Scripts/GameGuard/`
4. 启动参数（CI 拉起 Unity 时用）：
   ```bash
   ./Build.exe -batchmode -nographics -gameguard-port 50099
   ```
5. Python 侧：
   ```bash
   export GAMEGUARD_UNITY_ENDPOINT=127.0.0.1:50099
   gameguard run --plan ... --sandbox unity:headless
   ```

## 与真实大厂 QA 工具链的对齐

| 通用模式 | 本骨架的体现 |
|---|---|
| Unity headless 起 gRPC server 吃外部 test driver 指令 | `UnityBridge.cs` `RuntimeInitializeOnLoadMethod` |
| RPC 线程 → 主线程命令队列（Unity 非线程安全） | `MainThreadDispatcher.Pump()` in `Update()` |
| test driver 发 Action，引擎走 FixedUpdate 推进 | `GameGuardSandboxService.Step()` 里 `await UniTask.WaitForFixedUpdate()` |
| 事件流通过 server-streaming 推回 test driver | `StreamEvents` 用 `IAsyncEnumerable<EventBatch>` |
| 启动参数区分 QA build 和 production | `-gameguard-port` + `#if ENABLE_GAMEGUARD_BRIDGE` |

## 真实落地工作量估算

| 子项 | 工时 | 说明 |
|---|---|---|
| MagicOnion 包装 + 代码生成配置 | 0.5 天 | Unity-side 一次性配置 |
| Reset / Step 主线程桥接 | 1 天 | SynchronizationContext 细节 |
| Scene snapshot / restore（真实序列化） | 3-5 天 | MonoBehaviour + ScriptableObject 序列化 |
| 接入真实游戏的 event bus（cast / buff / damage） | 1-2 天 | 依赖项目 hook 点丰富度 |
| CI/CD 集成（Jenkins/GHA 拉 Unity headless） | 1 天 | 缓存 Library 是关键 |
| **合计** | **2-3 周** | 单 sandbox 类型 |

骨架里标的 `TODO:` 是每个真接时都要填的坑。
