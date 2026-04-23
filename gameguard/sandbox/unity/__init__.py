"""Unity Adapter 子包（D11 stub）。

完整接入需要先生成 protobuf Python 模块；详见 ``proto/gameguard_v1.proto``
与 ``adapter.py`` 顶部的接入指南。
"""
from gameguard.sandbox.unity.adapter import PROTO_PATH, UnityAdapter

__all__ = ["PROTO_PATH", "UnityAdapter"]
