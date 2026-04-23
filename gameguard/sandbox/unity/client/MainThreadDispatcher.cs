// MainThreadDispatcher.cs
// RPC 线程 → Unity main thread 的桥接。
//
// ============================================================================
// 这是真实 Unity gRPC 集成里**绕不过去**的一块：Unity API（Transform,
// SceneManager, Instantiate, Destroy 等）基本都必须在主线程调用；但
// gRPC server 是在 ThreadPool 上 dispatch RPC 的。两者必须用某种"把任务
// 丢进队列，Update() 里 Pump"的机制衔接。
//
// 本实现抄真实大厂 QA 工具的做法：
//   1. 一个静态 ConcurrentQueue<Action> 存待执行的动作
//   2. 一个 MonoBehaviour 在 Update() 里 Pump 队列
//   3. 对外暴露 InvokeAsync(Action)，RPC handler 用 await 它
// ============================================================================

#if UNITY_2021_3_OR_NEWER
using System;
using System.Collections.Concurrent;
using Cysharp.Threading.Tasks;
using UnityEngine;

namespace GameGuard.Bridge
{
    /// <summary>
    /// Dispatch Actions onto the Unity main thread. 必须在某处 Instantiate
    /// 一个持有本 Component 的 GameObject（由 UnityBridge.Bootstrap 负责）。
    /// </summary>
    public sealed class MainThreadDispatcher : MonoBehaviour
    {
        private static readonly ConcurrentQueue<Action> _queue = new();
        private static MainThreadDispatcher _instance;

        internal static void EnsureInstance()
        {
            if (_instance != null) return;
            var go = new GameObject("[GameGuard.MainThreadDispatcher]");
            DontDestroyOnLoad(go);
            _instance = go.AddComponent<MainThreadDispatcher>();
        }

        /// <summary>从任意线程调用：把 ``action`` 排进 main thread，等待完成。</summary>
        public static UniTask InvokeAsync(Action action)
        {
            EnsureInstance();
            var tcs = new UniTaskCompletionSource();
            _queue.Enqueue(() =>
            {
                try { action(); tcs.TrySetResult(); }
                catch (Exception e) { tcs.TrySetException(e); }
            });
            return tcs.Task;
        }

        /// <summary>同步排入（不等待）。</summary>
        public static void Post(Action action)
        {
            EnsureInstance();
            _queue.Enqueue(action);
        }

        /// <summary>Update() 里 Pump 队列。一次 Update 最多处理 MaxPerFrame 个。</summary>
        private const int MaxPerFrame = 64;
        private void Update()
        {
            int n = 0;
            while (n++ < MaxPerFrame && _queue.TryDequeue(out var action))
            {
                try { action(); }
                catch (Exception e)
                {
                    Debug.LogError($"[GameGuard] MainThreadDispatcher action 抛异常：{e}");
                }
            }
        }

        private void OnDestroy()
        {
            if (_instance == this) _instance = null;
        }
    }
}
#endif
