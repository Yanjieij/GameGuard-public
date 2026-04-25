# GameGuard

> **LLM Agent 驱动的游戏自动化测试系统。**
>
> 读策划文档（技能数值 / 任务流 / 3D 场景）→ Agent 抽机器可验证不变式 →
> Agent 生成测试用例 → 双沙箱（PySim 技能系统 + QuestSim 任务+3D）确定性执行 →
> 两阶段聚类产 Jira 兼容 bug 单 → v1 vs v2 差分回归输出 HTML 报告。

GameGuard 模拟真实游戏团队（米哈游 / 网易 / 腾讯）的 QA 工作流：策划写设计文档，
程序员发布 v2 实现引入若干 regression，GameGuard 的 **5 Agent 管线**仅凭策划文档
就能在 v1→v2 的回归中把 **10 个植入 bug（5 个技能 + 5 个任务/3D）** 全部抓出来。

**当前状态**：D1–D18 全部完成。160 tests 全绿（其中 3 条 pybullet 可选依赖 skip）。
~17k 行 Python（含 tests）。

---

## 1. 为什么做这个项目

行业里游戏自动化测试两条主流：

- **RL Agent 玩游戏**——NetEase Wuji（ASE 2019）、Tencent Juewu / 绝悟、
  EA SEED。难训、贵、对状态空间挑剔。
- **Spec-driven LLM Agent**——TITAN（arXiv 2025）部署到 8 条真实 QA 管线。
  工程量可控、推理可解释、和策划文档契合度高。

GameGuard 是**后一条路的完整工程参考实现**：从策划文档到 Jira bug 单全流程打通，
双沙箱覆盖米哈游 QA 真实工作量的 ~75%（技能数值 20% + 任务/3D 交互 55%），
对标米哈游 Agent 工程师 JD 三条核心方向：

| JD 方向 | 本项目体现 |
|---|---|
| 前沿 AI Agent 在游戏研发管线的落地 | 5 Agent 协作 · plan-and-execute · tool-calling 真实工业化 |
| Agent 驱动的研发工具链（代码审查 / 自动化测试 / 配置生成） | 文档→不变式→测试用例→执行→Jira bug 单 闭环 |
| 跨引擎 / 策划 / 程序协作重构工业化管线 | 双沙箱 + Unity gRPC proto 骨架 · 设计规范工程化 |

---

## 2. 完整工作流

![GameGuard 架构总览](docs/architecture.drawio.png)

> 源文件：[`docs/architecture.drawio`](docs/architecture.drawio)（draw.io 可编辑，PNG 已嵌入 XML，拖回 draw.io 即可二次修改）

**分层解读**（上到下）：
1. **CLI** — `run` / `generate` / `regress` / `triage` / `info` 五个子命令
2. **Orchestrator** — plan-and-execute 编排，串接各 Agent，预留 `review_hook`
3. **5 个 Agent** — DesignDoc / TestGen / Executor / Triage / Critic（stretch）
4. **Shared Tool Layer** — Pydantic schema → OpenAI function-calling，统一 schema 校验
5. **Domain Layer** — 纯数据模型（技能 + 任务 / 3D + Invariant + TestCase）
6. **GameAdapter ABC** — 唯一抽象：`reset / step / trace / snapshot / restore`
7. **双沙箱 + Unity 骨架** — PySim v1/v2 · QuestSim v1/v2 (+ pybullet) · UnityAdapter (proto)
8. **Reports** — Jira-compat BugReport · Markdown · HTML regress
- **右侧栏** 的 LLM Stack（LiteLLM + GLM/DeepSeek + Cache + Trace）由所有 Agent 共享

**两阶段拆分的工程意义**：
- **Plan 阶段** 是 ReAct/tool-calling 循环，开销在 LLM 调用上；产物（invariants/plan）落 YAML，下次复用。
- **Execute 阶段** 完全无 LLM，由 seed 决定结果；同 plan 跑两次必然产同样 trace（确定性）。

