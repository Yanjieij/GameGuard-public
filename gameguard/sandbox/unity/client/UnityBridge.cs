// UnityBridge.cs
// GameGuard Unity bridge 启动入口 + PlayMode hook。
//
// ============================================================================
// 本文件在 Unity 启动 / PlayMode 进入时起 gRPC server，退出时 graceful
// shutdown。生产里只在 `-gameguard-port` 命令行参数存在时启用，避免污染
// release build。
// ============================================================================

#if UNITY_2021_3_OR_NEWER
using System;
using Cysharp.Threading.Tasks;
using MagicOnion.Server;
using Grpc.Core;
using UnityEngine;

#if UNITY_EDITOR
using UnityEditor;
#endif

namespace GameGuard.Bridge
{
    /// <summary>
    /// Bootstrap：在 Unity 启动时读取 -gameguard-port 参数，起 gRPC server。
    /// 只在 batch mode 或 Editor PlayMode 启用；打包到 release build 无效。
    /// </summary>
    public static class UnityBridge
    {
        private static Server _server;
        private const string PortArg = "-gameguard-port";
        private const int DefaultPort = 50099;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void BootstrapOnLoad()
        {
            int port = ParsePortFromArgs();
            if (port <= 0) return; // 未指定则不启动，等同于 "GameGuard 不启用"

            try
            {
                MainThreadDispatcher.EnsureInstance();
                StartServer(port);
                Debug.Log($"[GameGuard] gRPC bridge listening on 127.0.0.1:{port}");

                Application.quitting += () =>
                {
                    Debug.Log("[GameGuard] Application quitting, shutting down bridge");
                    ShutdownServer();
                };
            }
            catch (Exception e)
            {
                Debug.LogError($"[GameGuard] Failed to start bridge: {e}");
            }
        }

        private static int ParsePortFromArgs()
        {
            var args = Environment.GetCommandLineArgs();
            for (int i = 0; i < args.Length - 1; i++)
            {
                if (args[i] == PortArg && int.TryParse(args[i + 1], out var p))
                    return p;
            }
            // Editor Play 时默认端口（方便本地手测）
#if UNITY_EDITOR
            return DefaultPort;
#else
            return 0;
#endif
        }

        private static void StartServer(int port)
        {
            // MagicOnion: 注册所有 ServiceBase 子类，这里只有 GameGuardSandboxService
            _server = new Server
            {
                // TODO: 真接时由 MagicOnionEngine 注册；骨架里留 API 示意
                // Services = { MagicOnionEngine.BuildServerServiceDefinition(typeof(GameGuardSandboxService)) },
                Ports = { new ServerPort("127.0.0.1", port, ServerCredentials.Insecure) },
            };
            _server.Start();
        }

        private static void ShutdownServer()
        {
            try
            {
                _server?.ShutdownAsync().Wait(TimeSpan.FromSeconds(2));
            }
            catch (Exception e)
            {
                Debug.LogWarning($"[GameGuard] Bridge shutdown warning: {e}");
            }
            finally { _server = null; }
        }

#if UNITY_EDITOR
        // Editor 退出 PlayMode 时立即关 server，否则下次 Enter PlayMode 端口冲突
        [InitializeOnLoadMethod]
        private static void InstallEditorHooks()
        {
            EditorApplication.playModeStateChanged += state =>
            {
                if (state == PlayModeStateChange.ExitingPlayMode)
                {
                    ShutdownServer();
                }
            };
        }
#endif
    }
}
#endif
