# GameGuard 10 分钟演示脚本

这份文件是用来**面试演示用的照敲脚本**。每一步包含：
- 要敲的命令
- 命令的预期输出（关键片段）
- 这一步在讲什么

直接从上往下敲就能走完全流程。面试时可以屏幕共享边跑边讲，也可以对方自己
按这份文档跑。

---

## 0. 前置检查（30 秒）

```bash
conda activate gameguard
gameguard info
```

**预期输出**：一张彩色表格，列出所有能力模块和状态：

```
                               GameGuard 能力清单
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 模块                   ┃ 当前能力                                            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Sandbox.pysim:v1       │ ✓ 确定性 Python 模拟 + 4 技能 + 3 buff + 暴击       │
│ Sandbox.pysim:v2       │ ✓ 植入 5 类 bug（cooldown/buff/state/DoT/RNG）      │
│ Sandbox.questsim:v1    │ ✓ Quest/3D/寻路/对话/物理 骨架                      │
│ Sandbox.questsim:v2    │ ✓ 植入 5 类 Quest bug                               │
│ Sandbox.unity:mock     │ ✓ 预录 trace mock；接入测试免 server                │
│ Sandbox.unity:headless │ ✓ 真 gRPC 通路 ↔ mock server (D19)                  │
│ Agents.DesignDoc       │ ✓ 18 invariants from real designer doc              │
│ Agents.TestGen         │ ✓ discovery + prefetch 双模式                       │
│ Agents.Triage          │ ✓ 两阶段聚类 + Jira-compatible BugReport            │
│ Agents.Critic          │ ✓ 静态校验 + LLM patch/drop 决策                    │
└────────────────────────┴─────────────────────────────────────────────────────┘
```

**讲什么**：一句话介绍项目边界——五个 Agent、两个沙箱、Unity 真 gRPC 通路。

---

## 1. 看架构（1 分钟）

打开 [`docs/architecture.drawio.png`](docs/architecture.drawio.png)。

**讲什么**（从上到下）：

> CLI 驱动 Orchestrator，Orchestrator 编排五个 Agent。各 Agent 只做一件事：
> DesignDoc 读文档抽不变式，TestGen 把不变式编成测试用例，Executor 跑沙箱，
> Triage 聚类失败出 Jira 单，Critic 做质量 review。
>
> 底下是 Domain 层（纯数据模型）和 GameAdapter（沙箱契约）。两个沙箱实现
> PySim 和 QuestSim 平行，Unity 走真 gRPC 接 mock server（D19），换成 C#
> Unity server 时 Python 侧零改动。
>
> 所有 Agent 共享同一套 Tool Registry——Pydantic schema 自动编译成 OpenAI
> function-calling schema，每个工具都 schema 校验 + 结构化错误反馈。

---

## 2. Agent 读策划文档生成测试（3 分钟）

### 命令

```bash
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml
```

### 预期输出（关键片段）

```
[plan] DesignDocAgent 读文档中...
  → tool_call: list_docs() = ["docs/example_skill_v1.md"]
  → tool_call: list_doc_sections(doc=0)
  → tool_call: read_doc_section(idx=4)   # 技能数据表
  → tool_call: read_doc_section(idx=7)   # 不变式章节
  → tool_call: emit_invariant(kind="hp_nonneg", actor="dummy")
  → tool_call: emit_invariant(kind="cooldown_at_least_after_cast", ...)
  ...
  → tool_call: finalize()
  DesignDocAgent 产出 12 条 invariants ✓

[plan] TestGenAgent 生成测试...
  → tool_call: list_invariants()
  → tool_call: list_skills()
  → tool_call: list_characters()
  → tool_call: emit_testcase(id="smoke-fireball", actions=[...])
  ...
  → tool_call: finalize()
  TestGenAgent 产出 7 条 testcases ✓

落盘 → testcases/skill_system/agent_generated.yaml
```