这种拆分是 LangGraph、AutoGen、Anthropic orchestrator-worker 等业界主流
multi-agent 模式的共同做法——**让昂贵 / 有随机性的部分尽量短，让 CI / 回归走纯
确定性路径**。

### 2.1 Plan-and-Execute 数据流详解

![Agent 管线数据流](docs/agent_pipeline.drawio.png)

上图把架构图里左右两条"半管道"铺开，显示每个 Agent 的 **输入 / 工具 / 产物** 三件套，
以及 YAML 产物如何跨越 Plan / Execute 边界。关键设计：

- **Plan 阶段**（绿）产物全部是 YAML，进 git 后能像 code 一样 review
- **Execute 阶段**（橙）无 LLM，`(plan, seed)` 二元组完全决定结果
- **TriageAgent** 只在有失败时被触发（节省 LLM 预算）
- **regress 模式** 把"同 plan 跑两次"打包成 NEW / FIXED / FLAKY 差分，输出 HTML

<details>
<summary>📄 点开看 ASCII fallback（供文本 diff 工具 / 终端 only 环境）</summary>

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CLI: generate / run / regress / triage / info                              │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
            ┌────────────────────────┼─────────────────────────┐
            │  Plan 阶段（LLM 参与）  │  Execute 阶段（纯确定性）│
            ▼                        │                         ▼
┌──────────────────────┐             │              ┌──────────────────────┐
│  Orchestrator         │            │              │   Runner              │
│  (plan-and-execute)   │            │              │   - reset(seed)       │
│  + review_hook        │            │              │   - 逐动作 step       │
└─────────┬─────────────┘            │              │   - 评估 invariants   │
          │                          │              │   - trace + snapshot  │
   ┌──────┼─────────┬──────────┐     │              └──────────┬───────────┘
   ▼      ▼         ▼          ▼     │                         ▼
 Doc   Test    Explorat.   Critic    │              ┌──────────────────────┐
 Agent Gen    (adverse)    review   ─┼──plan/review►│ Reports / Triage      │
   │      │       │          │      │              └──────────────────────┘
   ▼      ▼       ▼          ▼      │                        ▲
 docs  invars adv-cases  patches    │                        │
         │                          │              ┌─────────┴────────┐
         ▼                          │              │  GameAdapter     │
    Shared Tools ◄──────────────────┼─────────────►│  PySim v1/v2     │
    (Pydantic → OAI schema)         │              │  QuestSim v1/v2  │
                                    │              │  Unity (mock)    │
                                    │              └──────────────────┘
```
</details>

---

## 3. 演示故事线

### 3.1 技能系统闭环（PySim）

```bash
conda activate gameguard

# Step 1：让 Agent 读策划文档，生成测试 plan
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml
# → DesignDocAgent 抽 18 条 invariants
# → TestGenAgent 生成 8 条 testcases
# → 可选 --critic：CriticAgent 对每条 case 静态审查 + 决定 accept/patch/drop

# Step 2：在 v1（golden）上跑（黄金参考）
gameguard run --plan testcases/skill_system/agent_generated.yaml --sandbox pysim:v1

# Step 3：v1 vs v2 差分回归 + 自动 triage + HTML 报告
gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html
# → NEW: BUG-001 / BUG-002 / BUG-003 / BUG-004 / BUG-005 全到齐
# → 每个 NEW failure 附自动聚类产出的 Jira-format BugReport
# → HTML 可点 trace 链接看 EventLog
```

### 3.2 任务 + 3D 场景闭环（QuestSim，D12–D18 新增）

![初识港口分支任务 DAG](docs/harbor_quest_dag.drawio.png)

示例场景**"初识港口"**：2 条分支（帮船长 / 帮商人）+ 1 个汇流点（进仓库）+ 存档点 +
物理推箱谜题。v2 在分支 S2B 漏 set alliance flag（Q-BUG-002），走商人线的玩家
永远卡在汇流点 S3；同时 NavGrid 被误标 blocked 形成孤岛（Q-BUG-005），
空间 reachability 也被破坏。

```bash
# 手写的分支任务 plan（初识港口：2 条分支 + 1 个汇流 + save/load + 物理推箱）
gameguard run --plan testcases/quest_system/harbor_handwritten.yaml \
              --sandbox questsim:v1-harbor
