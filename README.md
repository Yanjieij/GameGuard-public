# GameGuard

[![CI](https://github.com/Yanjieij/GameGuard-public/actions/workflows/ci.yml/badge.svg)](https://github.com/Yanjieij/GameGuard-public/actions/workflows/ci.yml)
![pytest](https://img.shields.io/badge/pytest-162%20passed%20%2B%203%20skipped-brightgreen)
![python](https://img.shields.io/badge/python-3.11-blue)
[![Evaluation](https://img.shields.io/badge/eval-EVAL.md-0F766E)](EVAL.md)
[![Demo](https://img.shields.io/badge/demo-DEMO.md-1D4ED8)](DEMO.md)

[English README](README_en.md)

GameGuard 是一个面向游戏测试场景的 LLM Agent 自动化 QA 框架。它可以读取策划文档，
将设计约束转化为可执行测试计划，在确定性沙箱中执行测试，并输出回归分析报告和
Jira 风格的缺陷摘要。

整个系统围绕一条完整链路展开：

`策划文档 -> 不变式 -> 测试计划 -> 沙箱执行 -> 回归差分 -> 缺陷报告`

这个项目的重点不是“做一个会聊天的 Agent”，而是展示 AI Agent 如何真正落进一条
可复现、可审查、可演示的工程化测试流程。

## 项目概览

| 模块 | GameGuard 提供的能力 |
|---|---|
| Planning | 设计文档解析、不变式提取、测试用例生成、探索式测试生成 |
| Execution | 确定性执行、回放、快照、trace 记录 |
| Regression | baseline 与 candidate 的差分分析，区分 stable / new / fixed failure |
| Triage | 失败聚类、Jira-compatible BugReport 输出 |
| Coverage | 技能/战斗沙箱、任务/3D 沙箱、Unity 接入路径 |

## 展示亮点

- 五个 Agent 组成的完整流水线：`DesignDoc`、`TestGen`、`Exploratory`、`Triage`、`Critic`
- 使用 YAML 测试计划和结构化不变式作为中间产物，便于人和 Agent 共同审查
- 两类沙箱：
  - `PySim`：面向技能系统和战斗逻辑的测试
  - `QuestSim`：面向任务、对话、寻路、存档和 3D 交互的测试
- 确定性执行、回放、缓存、预算控制与 trace 调试
- Markdown 和 HTML 两种回归报告输出
- 通过 gRPC adapter 与 mock server 提供 Unity 接入路径

## 为什么这个项目有意思

很多 Agent Demo 只停留在“模型生成了一段答案”，而 GameGuard 更关注真实 QA 流程里
最重要的几件事：

- Agent 产出的是结构化测试资产，而不是一次性文本
- 执行结果是确定性的，可以重放和复现
- 回归分析是 baseline 与 candidate 的比较，而不是单次跑通就结束
- 失败不会停留在日志层面，而是继续收敛成缺陷报告

因此它更像一个内部工程工具原型，而不是一次性的展示脚本。

## 架构

![GameGuard Architecture](docs/architecture.drawio.png)

GameGuard 可以分为几层稳定的结构：

| 层级 | 职责 |
|---|---|
| `CLI` | `generate`、`run`、`regress`、`triage`、`info` 等入口 |
| `Orchestrator` | 组织 plan-and-execute 流程 |
| `Agents` | 基于统一 AgentLoop 的规划与缺陷分析角色 |
| `Tools` | 基于 Pydantic schema 的结构化工具接口 |
| `Domain` | 技能、任务、动作、事件、不变式等纯数据模型 |
| `Sandbox` | 通过统一 adapter 暴露的确定性执行后端 |
| `Reports` | 回归摘要、缺陷报告与 HTML 渲染 |
| `LLM Stack` | provider 抽象、缓存、预算控制、JSONL trace |

架构图源文件是 [`docs/architecture.drawio`](docs/architecture.drawio)，可以直接用
draw.io 继续编辑。

## 设计取舍

这个项目的很多设计都在强调“可观察”和“可复现”：

- 将规划与执行拆开，让昂贵的 LLM 过程沉淀成可复用产物
- 用 YAML 保存测试计划，让人类和 Agent 共享同一份可审查表示
- 让不同沙箱实现同一套 adapter 契约，保持上层逻辑稳定
- 把确定性视为核心特性，这样失败可以精确回放

这也是为什么它更像一条工程工具链，而不是一次运行的 Agent 实验。

## 演示场景

### 技能系统回归

给定一份技能策划文档，Agent 先生成测试用例，再用同一份计划比较 baseline 沙箱
和带有植入 bug 的 candidate 沙箱。

```bash
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml

gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html
```

`pysim:v2` 中预埋了五类代表性回归：

| Bug ID | 问题类型 | 示例 |
|---|---|---|
| `BUG-001` | 状态污染 | 切换技能时清掉无关技能的冷却 |
| `BUG-002` | 数值逻辑错误 | buff 刷新时错误地叠加 magnitude |
| `BUG-003` | 状态机泄漏 | 施法被打断后没有正确返还 MP |
| `BUG-004` | 精度问题 | DoT 走了浮点累加路径 |
| `BUG-005` | 确定性破坏 | RNG 绕过了沙箱 seed |

### 任务与 3D 回归

`QuestSim` 沙箱覆盖分支任务、对话、寻路、存档和场景交互等行为。

![Harbor Quest DAG](docs/harbor_quest_dag.drawio.png)

```bash
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml \
                  --baseline questsim:v1-harbor --candidate questsim:v2-harbor \
                  --html artifacts/reports/regress_quest.html
```

该 candidate 沙箱中同样预埋了五类 bug，覆盖碰撞、分支 flag、NPC reset、序列化
以及导航图构建错误。

### 不变式覆盖关系

![Bug Invariant Matrix](docs/bug_invariant_matrix.drawio.png)

每个植入 bug 至少对应一条可检测不变式。整个回归流程被设计成：`v1` 全部通过，
`v2` 只产生针对性的 new failure，这样演示时更容易解释 bug 与 oracle 的关系。

## Plan-And-Execute 流程

![Agent Pipeline](docs/agent_pipeline.drawio.png)

整个流水线被明确拆成两个阶段：

- `Plan`：LLM Agent 阅读文档、提取不变式、生成测试计划
- `Execute`：Runner 在确定性沙箱中执行计划并输出 trace 与报告

这样做的好处是，中间产物可以保存、审查、复跑，而执行阶段则更加便宜、稳定且适合 CI。

## 核心组件

| 组件 | 作用 |
|---|---|
| `DesignDocAgent` | 从设计文档中提取可机器检查的不变式 |
| `TestGenAgent` | 将不变式转为可执行 YAML 测试计划 |
| `ExploratoryAgent` | 生成更偏对抗、探索式的测试用例 |
| `TriageAgent` | 聚类失败并输出 Jira-compatible BugReport |
| `CriticAgent` | 审查并修补质量不足的生成用例 |
| `Runner` | 在指定沙箱上确定性执行测试计划 |
| `Reports` | 生成 Markdown 与 HTML 报告 |

## 评测

仓库中既包含端到端 demo，也包含评测脚本。详细结果可以看 [EVAL.md](EVAL.md)
和 [`evals/`](evals) 目录。

几个高层结论：

- 手写回归用例对植入 bug 达到完整召回
- 从文档到报告的 Agent 流程已经可以完整跑通
- 不同 provider 在 tool-calling 工作负载上的表现差异明显
- Agent 生成用例已经有实用价值，但仍显著弱于精心编写的基线用例

这个差距本身也是项目的一部分：它更接近真实工程场景中的“可审查、可改进”的
Agent，而不是被包装成“自动完美”的系统。

## 快速开始

环境要求：

- Python 3.11
- 建议使用 Conda

```bash
conda env create -f environment.yml
conda activate gameguard
pip install -e ".[dev]"
pytest -q
gameguard info
```

可选扩展：

```bash
pip install -e ".[dev,physics]"
pip install -e ".[dev,unity]"
```

如果需要启用依赖 LLM 的 planning 流程：

```bash
cp .env.example .env
```

然后在 `.env` 中配置至少一个支持的 provider。

当前本地验证的预期结果：

- `162 passed + 3 skipped`

## 常用命令

```bash
gameguard info
gameguard generate --doc docs/example_skill_v1.md --out testcases/skill_system/agent_generated.yaml
gameguard regress --plan testcases/skill_system/handwritten.yaml --baseline pysim:v1 --candidate pysim:v2 --html artifacts/reports/regress.html
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml --baseline questsim:v1-harbor --candidate questsim:v2-harbor --html artifacts/reports/regress_quest.html
```

退出码约定：

- `0`：全部通过
- `1`：存在失败
- `2`：执行出错

## 支持的沙箱

| Sandbox | 用途 |
|---|---|
| `pysim:v1` / `pysim:v2` | 技能系统与战斗逻辑测试 |
| `questsim:v1-harbor` / `questsim:v2-harbor` | 任务、对话、寻路和场景回归 |
| `questsim:v1+pybullet` | 带可选 physics backend 的 Quest 沙箱 |
| `unity:mock` | 预录 trace 的 Unity-facing adapter |
| `unity:headless` | 通过 mock Unity server 的 gRPC 路径 |
| `unity:headless+pysim:v2` / `unity:headless+questsim` | 显式指定 headless adapter 背后的后端 |

## 仓库结构

```text
gameguard/     核心包
tests/         自动化测试
testcases/     可执行 YAML 测试计划
docs/          架构图与集成文档
evals/         评测脚本与结果汇总
artifacts/     运行产物，默认忽略，仅保留占位
```

## 典型用途

- 从游戏策划文档生成测试计划
- 比较不同沙箱版本的确定性回归结果
- 在面试或分享场景中展示 bug 发现与 triage 流程
- 评估不同模型在 tool-calling Agent 工作负载上的表现
- 验证 Unity-facing QA 自动化路径的可行性

## 面试演示建议

如果你想用它做现场演示，最顺的路径通常是：

1. 先展示架构图
2. 运行 `gameguard info`
3. 跑一条 `PySim` 回归流程
4. 打开生成的 HTML 报告
5. 顺着一个 failure trace 讲到对应 bug report

更完整的演示脚本见 [DEMO.md](DEMO.md)。

## Public Release Notes

这个公开仓是面向作品展示的版本。如果你想继续整理或做更干净的 portfolio release，
可以先看：

- [docs/public_release_checklist.md](docs/public_release_checklist.md)
- [docs/public_repo_manifest.md](docs/public_repo_manifest.md)

## 限制

- 这是一个适合作品展示的原型，不是生产级游戏测试平台
- 视觉表现、动画质量、渲染正确性不在当前范围内
- Agent 生成测试的质量仍落后于精心编写的回归基线

## License

See `LICENSE`.
