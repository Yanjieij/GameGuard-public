"""自动生成的 gRPC stubs（由 scripts/gen_unity_proto.sh 生成）。

**不要手改本目录文件**——任何 proto schema 变更后运行：

    make proto

会重新生成 gameguard_v1_pb2.py / gameguard_v1_pb2_grpc.py。

为何提交到仓库：让普通用户 ``pip install -e .`` 就能跑，不需要再装
``grpcio-tools``。只有协议本身修改时才需要重生。
"""