### 这一步在讲什么

> 这就是 plan 阶段。Agent 有两个：DesignDoc 负责"读懂文档"，TestGen 负责
> "写出能跑的测试"。你看 trace 里每一步都是 tool_call——LLM 不直接吐 YAML，
> 而是调 `emit_invariant` 工具把结果当参数交过来，所有 schema 都 Pydantic
> 校验过，schema 错了 LLM 能收到 validation error 自己修。
>
> 全程是确定性的：LLMClient 的 cache 命中后二次运行零成本；`--deterministic`
> 模式下 cache miss 直接抛错，保证 CI 可复现。
>
> 产物是一份 YAML 进 git，下次运行直接读 YAML 跑沙箱——plan 阶段只调一次。

---

## 3. 跑 v1 → v2 差分回归（3 分钟）

### 命令

```bash
gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html
```

### 预期输出

```
[regress] 跑 baseline pysim:v1...
  12/12 passed ✓

[regress] 跑 candidate pysim:v2...
  7/12 passed, 5/12 failed

[regress] 差分分析...
  NEW failures: 5
    - cooldown-isolation-fireball-then-frostbolt  (I-04 cooldown 不应被其他技能影响)
    - buff-chilled-refresh-magnitude-stable       (I-05 refresh buff magnitude 不应累加)
    - interrupt-refunds-mp                         (I-08 打断应退 mp)
    - dot-burn-total-damage-predictable           (I-09 DoT 总伤应与 tick 无关)
    - replay-determinism-fireball-frostbolt       (I-10 同 seed 必须产同 trace)
  FIXED: 0
  FLAKY: 0
  STABLE: 7

[regress] 自动 triage...
  TriageAgent 分成 5 个 BugReport cluster（每个 bug 独立簇）

[regress] HTML 报告 → artifacts/reports/regress.html
```

### 打开 HTML 报告

```bash
open artifacts/reports/regress.html    # macOS
# xdg-open artifacts/reports/regress.html    # Linux
```

页面里：
- 顶部：NEW 5 / FIXED 0 / STABLE 7 的总览
- 下面：5 条 NEW failure，每条展开能看到：
  - 失败的 invariant 的具体违反点（witness tick、actual vs expected）
  - 对应的 BugReport（bug_id、severity、component、repro_steps、suggested_owner）
  - trace 文件链接（点开看完整 EventLog）

### 这一步在讲什么

> 这是 execute 阶段，**全程无 LLM**，只有 Triage 那一步会再掉一次 LLM
> 做聚类。相同 seed 每次跑结果完全一致。
>
> 差分测试的意思：同一份 plan 在 v1 / v2 各跑一次，比对差异。不只看
> "哪些红了"，还要看"哪些本该红但没红"（FIXED）和"同沙箱两次不一致"
> （FLAKY，能抓到确定性破坏这种问题）。
>
> TriageAgent 聚类是两阶段：第一阶段用规则（按 invariant kind + actor +
> skill 分簇），第二阶段让 LLM judge 确认同簇里是否真的同根 bug。这样避免
> "一个 bug 产生 50 条重复 Jira 工单"的常见痛点。

---

## 4. 挑一个 bug 讲因果（2 分钟）

选 **BUG-003**（打断不退 mp）讲最直观。打开 trace 文件：

```bash
cat artifacts/traces/interrupt-refunds-mp.jsonl | python -m json.tool | head -40
```

### 关键 trace 片段

```json
{"tick": 0, "kind": "cast_start", "actor": "p1", "skill": "skill_focus",
 "meta": {"mp_before": 100, "mp_after": 80}}
{"tick": 10, "kind": "cast_interrupted", "actor": "p1", "skill": "skill_focus",
 "meta": {"mp_refunded": true}}      ← meta 说退了
{"tick": 10, "kind": "invariant_violation",
 "invariant_id": "I-08-focus",
 "actual": {"mp": 80},                 ← 但实际 mp 还是 80
 "expected": {"mp": 100}}              ← 应该被退到 100
```