# → v1 8/8 pass

# 对比 v2（植入 5 个 Quest bug）
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml \
                  --baseline questsim:v1-harbor --candidate questsim:v2-harbor \
                  --html artifacts/reports/regress_quest.html
# → NEW: Q-BUG-001 ~ Q-BUG-005 全捕获
```

### 3.3 植入 bug 一览（对标 NetEase Wuji 实证分类）

![Bug ↔ Invariant 映射矩阵](docs/bug_invariant_matrix.drawio.png)

**10 个植入 bug（PySim 5 + QuestSim 5）全部被至少一条 invariant oracle 抓到**——
这是 v1 100% pass / v2 精确 10 条 NEW failure 的保证。Bug 分类全部对标
NetEase Wuji (ASE 2019) 对 1349 个商业游戏 bug 的 4 类实证分类（状态污染 /
数值溢出 / 状态机泄漏 / 浮点精度）再加边界条件 / 序列化 / 确定性 / 场景数据。

| Bug ID | 真实分类 | v2 表现 | 抓它的 Invariant |
|---|---|---|---|
| **PySim BUG-001** | 状态污染 | 切换技能瞬间错误清空所有 cooldown | `cooldown_at_least_after_cast` |
| **PySim BUG-002** | 数值溢出 | 同 buff refresh 时累加 magnitude（应替换） | `buff_refresh_magnitude_stable` |
| **PySim BUG-003** | 状态机泄漏 | 打断未退款 mp | `mp_nonneg` + `interrupt_refunds_mp` |
| **PySim BUG-004** | 浮点精度 | DoT 浮点累加误差 | `dot_total_damage_within_tolerance` |
| **PySim BUG-005** | 确定性破坏 | 暴击 RNG 用全局 random 而非 sandbox seed | `replay_deterministic` |
| **QuestSim Q-BUG-001** | AABB 边界 | `<=` 写成 `<`，触发体边界失效 | `trigger_volume_fires_on_enter` |
| **QuestSim Q-BUG-002** | 分支死锁 | 商人分支漏 set alliance flag，下游 step 永不可达 | `quest_step_reachable` + `quest_no_orphan_flag` |
| **QuestSim Q-BUG-003** | NPC 漏重置 | quest_reset 只重置 pos 不重置 state.dict | `npc_respawn_on_reset` |
| **QuestSim Q-BUG-004** | 存档丢字段 | JSON 序列化替代 pickle，丢 Enum/Vec3 | `save_load_round_trip` |
| **QuestSim Q-BUG-005** | Nav 孤岛 | 场景加载误标 blocked，仓库入口成孤岛 | `path_exists_between` + `no_stuck_positions` |

---

## 4. 架构详解

### 4.1 多 Agent 拓扑（5 个 Agent · 职责分离）

| Agent | 输入 | 输出 | LLM 用途 |
|---|---|---|---|
| **Orchestrator** | doc paths / plan | 串接各 Agent + `review_hook` | 不直接调 LLM |
| **DesignDocAgent** | 策划文档 (md) | `InvariantBundle` | 阅读 + 抽契约 |
| **TestGenAgent** | invariants + 技能/任务/实体信息 | `TestPlan` (YAML) | 设计动作序列（discovery / prefetch 双模式） |
| **ExploratoryAgent** | skill book + invariants | 对抗式动作序列 | "好奇玩家"视角，尝试奇怪组合让 invariant 红 |
| **TriageAgent** | 失败用例集合 | `list[BugReport]` (Jira schema) | 两阶段聚类：规则 + LLM-as-judge |
| **CriticAgent** (stretch, D10) | 生成的 TestPlan | accept / patch / drop 决策 | 静态审查每条 case，`inspect_case` / `propose_fix` |

**AutoGen / Anthropic orchestrator-worker 模式**：各 Agent **只做一件事**，通过
同一套 Tool Registry + AgentLoop 协作，便于独立评估与替换。

### 4.2 手写 AgentLoop（不依赖 LangChain）

`gameguard/agents/base.py` ~200 行实现 production-grade tool-calling 循环：

- **plan-and-execute 兼容**：默认 ReAct 风格 while 循环；`tool_choice="required"`
  可强制每轮调工具，避免推理型模型把 max_tokens 烧在 reasoning_content 上
  （GLM-4.7 / Claude thinking / o1 的常见失败模式）
- **`stop_when` 收敛信号**：tool 执行后回调，典型用法 `lambda r: r.tool_name == "finalize"`
- **错误结构化反馈**：tool 失败时打包成 `role=tool` content 喂回 LLM 自修复（ReAct 论文核心模式）
- **token / USD 双重预算**：超出立即抛 `BudgetExceeded`，CI 安全
- **trace 全程发射**：JSONL 格式，OpenTelemetry GenAI 兼容字段

### 4.3 Tool 系统（Pydantic → OpenAI function-calling）

`gameguard/tools/schemas.py` 的 `Tool` + `ToolRegistry` 抽象：

- 输入用 Pydantic 模型 → `model_json_schema()` 自动产 OpenAI schema
- 输出可序列化对象 → 自动 JSON 化
- 错误统一转成 `ToolInvocationResult(ok=False, content="ERROR: ...")` 而非 raise

五组 tools：
- `doc_tools.py` — `list_docs` / `list_doc_sections` / `read_doc_section` / `read_full_doc`（沙箱化文档浏览）
- `testgen_tools.py` — `list_invariants` / `list_skills` / `list_quests` / `list_entities` / `emit_testcase` / `finalize`
- `triage_tools.py` — `list_failures` / `emit_bug_report` / `merge_clusters` / `finalize`
- `critic_tools.py` — `inspect_case` / `propose_fix` / `drop_case` / `accept_all`

设计意图：**emit-as-side-channel**——Agent 通过 `emit_*` 工具提交结构化产物，
比 `response_format=json_schema` 更稳健。

### 4.4 LLM 网关层（LiteLLM 多家通吃）

`gameguard/llm/client.py` 用 LiteLLM 统一封装：

- `provider/model` 统一语法（`zai/glm-4.6` / `deepseek/deepseek-chat` / ...）
- **磁盘缓存**（content-addressed）：相同 `(model, messages, tools, temperature, ...)` 命中即不再调 API。
  deterministic 模式下 miss 即抛 `CacheMissInStrictMode`，CI 不会意外花钱。
- **JSONL trace** + 钩子接口（可接 Langfuse/Phoenix）
- **`disable_thinking` 选项**：`extra_body={"thinking":{"type":"disabled"}}` 关闭推理型
  模型的内置思考。实测对 GLM-4.7 减时长 15.6×、减 token 130×

### 4.5 双沙箱 + Unity 骨架

```
GameAdapter (ABC)
├── reset(seed) -> SandboxState
├── step(action) -> StepResult
├── trace() -> EventLog
├── snapshot() / restore(bytes)   ← 一键复现 bug 用
└── info -> AdapterInfo

