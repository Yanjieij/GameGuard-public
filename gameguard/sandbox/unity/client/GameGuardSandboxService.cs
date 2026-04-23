// GameGuardSandboxService.cs
// MagicOnion service 实现：对应 gameguard_v1.proto 的 GameGuardSandbox service。
//
// ============================================================================
// 文件状态：最小可编译骨架（在 Unity 2022.3 + MagicOnion 5.x 环境下能编译）
// ============================================================================
// 本骨架演示"Unity 侧 gRPC server 怎么落地"的骨架级代码，不依赖具体游戏
// 的 domain 类型。真接时需要：
//   1) 从 gameguard_v1.proto 生成 C# proto 类（通过 MagicOnion.Generator）
//   2) 把 TODO: 标的部分替换为真实 game logic 调用
//   3) 在 Unity 项目 Assets/Scripts/GameGuard/ 下放这 3 个 .cs 文件
//
// 大厂模式参考：
//   - 米哈游 "Ares" / 网易 "Mongoose" 之类的 QA 框架都用类似的
//     "engine 起 gRPC server，test driver 外部连" 拓扑
//   - Cygames 的 MagicOnion 方案：https://github.com/Cysharp/MagicOnion
// ============================================================================

#if UNITY_2021_3_OR_NEWER
using System;
using System.Collections.Generic;
using System.Threading;
using Cysharp.Threading.Tasks;          // UniTask：Unity 协程 ⇄ async 桥
using MagicOnion;
using MagicOnion.Server;
// using GameGuard.Unity.V1;            // 由 MagicOnion.Generator 从 proto 生成

namespace GameGuard.Bridge
{
    /// <summary>
    /// 对应 proto 里的 GameGuardSandbox service。每个 RPC 都在 RPC 线程被调用，
    /// 涉及 Unity API 的操作必须先 Post 到 main thread。
    /// </summary>
    public sealed class GameGuardSandboxService /* : ServiceBase<IGameGuardSandbox>, IGameGuardSandbox */
    {
        // TODO: 替换为真实游戏的事件总线（CombatEventBus / QuestEventBus 等）。
        private readonly EventBuffer _eventBuffer = new EventBuffer();
        private long _seed;
        private long _tick;

        // ---- Reset ---------------------------------------------------------

        public async UniTask<StateResponseStub> Reset(ResetRequestStub request)
        {
            _seed = (long)request.Seed;
            _tick = 0;

            // 跨线程跳回 Unity main thread 执行 Scene 重置
            await MainThreadDispatcher.InvokeAsync(() =>
            {
                UnityEngine.Random.InitState((int)(_seed & 0x7FFFFFFF));
                // TODO: 调用游戏的 SceneManager.Reset / ScriptableObject.Reinitialize()
                // TODO: 按 request.SandboxSpec 路由到对应关卡 / 测试场景
                _eventBuffer.Clear();
            });

            return BuildStateResponse();
        }

        // ---- Step ----------------------------------------------------------

        public async UniTask<StepResponseStub> Step(StepRequestStub request)
        {
            var cmd = TranslateAction(request.Action);
            int before = _eventBuffer.Count;

            try
            {
                await MainThreadDispatcher.InvokeAsync(() =>
                {
                    // TODO: ApplyCommand + 推 FixedUpdate N 次
                    ApplyCommandOnMainThread(cmd);
                });
                // 固定 timestep 下推一帧物理；真实游戏里可能是 N 帧
                await UniTask.WaitForFixedUpdate();
            }
            catch (Exception e)
            {
                return new StepResponseStub
                {
                    State = BuildStateResponse(),
                    Accepted = false,
                    Reason = e.Message,
                    NewEvents = 0,
                    Done = false,
                };
            }

            _tick++;
            return new StepResponseStub
            {
                State = BuildStateResponse(),
                Accepted = true,
                Reason = string.Empty,
                NewEvents = (uint)(_eventBuffer.Count - before),
                Done = false,
            };
        }

        // ---- StreamEvents (server-streaming) -------------------------------