### 讲什么

> 这个 bug 特别狡猾：事件日志里 `meta.mp_refunded = true` 说"退了"，
> 但实际 `mp` 字段还是 80。v2 代码里 `on_interrupt` 只清了 casting_skill
> 字段，忘了 `actor.mp += spec.mp_cost` 这一行，而 emit 的 meta 却写死
> `mp_refunded: True`——"日志撒谎"。
>
> 这是 QA 最头疼的场景：光看日志以为没问题，要看实际状态才发现。
> 我们的 oracle `interrupt_refunds_mp` 检查的是 **mp 数值**，不是
> meta 标记，所以能抓到。
>
> 面试 point：**oracle 的设计决定了能抓什么 bug**。如果 oracle 只 check
> meta，v2 就骗过去了。
>
> 想看是哪条代码改坏的？
> `gameguard/sandbox/pysim/v2/skills.py::V2SkillHandler.on_interrupt`
> 有注释圈出 BUG-003 的位置。

---

## 5. Unity mock gRPC 通路（可选，2 分钟）

Stage 6 · D19 加：`unity:headless` sandbox 从"proto 骨架 + NotImplementedError"
升级成真 gRPC client ↔ 本地 mock server，证明跨进程协议可跑通。mock server
内部复用 PySim/QuestSim 当 real backend，这样上层 Runner 零感知。

### 启动 server 与跑一条 plan

```bash
# 终端 A：起 mock server（监听 127.0.0.1:50099）
make unity-server

# 终端 B：用 unity:headless 跑一份 plan，结果应与直跑 pysim:v1 一致
GAMEGUARD_UNITY_ENDPOINT=127.0.0.1:50099 \
gameguard run --plan testcases/skill_system/handwritten.yaml \
              --sandbox unity:headless+pysim:v1
```

### E2E 自动验证

```bash
make test-unity    # 5 条 E2E，覆盖：
                   # - pysim:v1 gRPC vs 直跑 byte-level 一致
                   # - pysim:v2 info 透传
                   # - snapshot/restore 往返
                   # - questsim:v1 基础跑通
                   # - 未 reset 的错误码清晰
```

### 这步发生了什么

- `mock_server.py` 起 `grpc.Server`，4 workers + 锁串行化 backend 访问
- `UnityAdapter.from_endpoint()` 建 `grpc.insecure_channel`，所有 RPC 经
  `gameguard/sandbox/unity/translate.py` 把 Action / SandboxState / Event
  / Snapshot 在 Pydantic ↔ protobuf 之间双向翻译
- QuestSim 的 scene/quest/entities 走 `custom_fields` bytes JSON 透传，
  proto schema 对两种后端都稳定
- `client/` 下的 C# 骨架（MagicOnion + UniTask + PlayMode hook）给出
  真 Unity 侧落地的最小可编译路径，不跑但能看懂

---

## 6. 深挖路标（1 分钟）

面试官如果想深入问，这里是分层指路：

