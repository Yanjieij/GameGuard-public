# GameGuard Makefile
# 所有 target 必须在 conda env `gameguard` 里跑。
# 不要装到 base——base 应当保持干净（见 README 的 quickstart）。
#
# check-env 护栏会在每个非 env/help target 之前阻断非 gameguard 环境的调用。

.PHONY: help env check-env install test lint typecheck demo regress clean \
        proto unity-server test-unity

help:
	@echo "GameGuard targets (先 conda activate gameguard)："
	@echo "  make env        创建 conda env (environment.yml)"
	@echo "  make install    pip install -e '.[dev]' 到当前 env"
	@echo "  make test       跑 pytest（自动校验 env）"
	@echo "  make lint       ruff check"
	@echo "  make typecheck  mypy --strict"
	@echo "  make demo       跑端到端 doc → bug report demo"
	@echo "  make regress    跑 v1 → v2 差分回归"
	@echo "  make eval       跑所有 Agent eval + rollup 到 EVAL.md"
	@echo "  make proto      从 .proto 重生 Python gRPC stubs"
	@echo "  make unity-server  启动 Stage 6 mock gRPC server (:50099)"
	@echo "  make test-unity    跑 unity:headless ↔ mock server E2E"
	@echo "  make clean      清 artifacts/ 和 __pycache__"

# 护栏：不是在 gameguard env 里就直接 fail，避免误装到 base。
check-env:
	@if [ "$$CONDA_DEFAULT_ENV" != "gameguard" ]; then \
		echo ""; \
		echo "✗ 当前 conda env = '$$CONDA_DEFAULT_ENV'，不是 'gameguard'。"; \
		echo "  请先跑：conda activate gameguard"; \
		echo "  （项目规则：不在 base 环境里装/跑 GameGuard 相关代码）"; \
		echo ""; \
		exit 1; \
	fi

env:
	conda env create -f environment.yml

install: check-env
	pip install -e ".[dev]"

test: check-env
	pytest -v

lint: check-env
	ruff check gameguard tests

typecheck: check-env
	mypy gameguard

demo: check-env
	gameguard run --doc docs/example_skill_v1.md --sandbox pysim:v2

regress: check-env
	gameguard regress --baseline v1 --candidate v2

eval: check-env
	python -m evals.design_doc.eval_design_doc --runs 1
	python -m evals.test_gen.eval_test_gen --runs 1
	python -m evals.triage.eval_triage --runs 1
	python -m evals.critic.eval_critic --runs 1
	python -m evals.rollup

clean:
	rm -rf artifacts/traces/* artifacts/reports/* artifacts/snapshots/*
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +

# ------------------------------------------------------------------------- #
# Stage 6 · Unity mock gRPC server
# ------------------------------------------------------------------------- #

proto: check-env
	bash scripts/gen_unity_proto.sh

# 前台启 mock server（Ctrl-C 退出）。端口可用 PORT=xxxx 覆盖。
unity-server: check-env
	python -m gameguard.sandbox.unity.mock_server --port $${PORT:-50099} -v

test-unity: check-env
	pytest tests/test_unity_e2e.py -v