        public async UniTask StreamEvents(
            StreamEventsRequestStub request,
            Func<EventBatchStub, UniTask> writeAsync,
            CancellationToken ct)
        {
            // 订阅 EventBuffer，每有新事件就 push 一个 batch。
            // TODO: 真实项目里改成 Reactive（UniRx / R3 等）更简洁。
            int lastCursor = 0;
            while (!ct.IsCancellationRequested)
            {
                var snap = _eventBuffer.Snapshot();
                if (snap.Count > lastCursor)
                {
                    var slice = new List<EventStub>(snap.Count - lastCursor);
                    for (int i = lastCursor; i < snap.Count; i++)
                    {
                        if (request.KindFilter == null || request.KindFilter.Count == 0
                            || request.KindFilter.Contains(snap[i].Kind))
                        {
                            slice.Add(snap[i]);
                        }
                    }
                    lastCursor = snap.Count;
                    if (slice.Count > 0)
                    {
                        await writeAsync(new EventBatchStub { Events = slice });
                    }
                }
                await UniTask.Delay(TimeSpan.FromMilliseconds(50), cancellationToken: ct);
            }
        }

        // ---- Snapshot / Restore / Info ------------------------------------

        public UniTask<SnapshotBlobStub> Snapshot()
        {
            // TODO: 序列化 Scene + ScriptableObject 状态到 byte[]
            var data = Array.Empty<byte>();
            return UniTask.FromResult(new SnapshotBlobStub { Data = data, FormatVersion = "v1" });
        }

        public async UniTask<StateResponseStub> Restore(SnapshotBlobStub blob)
        {
            await MainThreadDispatcher.InvokeAsync(() =>
            {
                // TODO: 反序列化并 Apply 到当前 Scene
            });
            return BuildStateResponse();
        }

        public UniTask<AdapterInfoStub> Info()
        {
            return UniTask.FromResult(new AdapterInfoStub
            {
                Name = "unity-headless",
                Version = "0.1-minimal-skeleton",
                Deterministic = true,
                EngineVersion = UnityEngine.Application.unityVersion,
                ProjectName = UnityEngine.Application.productName,
            });
        }

        // ---- Helpers -------------------------------------------------------

        private StateResponseStub BuildStateResponse()
        {
            // TODO: 从真实 game state 收集 Character / Buff / Cooldown
            return new StateResponseStub
            {
                T = _tick * UnityEngine.Time.fixedDeltaTime,
                Tick = (ulong)_tick,
                Seed = (ulong)_seed,
                Characters = new List<CharacterStub>(),
                RngDraws = 0,
            };
        }

        private object TranslateAction(ActionStub action)
        {
            // TODO: 根据 oneof variant 翻成游戏内 Command 类型
            return new object();
        }

        private void ApplyCommandOnMainThread(object cmd)
        {
            // TODO: 游戏里真正 Apply 命令（e.g. PlayerController.Cast(skillId)）
        }
    }

    // -------------------------------------------------------------------------
    // 以下 *Stub 类型代表 MagicOnion.Generator 会生成的 proto-gen 类。
    // 真接时删除这些并 using GameGuard.Unity.V1。保留这里是为了本骨架在
    // Unity 之外也能 type-check。
    // -------------------------------------------------------------------------

    public class ResetRequestStub { public ulong Seed; public string SandboxSpec = ""; }
    public class StepRequestStub { public ActionStub Action = new ActionStub(); }
    public class ActionStub { /* oneof variant 的占位 */ }
    public class StateResponseStub
    {
        public double T;
        public ulong Tick;
        public ulong Seed;
        public List<CharacterStub> Characters = new();
        public ulong RngDraws;
    }
    public class CharacterStub { public string Id = ""; public double Hp; }
    public class StepResponseStub
    {
        public StateResponseStub State = new();
        public bool Accepted;
        public string Reason = "";
        public uint NewEvents;
        public bool Done;
    }
    public class StreamEventsRequestStub { public List<string> KindFilter = new(); }
    public class EventBatchStub { public List<EventStub> Events = new(); }
    public class EventStub { public ulong Tick; public double T; public string Kind = ""; }
    public class SnapshotBlobStub { public byte[] Data = Array.Empty<byte>(); public string FormatVersion = "v1"; }
    public class AdapterInfoStub
    {
        public string Name = ""; public string Version = ""; public bool Deterministic;
        public string EngineVersion = ""; public string ProjectName = "";
    }

    // -------------------------------------------------------------------------
    // EventBuffer：线程安全的事件累积器。真接时换成项目自己的 EventBus。
    // -------------------------------------------------------------------------
    internal sealed class EventBuffer
    {
        private readonly object _sync = new object();
        private readonly List<EventStub> _events = new();
        public int Count { get { lock (_sync) return _events.Count; } }
        public void Append(EventStub e) { lock (_sync) _events.Add(e); }
        public void Clear() { lock (_sync) _events.Clear(); }
        public List<EventStub> Snapshot()
        {
            lock (_sync) return new List<EventStub>(_events);
        }
    }
}
#endif