├── SandboxBase (共享底盘, D12 抽离)
│   ├── rng / tick 计数 / snapshot / restore / _emit
│   └── 主循环由各实现自己写

├── PySim (gameguard/sandbox/pysim/)
│   ├── 20 Hz 定步长 tick · 4 技能 + 3 buff + 暴击
│   ├── _TrackingRandom：所有 RNG 走 sandbox seed
│   ├── v1/skills.py — 黄金实现
│   └── v2/skills.py — 继承 v1 + 覆写 5 处植入 bug

├── QuestSim (gameguard/sandbox/questsim/)  ← D12-D18 新增
│   ├── core.py — 主循环（MoveTo / Interact / Dialogue / Save / Load）
│   ├── nav.py — A*（Manhattan 启发式）+ walkable_components (SCC)
│   ├── quest_runtime.py — QuestStep 状态机 + flag 命名空间（quest./dlg./sys./scene.）
│   ├── dialogue_runtime.py — DialogueGraph 跑动
│   ├── save_codec.py — PickleSaveCodec（v1）/ LossyJsonSaveCodec（v2 Q-BUG-004）
│   ├── physics/
│   │   ├── dummy.py — 纯 Python AABB + 速度积分（默认）
│   │   └── pybullet_backend.py — 可选，pip install gameguard[physics]
│   ├── scenes/harbor.py — 示例："初识港口"分支任务（2 分支 + 1 汇流 + 物理推箱）
│   ├── v1/handlers.py — 黄金实现
│   └── v2/handlers.py — 继承 v1 + 覆写 5 处植入 Q-BUG

