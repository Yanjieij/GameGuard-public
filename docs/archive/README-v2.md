# GameGuard

[![CI](https://github.com/Yanjieij/GameGuard/actions/workflows/ci.yml/badge.svg)](https://github.com/Yanjieij/GameGuard/actions/workflows/ci.yml)
![pytest](https://img.shields.io/badge/pytest-162%20passed%20%2B%203%20skipped-brightgreen)
![python](https://img.shields.io/badge/python-3.11-blue)
[![Agent 效果评估](https://img.shields.io/badge/eval-EVAL.md-8A2BE2)](EVAL.md)

让 LLM Agent 代替人类 QA 测试游戏：读一份策划文档，自动写出能跑的测试用例，
在沙箱里执行出发现 bug，出一份可以直接提到 Jira 的 bug 单。

这个项目做了 19 天，~17000 行 Python（含测试），162 条 pytest 全绿（含 5 条
真 gRPC E2E）。两个沙箱（技能系统 + 任务/3D 系统）各植了 5 个典型 bug，手写
回归用例 100% 召回；LLM Agent 自己生成的用例召回从 DeepSeek 的 20% 到
GPT-4.1 的 80%——跨 4 家 provider 的真实对比数字都在 [`EVAL.md`](EVAL.md) 里。

> 📓 上一版 README 保留在 [`docs/archive/README-v1.md`](docs/archive/README-v1.md)，
> 那一版写得比较像模板，这一版是重新写的，说人话的版本。

---

## 为什么做这个

我在准备米哈游的 Agent 工程师面试。JD 里三件事要答：
前沿 AI Agent 怎么落进游戏研发管线、Agent 怎么做成研发工具链、怎么和引擎
策划程序一起重构工业化流程。

行业里游戏自动化测试有两条路：

- **RL Agent 直接玩游戏**——NetEase Wuji（ASE 2019）、Tencent Juewu、EA SEED
  走的这条。训起来贵，对状态空间挑剔，不好解释。
- **LLM Agent 读文档生成测试**——TITAN（arXiv 2025）这类方向。工程量可控，
  推理可解释，和真实策划文档直接对接。

GameGuard 走第二条，做成一个**完整闭环**：从 markdown 策划文档，到 Jira bug
单，全程跑通。双沙箱覆盖了米哈游类游戏 QA 实际工作量的 ~75%——技能数值占
20%，任务 + 3D + 寻路 + 存档占 55%，剩下的视觉 / 动作 / 物理精度这个 demo 不
碰。

---

## 长什么样

![GameGuard 整体架构](docs/architecture.drawio.png)

从上到下看：

1. **CLI**——五个子命令：`run` / `generate` / `regress` / `triage` / `info`
2. **Orchestrator**——编排各个 Agent，plan-and-execute 模式，留了 `review_hook` 给 Critic 接
3. **五个 Agent**——DesignDoc、TestGen、Exploratory、Triage、Critic
4. **Shared Tool Layer**——Pydantic schema 自动编译成 OpenAI function-calling schema，
   所有 Agent 共享一套工具协议
5. **Domain 层**——纯数据模型：技能、任务、3D 实体、对话、不变式、测试用例，
   不含 IO 和业务逻辑
6. **GameAdapter (ABC)**——沙箱的抽象契约，只有 `reset / step / trace / snapshot / restore`
7. **两个沙箱 + Unity mock gRPC 通路**——PySim（技能）、QuestSim（任务+3D）、
   UnityAdapter 走真 gRPC 连 mock server（D19 完成，E2E 验证 gRPC 版和
   直跑版结果一致）；接真 Unity PlayMode 只需换 C# server 实现
8. **Reports**——Jira-compat BugReport / Markdown / HTML 差分报告
9. **LLM Stack**（右侧栏）——LiteLLM 封装 + DeepSeek / GLM / OpenAI 多家通用 + 磁盘缓存 + JSONL trace

图的源文件是 [`docs/architecture.drawio`](docs/architecture.drawio)，draw.io 能
直接打开编辑（PNG 也嵌入了 XML，拖回 draw.io 同样能改）。

---

## 能跑什么

### 场景一：测技能系统

假设你是策划或程序员，刚写完一版技能设计文档（`docs/example_skill_v1.md`），
想验证它是否和 v1 沙箱实现一致；等 v2 发布后，还要回归 v1 → v2 有没有引入
regression。

```bash
# 让 Agent 读策划文档，自己生成测试用例
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml
# 可以加 --critic 让 CriticAgent 对每条用例做 accept / patch / drop 审查

# v1 vs v2 差分回归，自动 triage，输出 HTML
gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html
```

跑完应该看到 5 条 NEW failure，分别对应 v2 植入的 5 个 bug：

| Bug ID | 真实世界分类 | v2 怎么坏的 |
|---|---|---|
| BUG-001 | 状态污染 | 切换技能时把所有冷却清零 |
| BUG-002 | 数值溢出 | 同 buff refresh 时 magnitude 累加（应替换） |
| BUG-003 | 状态机泄漏 | 施法被打断没退 mp，meta.mp_refunded 标记还撒谎 |
| BUG-004 | 浮点精度 | DoT 用了浮点累加路径 |
| BUG-005 | 确定性破坏 | 暴击 RNG 用全局 random，没走 sandbox seed |

### 场景二：测任务 + 3D 场景

这是 D12-D18 加的 QuestSim 沙箱。示例场景是"初识港口"——一个分支任务，
玩家可以选船长或商人两条线，在仓库汇合，最后推箱子到压力板。

![初识港口分支任务 DAG](docs/harbor_quest_dag.drawio.png)

v2 植入了 5 个典型 bug：AABB 边界判断错（Q-BUG-001）、商人分支漏 set alliance
flag 导致分支死锁（Q-BUG-002）、NPC 重置只重置位置没重置状态（Q-BUG-003）、
存档用 JSON 丢了 Enum / Vec3 字段（Q-BUG-004）、NavGrid 误标 blocked 形成
孤岛（Q-BUG-005）。

```bash
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml \
                  --baseline questsim:v1-harbor --candidate questsim:v2-harbor \
                  --html artifacts/reports/regress_quest.html
```

HTML 报告里会有 5 条 NEW failure，每条附 BugReport，可以点到 trace 详情。

### 为什么这 10 个 bug 都逃不掉

![Bug 和 Invariant 的对应矩阵](docs/bug_invariant_matrix.drawio.png)

每个 bug 都至少有一条 invariant 能抓到它。图里左边是 10 个植入 bug，右边是
14 条检测不变式，中间的连线说明抓与被抓的对应关系。差分测试的保证是：
v1 全过 + v2 恰好 10 条 NEW failure。

Bug 分类参考了 NetEase Wuji 在 ASE 2019 对 1349 个商业游戏 bug 的实证分析，
不是我瞎编的分类。

---

## 核心设计

### 为什么拆五个 Agent

每个 Agent 只做一件事：

| Agent | 输入 | 输出 |
|---|---|---|
| DesignDoc | markdown 策划文档 | InvariantBundle（一批不变式） |
| TestGen | 不变式 + 技能 / 任务 / 角色信息 | TestPlan（一批 TestCase） |
| Exploratory | 同上 | TestPlan（但策略是"尝试让事情坏"） |
| Critic | TestPlan | TestPlan（对每条做 accept / patch / drop） |
| Triage | 失败用例 | Jira 格式的 BugReport |

拆开是因为它们的思考方式完全不同：DesignDoc 像个认真读文档的实习生，TestGen
要有游戏直觉（知道"先 cast Focus 再打断"这种动作序列怎么编），Exploratory 要
有对抗心态（尝试奇怪组合），Triage 要像经验老道的 bug 管理员（合并同根失败、
写好复现步骤）。塞一个 Agent 里 prompt 会变得又长又撕裂。

实现上它们共享一个 AgentLoop + 一套 Tool Registry——换皮不换骨。

### 为什么 plan-and-execute，不是全 ReAct

![Plan-and-Execute 数据流](docs/agent_pipeline.drawio.png)

整个管线切成两段：

- **Plan 阶段**（绿）：LLM 参与。产出 Invariant、TestPlan 都落成 YAML。
  YAML 进 git，能像代码一样 review；下次想复跑，直接读 YAML 就行，不需要
  再调 LLM。
- **Execute 阶段**（橙）：纯确定性。seed + plan 两个输入完全决定 trace。
  CI 可以跑，测试可以缓存，bug 可以一键复现。

好处有三个：昂贵 + 随机的部分（LLM）尽量短；便宜 + 确定的部分（Runner）随便跑；
两段之间靠 YAML 黏合，人和机器都看得懂。这和 LangGraph、AutoGen、Anthropic
orchestrator-worker 几家的做法一致。

### 为什么两个沙箱而不是一个

PySim 和 QuestSim 的 domain model 完全不一样：前者是 Character + Skill + Buff，
后者是 Entity + Quest + NavGrid + DialogueGraph。Action 类型也不同。硬塞一个
沙箱里，tick 循环会变得像意面，基类会长出一堆 hook 只给一个子类用。

所以做法是：抽一个很薄的 `SandboxBase`（~180 行，管 reset / snapshot / rng /
_emit 这些共享底盘），tick 循环和 step 分发各自写。重构上叫 strangler fig
pattern——先建平行实现，不碰旧代码，等稳定了再看要不要合。

PySim 和 QuestSim 对上层（Runner、TriageAgent）完全透明——都是 `GameAdapter`。

### 为什么测试用例用 YAML 做

有两个好处：

1. LLM 直接产 YAML，不用写 Python 代码。schema 由 Pydantic 定义，出错 LLM
   看得到 validation error 能自己修。
2. YAML 进 git 之后，能像代码一样 review——这是真实 QA 团队用 TestRail / Xray
   的做法。我以前看过网易内部导出的格式，基本就是 YAML + 少量元数据。

Runner 把 YAML 还原成 Pydantic 对象后按 seed 跑。trace + snapshot 都落盘，
每条失败都有复现路径。

### 为什么强调确定性

三件事必须死磕：

- 同 seed 两次跑，事件序列必须完全一致（`replay_deterministic` 不变式就是
  抓这个的，BUG-005 逃不掉）。
- LLM 在 deterministic 模式下必须命中磁盘缓存，miss 就报 `CacheMissInStrictMode`。
  CI 不会意外花钱。
- 每个 Agent 有 token 和 USD 双重预算，超了直接抛 `BudgetExceeded`。

这是抄游戏 lockstep 同步 + LLM eval 社区的通用做法。好处是面试讲 demo 时
能说"你随便看一条 bug，我敲一行命令就能精确复现"。

---

## 快速开始

前置要求：Python 3.11、conda 可用。**强烈建议**跑在独立 conda 环境里，不要
装到 base。

```bash
# 1. 建环境
conda env create -f environment.yml
conda activate gameguard

# 2. 装项目（editable 模式）
pip install -e ".[dev]"
# 想跑物理推箱场景再加 pybullet：
pip install -e ".[dev,physics]"
# 想跑 Unity mock gRPC 通路（Stage 6）再加：
pip install -e ".[dev,unity]"

# 3. 配 API key
cp .env.example .env
# 编辑 .env，至少填一个：
#   GAMEGUARD_MODEL=deepseek/deepseek-chat
#   DEEPSEEK_API_KEY=sk-xxxxx
# 也支持 zai/glm-5.1（推理型，DesignDoc 召回最佳）、
# openai/gpt-4.1（TestGen 效果最佳）、anthropic/claude-sonnet-4-6 等

# 4. 跑测试（这步不花 API 钱）
pytest -q
# 应该看到 162 passed + 3 skipped
# 含 5 条真 gRPC E2E（Stage 6 Unity mock server 通路验证）

# 5. 看能力清单
gameguard info

# 6. 跑第一个 demo（技能系统回归）
gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html

# 7. 跑第二个 demo（任务系统回归）
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml \
                  --baseline questsim:v1-harbor --candidate questsim:v2-harbor \
                  --html artifacts/reports/regress_quest.html
```

退出码 0=全过、1=有失败、2=有错误，兼容 CI。

### 支持的沙箱字符串（`gameguard info` 里有）

| sandbox | 说明 |
|---|---|
| `pysim:v1` / `pysim:v2` | 技能系统，v1 是黄金参考，v2 植了 5 个 bug |
| `questsim:v1-harbor` / `questsim:v2-harbor` | 任务 + 3D + 寻路 + 对话 + 存档，初识港口场景 |
| `questsim:v1+pybullet` | 同上 + pybullet 物理后端（需 `[physics]` extras） |
| `unity:mock` | 预录 trace mock，不需要 server 进程 |
| `unity:headless` | 真 gRPC 连 mock server（`make unity-server`），默认后端 pysim:v1 |
| `unity:headless+pysim:v2` / `unity:headless+questsim` | 显式指定 mock server 内部 backend |

### 成本控制

- 跑一次 `gameguard generate` 在 DeepSeek 上大约 200k token（~¥0.5），命中缓存
  后零成本
- `GAMEGUARD_DETERMINISTIC=1` 强制 temperature=0 + 必须命中缓存
- `GAMEGUARD_USD_BUDGET=0.50` 单次预算上限，超了立即抛异常

---

## 文件结构

```
GameGuard/
├── README.md                           # 当前版（旧版在 docs/archive/）
├── environment.yml / pyproject.toml    # conda 环境 + pip 依赖
├── Makefile                            # make demo / make test / make regress
├── .env.example                        # API key 模板
│
├── gameguard/
│   ├── cli.py                          # typer CLI 入口
│   │
│   ├── agents/                         # 五个 Agent
│   │   ├── base.py                     # AgentLoop 主循环（~200 行手写）
│   │   ├── orchestrator.py             # plan-and-execute 编排
│   │   ├── design_doc.py
│   │   ├── test_gen.py
│   │   ├── exploratory.py
│   │   ├── triage.py
│   │   └── critic.py
│   │
│   ├── tools/                          # 五组工具
│   │   ├── schemas.py                  # Pydantic → OpenAI function-calling schema
│   │   ├── doc_tools.py                # 文档沙箱化浏览
│   │   ├── testgen_tools.py            # emit_testcase / list_*
│   │   ├── triage_tools.py             # 聚类 + emit_bug_report
│   │   └── critic_tools.py             # inspect / patch / drop
│   │
│   ├── domain/                         # 纯数据，零 IO
│   │   ├── skill.py / character.py / buff.py       # 技能系统
│   │   ├── geom.py / entity.py                     # 3D 原语（D12 加）
│   │   ├── quest.py / scene.py / dialogue.py       # 任务 / 场景 / 对话（D13-15）
│   │   ├── action.py / event.py                    # Action union / EventLog
│   │   └── invariant.py                            # 19 种不变式 + evaluator 注册表
│   │
│   ├── sandbox/
│   │   ├── adapter.py                  # GameAdapter ABC
│   │   ├── base.py                     # SandboxBase 共享底盘（D12 抽离）
│   │   ├── pysim/                      # 技能沙箱
│   │   │   ├── core.py / factory.py    # 20Hz tick + 确定性 RNG
│   │   │   ├── v1/skills.py            # 黄金实现
│   │   │   └── v2/skills.py            # 植了 5 个 bug
│   │   ├── questsim/                   # 任务 / 3D 沙箱（D12-D18）
│   │   │   ├── core.py / factory.py
│   │   │   ├── nav.py                  # A* + Tarjan SCC
│   │   │   ├── quest_runtime.py / dialogue_runtime.py / save_codec.py
│   │   │   ├── physics/                # dummy 默认 / pybullet 可选
│   │   │   ├── scenes/harbor.py        # 初识港口分支任务
│   │   │   ├── v1/handlers.py          # 黄金实现
│   │   │   └── v2/handlers.py          # 植了 5 个 Q-BUG
│   │   └── unity/                      # Unity mock gRPC 通路（D11 + D19）
│   │       ├── adapter.py              # 真 gRPC client + mock trace 回放
│   │       ├── mock_server.py          # grpc.server，按 spec 路由 PySim/QuestSim
│   │       ├── translate.py            # proto ↔ domain 双向翻译
│   │       ├── proto/gameguard_v1.proto
│   │       ├── generated/              # pb2 + pb2_grpc（make proto 重生）
│   │       └── client/                 # C# Unity 侧骨架（MagicOnion + UniTask）
│   │
│   ├── testcase/
│   │   ├── model.py                    # TestCase / TestPlan / TestSuiteResult
│   │   ├── loader.py                   # YAML ↔ Pydantic
│   │   └── runner.py                   # 跑批 + StateView + snapshot 落盘
│   │
│   ├── reports/
│   │   ├── schema.py                   # SuiteReport + Jira-compat BugReport
│   │   ├── markdown.py                 # 套件级 / bug 级 md
│   │   ├── html.py / templates/*.j2    # Jinja2 HTML 报告
│   │   └── regress.py                  # NEW / FIXED / FLAKY 差分
│   │
│   └── llm/
│       ├── client.py                   # LiteLLM + cache + budget + trace
│       ├── cache.py                    # content-addressed 磁盘缓存
│       └── trace.py                    # JSONL trace
│
├── docs/
│   ├── architecture.drawio(.png)       # 主架构图，可编辑
│   ├── agent_pipeline.drawio(.png)     # Plan-and-execute 数据流图
│   ├── harbor_quest_dag.drawio(.png)   # 初识港口 DAG
│   ├── bug_invariant_matrix.drawio(.png)  # bug ↔ invariant 映射
│   ├── example_skill_v1.md             # 技能策划文档示例（飞书风格）
│   ├── unity_integration.md            # Unity 接入指南
│   └── dev-log.md                      # 每日 standup（D1-D18）
│
├── testcases/
│   ├── skill_system/
│   │   ├── handwritten.yaml            # 10 条手写回归用例
│   │   └── agent_generated.yaml        # Agent 生成版
│   └── quest_system/
│       └── harbor_handwritten.yaml     # 初识港口 8 条用例
│
├── tests/                              # 165 tests（162 pass + 3 skip）
│   ├── test_pysim_v1.py / test_pysim_v2_bugs.py
│   ├── test_questsim_d12.py ~ d17.py   # 每天一组守护测试
│   ├── test_invariant_dot_replay.py    # D8 新不变式
│   ├── test_property_v1.py             # hypothesis property-based
│   ├── test_triage.py / test_critic.py / test_regress.py
│   ├── test_unity_adapter.py           # proto round-trip + mock 回放
│   └── test_unity_e2e.py               # 5 条真 gRPC E2E（D19）
│
└── artifacts/                          # 运行产物（gitignore 过滤内容，保留目录）
    ├── traces/*.jsonl                  # sandbox + LLM 双层 trace
    ├── snapshots/*.bin
    ├── suite.json                      # TestSuiteResult 落盘
    └── reports/*.{md,html}
```

---

## 开发历程

19 天 D1-D19，按游戏工作室双周迭代节奏走。完整流水账在 [`docs/dev-log.md`](docs/dev-log.md)。

| 阶段 | 天数 | 产物 |
|---|---|---|
| Week 1：骨架到闭环 | D1-D7 | 域模型 → PySim v1/v2 → Invariant DSL → Runner → 手写 Agent Loop → DesignDoc + TestGen → Triage 两阶段聚类 |
| Week 2：功能完整 | D8-D11 | property-based + exploratory 策略、I-09/I-10 新不变式、`regress` 子命令 + HTML 报告、Critic Agent、Unity proto 骨架 |
| Week 3：QuestSim | D12-D18 | SandboxBase 抽离 → NavGrid + A* → Quest 运行时 → 对话 + 存档 → 物理 backend → 10 条新不变式 → v2 植 5 个 Q-BUG |
| 面试冲刺 | D19 | Stage 1-5 Agent eval harness + prompt 迭代记录 + LLM 对比 + CI + DEMO.md + **Stage 6 Unity mock gRPC 通路（E2E 验证 gRPC 版与直跑字节一致）** + 下线 Gemini + 加 GPT-4.1 对比 |

测试数从 D6 的 32 条一路涨到 D19 的 162 条（含 5 条真 gRPC E2E）。整体 ~17k 行
Python + 439 行 C# Unity 骨架，5 Agent + 两个 Python 沙箱 + 一条真 gRPC Unity
通路全部齐活。

---

## 行业参考

这些我都真看过（或至少读了 abstract），不是为了 readme 吹水而堆的：

- **NetEase Wuji (ASE 2019)** —— 1349 个商业游戏 bug 的实证分类，我植入 bug
  的分类参考了这篇
- **TITAN (arXiv 2025)** —— LLM Agent 做 MMORPG QA，部署到 8 条真实生产管线
- **EA SEED** —— DRL 辅助测试的 AAA 工业实践
- **Tencent Juewu / 绝悟** —— AI 做英雄平衡回归
- **Unity Test Framework** + `-batchmode -nographics` —— Unity 官方 headless CI
- **Regression Games (regression.gg)** —— LLM 驱动 UI 测试的商业尝试
- **Jepsen / Hypothesis / QuickCheck** —— property-based testing 学术基础
- **AutoGen / LangGraph** —— multi-agent 编排框架的先例
- **Anthropic orchestrator-worker** —— 本项目 Agent 拓扑的主要参考

---

## License

面试 / portfolio 用途。暂未授权第三方分发。
