#!/usr/bin/env bash
# 从 gameguard_v1.proto 生成 Python gRPC stubs。
#
# 用法：
#   bash scripts/gen_unity_proto.sh
# 或：
#   make proto
#
# 前置：pip install -e ".[unity]"（装 grpcio + grpcio-tools + protobuf）
#
# 生成物落在 gameguard/sandbox/unity/generated/，**提交到仓库**，
# 这样 end-user 装包即跑不需要 grpcio-tools。
#
# 注意：grpcio-tools 生成的 _pb2_grpc.py 用 `import gameguard_v1_pb2` 绝对
# 路径导入，我们需要 fix 成相对导入，否则作为 package 引用会失败。

set -euo pipefail

# 环境守卫：必须在 gameguard conda env 里
if [ "${CONDA_DEFAULT_ENV:-}" != "gameguard" ]; then
  echo "错误：脚本必须在 conda env 'gameguard' 中运行，当前是 '${CONDA_DEFAULT_ENV:-<none>}'" >&2
  echo "请先：conda activate gameguard" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="${REPO_ROOT}/gameguard/sandbox/unity/proto"
OUT_DIR="${REPO_ROOT}/gameguard/sandbox/unity/generated"

mkdir -p "${OUT_DIR}"
# 保证是 Python package
touch "${OUT_DIR}/__init__.py"

python -m grpc_tools.protoc \
  -I "${PROTO_DIR}" \
  --python_out="${OUT_DIR}" \
  --grpc_python_out="${OUT_DIR}" \
  "${PROTO_DIR}/gameguard_v1.proto"

# 把 "_pb2_grpc.py" 里的 `import gameguard_v1_pb2` 改成相对导入
# 否则 from gameguard.sandbox.unity.generated import gameguard_v1_pb2_grpc 会失败
PB2_GRPC="${OUT_DIR}/gameguard_v1_pb2_grpc.py"
if [ -f "${PB2_GRPC}" ]; then
  # 兼容 BSD / GNU sed
  if sed --version >/dev/null 2>&1; then
    sed -i 's/^import gameguard_v1_pb2 /from . import gameguard_v1_pb2 /' "${PB2_GRPC}"
  else
    sed -i '' 's/^import gameguard_v1_pb2 /from . import gameguard_v1_pb2 /' "${PB2_GRPC}"
  fi
fi

echo "生成完成："
ls -la "${OUT_DIR}"