└── UnityAdapter (gameguard/sandbox/unity/)
    ├── proto/gameguard_v1.proto — gRPC 协议（reset/step/query/snapshot）
    ├── adapter.py — UnityAdapter(GameAdapter) 骨架 + mock 模式
    └── client/UnityBridge.cs — C# 端骨架（文档级，不编译）
```

**设计决策**：QuestSim **不继承** PySim，而是抽公共基类 `SandboxBase`——避免被
技能语义拖累。两个沙箱各自实现主循环，共享 reset/snapshot/rng/emit 底盘。

### 4.6 Invariant DSL（pure data + 注册表 evaluator）

`gameguard/domain/invariant.py` 关键设计：

- 每条 Invariant 是 Pydantic 模型，按 `kind` discriminated union
- 评估器走 Python 注册表，**LLM 永远只能发结构化 JSON**，不能传 Python 代码
- 这避免了 "LLM 生成代码 → exec" 模式的安全风险

**已注册的 invariant kinds（19 种）**：

技能系统（9）：`hp_nonneg` / `mp_nonneg` / `cooldown_at_least_after_cast` /
`buff_stacks_within_limit` / `buff_refresh_magnitude_stable` /
`interrupt_clears_casting` / `interrupt_refunds_mp` /
`dot_total_damage_within_tolerance` / `replay_deterministic`

任务 + 3D（10，D17 新增）：`quest_step_reachable` / `quest_step_once` /
`quest_no_orphan_flag` / `trigger_volume_fires_on_enter` /
`npc_respawn_on_reset` / `save_load_round_trip` / `path_exists_between` /
`no_stuck_positions` / `dialogue_no_dead_branch` / `interaction_range_consistent`

### 4.7 测试用例 = 数据

`testcase/model.py` 中 `TestCase` 是 Pydantic 模型：

- LLM 直接产 YAML，免写 Python
- 进版本库后能像 code 一样 review（米哈游 / 网易内部 QA 用 TestRail / Xray
  导出格式即类似）
- `Runner` 三态分类（PASSED / FAILED / ERROR）
- trace + snapshot 全部落盘，每条失败都可复现

---

## 5. 文件结构

```
GameGuard/
├── README.md                           # 你正在看的文件
├── environment.yml                     # conda 环境（Python 3.11）
├── pyproject.toml                      # 依赖 + extras: [dev] / [physics]
├── Makefile
│
├── gameguard/
│   ├── cli.py                          # typer: run / generate / regress / triage / info
│   │
│   ├── agents/
│   │   ├── base.py                     # AgentLoop（手写 ~200 行）
│   │   ├── orchestrator.py             # plan-and-execute 串接 + review_hook
│   │   ├── design_doc.py               # 文档 → InvariantBundle
│   │   ├── test_gen.py                 # invariants → TestPlan（discovery/prefetch）
│   │   ├── exploratory.py              # 对抗式"好奇玩家" Agent（D8）
│   │   ├── triage.py                   # 失败 → BugReport（D7）
│   │   └── critic.py                   # 生成后静态审查（D10, stretch）
│   │
│   ├── tools/
│   │   ├── schemas.py                  # Tool / ToolRegistry / Pydantic→schema
│   │   ├── doc_tools.py                # 文档沙箱化浏览
│   │   ├── testgen_tools.py            # emit_testcase / list_skills / list_quests / ...
│   │   ├── triage_tools.py             # emit_bug_report / merge_clusters
│   │   └── critic_tools.py             # inspect_case / propose_fix / drop_case
│   │
│   ├── domain/                         # 纯数据，零 I/O
│   │   ├── skill.py / character.py / buff.py      # 技能系统
│   │   ├── geom.py / entity.py                    # 3D 原语 + 实体（D12）
│   │   ├── quest.py / scene.py / dialogue.py      # 任务 + 场景 + 对话（D13-15）
│   │   ├── action.py / event.py                   # Action/EventLog（union 扩展）
│   │   └── invariant.py                           # 17 种 invariant + evaluator 注册表
│   │
│   ├── sandbox/
│   │   ├── adapter.py                  # GameAdapter ABC
│   │   ├── base.py                     # SandboxBase（D12 抽离）
│   │   ├── pysim/
│   │   │   ├── core.py / factory.py    # 20Hz tick + 确定性 RNG
│   │   │   ├── v1/skills.py            # 黄金实现
│   │   │   └── v2/skills.py            # 5 个植入 bug
│   │   ├── questsim/                   # D12-D18 新增
│   │   │   ├── core.py / factory.py
│   │   │   ├── nav.py                  # A* + SCC
│   │   │   ├── quest_runtime.py / dialogue_runtime.py / save_codec.py
│   │   │   ├── physics/                # dummy + pybullet（可选）
│   │   │   ├── scenes/harbor.py        # 初识港口分支任务
│   │   │   ├── v1/handlers.py          # 黄金 Handler
│   │   │   └── v2/handlers.py          # 5 个 Q-BUG
│   │   └── unity/                      # D11
│   │       ├── adapter.py              # UnityAdapter + mock
│   │       ├── proto/gameguard_v1.proto
│   │       └── client/UnityBridge.cs
│   │
│   ├── testcase/
│   │   ├── model.py                    # TestCase / TestPlan / Result
│   │   ├── loader.py                   # YAML ↔ Pydantic（保留注释）
│   │   └── runner.py                   # 跑批 + StateView + snapshot
│   │
│   ├── reports/
│   │   ├── schema.py                   # SuiteReport + BugReport Jira
│   │   ├── markdown.py                 # 套件级 / bug 级
│   │   ├── html.py                     # Jinja2 HTML 报告（D9）
│   │   ├── regress.py                  # NEW / FIXED / FLAKY 差分
│   │   └── templates/*.html.j2
│   │
│   └── llm/
│       ├── client.py                   # LiteLLM + cache + budget + trace
│       ├── cache.py                    # content-addressed 磁盘缓存
│       └── trace.py                    # JSONL session trace
│
├── docs/
│   ├── architecture.drawio             # 可编辑架构图（draw.io）
│   ├── architecture.drawio.png         # 导出 PNG（嵌入 XML，可拖回编辑）
│   ├── example_skill_v1.md             # 技能策划文档（飞书风格，10 条 I-xx）
│   ├── unity_integration.md            # Unity 接入指引（proto/C# stub 说明）
│   └── dev-log.md                      # 每日 standup 风格 D1-D18 记录
│
├── testcases/
│   ├── skill_system/
│   │   ├── handwritten.yaml            # 10 条人写回归用例
│   │   └── agent_generated.yaml        # Agent 产物
│   └── quest_system/
│       └── harbor_handwritten.yaml     # 初识港口分支任务 8 条用例
│
├── tests/                              # 160 tests (157 pass + 3 skip pybullet)
│   ├── test_pysim_v1.py / test_pysim_v2_bugs.py
│   ├── test_questsim_d12.py ~ test_questsim_d17.py   # D12-D17 每日守护测试
│   ├── test_testcase_layer.py / test_agent_layer.py
│   ├── test_invariant_dot_replay.py    # D8 I-09/I-10
│   ├── test_property_v1.py             # hypothesis property-based
│   ├── test_triage.py / test_critic.py / test_regress.py
│   └── test_unity_adapter.py           # proto round-trip + mock
│
└── artifacts/                          # 运行产物（gitignored）
    ├── traces/*.jsonl                  # sandbox + LLM 双层 trace
    ├── snapshots/*.bin
    ├── suite.json                      # TestSuiteResult（事后 triage 复用）
    └── reports/*.{md,html}
```

---

## 6. 快速开始

**重要：使用专属 conda 环境，不要装到 `base`**。

```bash
# 1. 创建 conda env（Python 3.11）
conda env create -f environment.yml
conda activate gameguard

# 2. 安装项目（editable）
pip install -e ".[dev]"
# 想跑物理谜题再装 pybullet：
pip install -e ".[dev,physics]"

# 3. 配置 API key
cp .env.example .env
# 编辑 .env：
#   GAMEGUARD_MODEL=deepseek/deepseek-chat   # 推荐
#   DEEPSEEK_API_KEY=sk-xxxxx
# 也支持 zai/glm-4.6, anthropic/..., openai/...

# 4. 跑测试（不花钱）
pytest -q
# 应当 157 passed + 3 skipped (pybullet 未装)

# 5. 能力清单（面试演示开场）
gameguard info

# 6. 技能系统端到端 demo
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml \
                   --critic                         # 可选：CriticAgent 审查
gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html

# 7. 任务 + 3D 端到端 demo
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml \
                  --baseline questsim:v1-harbor --candidate questsim:v2-harbor \
                  --html artifacts/reports/regress_quest.html
# → HTML 含分支任务图 + save/load + 物理推箱 trace

# 退出码：0=全过 / 1=有失败 / 2=有错误（CI 兼容）
```

### 成本与确定性

- 单次 `gameguard generate` 在 DeepSeek 上约 200k tokens（~¥0.5），命中缓存后 ¥0
- `GAMEGUARD_DETERMINISTIC=1` 强制 temperature=0 + 必须命中缓存，CI 安全
- `GAMEGUARD_USD_BUDGET=0.50` 单次预算上限，超出立即抛 `BudgetExceeded`

### 支持的 Sandbox（`gameguard info` 实测）

| sandbox 字符串 | 说明 |
|---|---|
| `pysim:v1` | 技能系统 golden |
| `pysim:v2` | 技能系统 + 5 个植入 bug |
| `questsim:v1-harbor` | 任务/3D/寻路/对话/存档/物理 golden，初识港口场景 |
| `questsim:v2-harbor` | 任务系统 + 5 个植入 Q-BUG |
| `questsim:v1+pybullet` | 同上 + pybullet 物理 backend（需 `pip install gameguard[physics]`） |
| `unity:mock` | 预录 trace mock，证明接口可替换 |
| `unity:headless` | proto 就绪（gRPC server 待实现，2-3w 工作量） |

---

## 7. 关键技术决策 vs 行业实践

| 决策 | 项目里的体现 | 对标行业实践 / 文献 |
|---|---|---|
| Spec-driven 而非 RL-driven | DesignDocAgent + invariant DSL | TITAN (2025), Hypothesis, Jepsen |
| 多 Agent + 共享工具层 | 5 agent 各 1 文件，共享 ToolRegistry | AutoGen, Anthropic orchestrator-worker |
| plan-and-execute > pure ReAct | Plan 阶段→YAML→Execute 阶段 | LangGraph 长任务最佳实践 |
| Tool-as-side-channel 输出结构化 | `emit_invariant` / `emit_testcase` / `emit_bug_report` | OpenAI function-calling 进阶模式 |
| 错误结构化反馈让 LLM 自修复 | `ToolRegistry.dispatch` 永不 raise | ReAct (Yao 2022) 核心模式 |
| 10 类典型 bug 植入 | PySim 5 类 + QuestSim 5 类 | NetEase Wuji 4 oracle 分类 (ASE 2019) |
| 数据表共享，仅 handler 改 | v2 继承 v1 + 覆写 | 真实 PR review 视角 |
| Differential testing | `gameguard regress --baseline X --candidate Y` | Ubisoft / Blizzard 回归常用 |
| 测试用例 = 数据（YAML） | testcase/model.py + loader | TestRail / Xray-for-Jira 工业格式 |
| trace 用 JSONL | sandbox + LLM 双层 trace | OpenTelemetry, ELK, Loki |
| 确定性沙箱 + 注入 seed | _TrackingRandom + `replay_deterministic` oracle | 游戏 lockstep 同步 + LLM eval 通用 |
| 两阶段聚类（规则+LLM-as-judge） | TriageAgent | Jira 查重痛点学术方案 |
| LiteLLM 网关 + 多 provider | 一行 env 切换 | 生产系统避免 vendor lock-in |
| Disable thinking on tool-calling | `extra_body={"thinking":{"type":"disabled"}}` | 2026 推理型模型工程经验 |
| Property-based 测试策略 | `hypothesis` 随机 action 序列 | Jepsen, QuickCheck |
| 双沙箱 + SandboxBase 抽公共底盘 | `sandbox/base.py` 共享 rng/snapshot | 策略模式 / strangler fig 重构 |
| pybullet 可选 backend | `extras_require = {"physics": [...]}` | 工业 decoupled 依赖管理 |

---

## 8. 开发进度

详见 [`docs/dev-log.md`](docs/dev-log.md)。

**Week 1 — 骨架与核心闭环（D1-D7）** ✅ 全部完成
- D1-D2 域模型 + Invariant DSL + GameAdapter ABC + PySim v1
- D3 TestCase YAML + Runner + Markdown 报告 + 10 条手写回归
- D4 LLM 网关层（LiteLLM + cache + trace + budget）+ 工具层 + AgentLoop
- D5 DesignDocAgent + TestGenAgent + Orchestrator + CLI generate
- D6 PySim v2 植入 5 类 bug
- D7 TriageAgent + Jira-compatible BugReport（两阶段聚类）

**Week 2 — 深化与打磨（D8-D11 + D10 stretch）** ✅ 全部完成
- D8 Property-based（hypothesis）+ Exploratory Agent + I-09 DoT + I-10 replay determinism evaluator
- D9 `gameguard regress` + Jinja2 HTML 报告（NEW/FIXED/FLAKY）
- D10（stretch）CriticAgent + `review_hook` 接入
- D11 Unity Adapter proto 骨架 + C# bridge stub + mock 模式

**Week 3 — QuestSim 扩展（D12-D18）** ✅ 全部完成
- D12 SandboxBase 抽离 + geom/entity + QuestSim 空壳
- D13 Scene + NavGrid + A* + MoveToAction
- D14 Quest DAG + QuestRuntime + TriggerVolume + InteractAction
- D15 DialogueGraph + DialogueRuntime + SaveCodec (Pickle/LossyJson)
- D16 PhysicsBackend (dummy + pybullet) + 推箱场景
- D17 10 个新 invariant evaluator 全部注册
- D18 v2 植入 5 类 Q-BUG + 初识港口 YAML + E2E regress 验证

**测试数演进**：D6 32 条 → D11 105 条 → D18 **160 条（157 pass + 3 skip）**

---

## 9. 行业参考

- **NetEase Wuji** (ASE 2019) — 1349 个商业游戏 bug 的 4 类 oracle 实证分类
- **TITAN** (arXiv 2025) — LLM Agent 做 MMORPG QA，部署到 8 条生产管线
- **EA SEED** — DRL 辅助测试 AAA 工业实践
- **Tencent Juewu / 绝悟** — AI 做英雄平衡回归
- **Unity Test Framework** + `-batchmode -nographics` headless CI
- **Regression Games (regression.gg)** — LLM 驱动 UI 测试的商业尝试
- **Jepsen** / **Hypothesis** / **QuickCheck** — property-based testing 学术基础
- **AutoGen / LangGraph** — multi-agent 编排框架
- **Anthropic orchestrator-worker** — 本项目 Agent 拓扑的主要参考

---

## 10. License

面试 / portfolio 用途。暂未授权第三方分发。