| 想看什么 | 去哪 |
|---|---|
| **Agent 效果的真实数字** | [`EVAL.md`](EVAL.md)（DesignDoc 召回 55%、TestGen 召回 20%、Triage 100%、Critic 80%） |
| **架构图 / 数据流** | [`docs/architecture.drawio.png`](docs/architecture.drawio.png) + [`docs/agent_pipeline.drawio.png`](docs/agent_pipeline.drawio.png) |
| **AgentLoop 主循环**（~200 行手写） | [`gameguard/agents/base.py`](gameguard/agents/base.py) |
| **Tool schema 自动生成** | [`gameguard/tools/schemas.py`](gameguard/tools/schemas.py) |
| **不变式 DSL + registry** | [`gameguard/domain/invariant.py`](gameguard/domain/invariant.py) |
| **v2 五个 bug 具体怎么植的** | [`gameguard/sandbox/pysim/v2/skills.py`](gameguard/sandbox/pysim/v2/skills.py) |
| **Unity gRPC 通路** | [`gameguard/sandbox/unity/mock_server.py`](gameguard/sandbox/unity/mock_server.py) · [`gameguard/sandbox/unity/translate.py`](gameguard/sandbox/unity/translate.py) · [`gameguard/sandbox/unity/client/`](gameguard/sandbox/unity/client/) · [`docs/stage6-unity-mock-plan.md`](docs/stage6-unity-mock-plan.md) |
| **QuestSim 场景举例** | [`gameguard/sandbox/questsim/scenes/harbor.py`](gameguard/sandbox/questsim/scenes/harbor.py) + [`docs/harbor_quest_dag.drawio.png`](docs/harbor_quest_dag.drawio.png) |
| **开发历程** | [`docs/dev-log.md`](docs/dev-log.md) 每天一条 D1-D18 |

---

## 常见面试提问参考答案

这些是跑 demo 过程中高频被问的问题：

### "为什么这么相信 Agent 抽的 invariant 是对的？"

> 我不"相信"——我建了 `evals/design_doc/` 对比 Agent 抽出来的和人工标注
> 的 golden，当前召回 55%、准确 100%。所以真实情况是 Agent 能抓到一半
> 左右的 required，漏抽主要集中在 `interrupt_*` 和 `replay_deterministic`
> 这些 meta-invariant 上——可以通过改 prompt 展开策略来补。

### "为什么 TestGen 召回这么低（20%）？"

> 两个原因：上游 DesignDoc 漏抽了相关 invariant（比如 I-10），下游 TestGen
> 没 invariant 可依据；另一个是 TestGen 的动作序列算错时会产生 ERROR 用例
> 本身跑不起来。这就是 Critic 的价值——accuracy 80% 能 catch 一部分
> broken case 修好。

### "这个项目能接入真实 Unity 吗？"

> 已经分三层证明：(1) proto 协议就绪（7 个 RPC 完整覆盖 GameAdapter ABC）；
> (2) **真 gRPC 通路**——Python mock server + UnityAdapter client 经真 TCP
> 跑通，5 条 E2E 测试里 `pysim:v1` 的 gRPC 版和直跑版 byte-level 一致；
> (3) Unity C# 侧给了最小可编译骨架（MagicOnion + UniTask + PlayMode hook，
> `client/` 下三份 .cs）。真接通 Unity PlayMode 大约再 2-3 周
> （Scene 序列化 + event bus hook + Jenkins 拉 headless），Python 侧零改动。

### "为什么不用 LangChain / LangGraph？"

> AgentLoop ~200 行手写。好处是边界行为完全可控：步数超限怎么退出、工具
> 失败怎么塞错误让 LLM 修、trace 怎么记、`tool_choice=required` + `stop_when`
> 怎么配合——这些细节在 LangChain 里要翻几层 wrapper 才能调到，手写能直接
> debug。模型也不绑定——LiteLLM 封装支持 DeepSeek / 智谱 / Claude / OpenAI
> 一行 env 切换。

### "为什么只做了一个策划文档的示例？"

> 这是有意识的取舍。增加第二个文档会让 DesignDoc eval / TestGen eval 的
> 分母翻倍，但 Agent 架构验证层面的信息量边际递减。我选择把精力投到深度
> （eval harness、prompt 迭代记录、LLM 模型对比），而不是广度。

---

## 跑完了想复跑

所有命令都可以反复跑。LLM cache 会命中之前的响应，第二次起零成本。

清缓存重跑（真烧 API 钱）：

```bash
rm -rf .cache/llm/
gameguard generate --doc docs/example_skill_v1.md --out ...
# 单次完整流程约 200k token / ¥0.5（DeepSeek 定价）
```
