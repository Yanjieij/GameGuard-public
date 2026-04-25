# Prompt Engineering 迭代记录

GameGuard 有 5 个 LLM Agent，每个 Agent 的 system prompt 都经历了几版调整
才到现在的样子。这份文档把"为什么这么写"翻出来给外人看——面试官、未来的
自己、或接手的人。

内容不是天马行空的反思，而是有据可查：git log + `docs/dev-log.md` +
`evals/*/results.md` 的真实数据。

---

## 0. 三条贯穿全项目的原则

在讲具体 Agent 之前，先说三个大原则——每一条都从失败里学来的。

### 0.1 Tool-as-side-channel 优于 structured output

让 LLM 吐结构化数据有两种做法：

| 做法 | 代价 | 好处 |
|---|---|---|
| `response_format: json_schema`（或 structured output） | 省一次调用，但跨 provider 支持参差，校验错误信息不清晰 | 便宜 |
| 定义一个 `emit_xxx` 工具，LLM 把数据作为工具参数传入 | 多一轮 tool_call | 走同一套 tool-calling 协议、Pydantic 强校验、trace 能看到每条数据是什么时候生成的 |

GameGuard 全走第二条。DesignDocAgent 用 `emit_invariant`，TestGenAgent 用
`emit_testcase`，Triage 用 `emit_bug_report`——所有 Agent 复用一套
AgentLoop + ToolRegistry，一致性最高。

**代价是多一轮 LLM 调用**，但这一轮在 DeepSeek 上也就 2-3 秒，没有明显
拖慢 wall-clock。

### 0.2 推理型模型的 "静默 token 黑洞" 要系统性治理

2025-2026 年这是整个 Agent 圈最难处理的问题之一。

**症状**：GLM-4.7、Claude thinking、OpenAI o1 这类推理型模型，如果 prompt
不到位或 tool_history 太长，它们会把整个 `max_tokens` 全烧在
`reasoning_content` 上，对外 `content=""` 且 `tool_calls=[]`——**静默失败**。

**我们踩的坑**（D5 期间）：
- GLM-4.7 单轮 chat 正常，多轮带 tool_history 时 100% 卡死
- `tool_choice="required"` 能治单轮，多轮仍炸
- `extra_body={"thinking":{"type":"disabled"}}` **实测生效** 但 Z.AI 服务端
  在多轮 tool history 场景下忽略此参数（A/B 实测：单轮时长从 15.6s → 1s、
  token 从 130k → 1k；多轮无变化）

**当前解法**（两件套同时用）：
1. `tool_choice="required"` — 强制每轮必须调工具，堵死"LLM 只想不做"
2. `disable_thinking=True` + `stop_when=lambda r: r.tool_name == "finalize"`
   — 给推理型模型一个明确的退出点，避免 tool_choice=required 把 finalize
   之后的 LLM 再逼一次

**关于模型选型**：DeepSeek-chat 非推理型，tool-calling 稳定性最好，是当前
默认。GLM-4.6（非推理型）备选。推理型模型（GLM-4.7、Claude thinking）留给
单轮任务用。

### 0.3 错误结构化回传让 LLM 自修复

这是 ReAct 论文（Yao 2022）的核心模式。`ToolRegistry.dispatch` 永不 raise；
任何异常都打包成 `ToolInvocationResult(ok=False, content="ERROR: ...")`
喂回 LLM 的下一轮 prompt。

效果：LLM 看到 "emit_invariant 失败：actor 字段必填"，会在下一轮自己补上 actor
字段重试——像人类被 IDE 提示后修 bug 一样。

没有这层，每次 schema 错误都会杀死整个 run。加了之后，TestGen 的成功率显著
提升（具体数字没精确测过，但 D5 调试期间能观察到 LLM 读到错误后连续几轮
修正）。

---

## 1. DesignDocAgent

**文件**：`gameguard/agents/design_doc.py`  
**当前 recall**：55.6%（[`evals/design_doc/results.md`](../evals/design_doc/results.md)）

### 职责

读一份策划 markdown → 抽一批机器可验证的 `Invariant` → emit 出 `InvariantBundle`。
只干这一件事。

### Prompt 演化

**v0（D4 初版，未提交）**
- 让 LLM 直接读 doc content → 输出 JSON（structured output）
- 失败：provider 之间 schema 支持参差，校验错误信息不清晰
- 放弃

**v1（D5 落地版）**
- 改 tool-as-side-channel：`list_docs` / `list_doc_sections` / `read_doc_section`
  / `emit_invariant` / `finalize`
- prompt 描述交互式浏览流程："先 list，再挑重点读，再 emit"
- 在 DeepSeek / GLM-4.6 上跑顺畅
- GLM-4.7 场景下经常陷入静默推理（tool_calls 空）

**v2（当前版，加了 workaround）**
- `tool_choice="required"` + `stop_when=finalize` + `disable_thinking`
- prompt 结构：身份 → 工作流 → tool 调用顺序 → 输出模板 → 反例

```python
# design_doc.py 现状（核心部分）
loop = AgentLoop(
    client=llm,
    tools=tools,
    agent_name="DesignDocAgent",
    system_prompt=SYSTEM_PROMPT,
    max_steps=20,
    tool_choice="required",                              # 治静默
    stop_when=lambda r: r.ok and r.tool_name == "finalize",
)
```

### Eval 发现的残余问题

跑 `evals/design_doc/` 跑出来 **55.6% recall**，漏的主要集中在：

| 漏抽的 invariant | 原因推测 |
|---|---|
| `interrupt_refunds_mp × skill_fireball/focus/frostbolt` | prompt 没强调"对每个可打断的技能都要展开一条" |
| `replay_deterministic` | 这是 meta-invariant，LLM 认为它不是数值断言类，抽取逻辑忽略了 |
| `dot_total_damage_within_tolerance × buff_burn` | 文档里 I-09 描述用的是口语（"与 tick_dt 无关"），不是显式数据 |

**下一步 prompt 改进**（暂未落地）：
1. prompt 里显式加"你必须对每个出现在技能表里的 skill 展开 cooldown / interrupt
   类的 invariant"
2. meta-invariant 列一份白名单（`replay_deterministic` / `save_load_round_trip`
   这种）直接告诉 LLM 需要抽
3. 实验看 Claude Sonnet 在同样 prompt 下召回多少（Stage 3 会做）

---

## 2. TestGenAgent

**文件**：`gameguard/agents/test_gen.py`  
**当前成绩**：生成 7 条 / v1 pass 57% / v2 bug 召回 20%（[`evals/test_gen/results.md`](../evals/test_gen/results.md)）  
**baseline**：handwritten.yaml 12 条 / v1 pass 100% / v2 召回 100%

这是项目里**最难写的 prompt**，也是目前效果最差的 Agent。

### 职责

给 Invariant → 生成动作序列，让那些 invariant 在沙箱里进入可观测窗口。
本质是"给机器人规划动作"。

### Prompt 演化

**v0（D4）**
- 朴素 prompt：给 invariants + skill book + characters，让 LLM 一次性 emit
  全部 test case
- 失败：GLM-4.7 静默推理；偶尔能生成，但 JSON 格式漂移

**v1 prefetch（D5 早期，GLM 时代）**
- 把 invariants + skills + characters **全文** 嵌入 user message
- 让 LLM 跳过 `list_*` 工具调用，直接进 emit 阶段
- 好处：少 3 步、省 token、对 GLM 更友好
- 坏处：`list_invariants` / `list_skills` / `list_characters` 三个工具注册了
  但 LLM 永远不调——**死工具**。trace 看不出"Agent 在浏览数据"的过程

**v2 discovery（D17 恢复）**
- 用户明确要求："请尽量保留完整的设计，不要过分简化"
- 让 LLM 真的按 prompt 流程去调 `list_invariants` → `list_skills` →
  `list_characters` → `emit_testcase × N` → `finalize`
- trace 里能看到 Agent"在浏览数据"，面试讲故事价值高
- 代价：多 3 步、token +4%、wall-clock +30%（DeepSeek 实测）

**v3 保留双模式（当前）**
- `run_test_gen_agent(prefetch_context: bool = False)`：默认 discovery，
  CI / GLM fallback 可切 prefetch
- 两种模式共享同一套代码和同一个 system prompt（prompt 里依然描述"先读三个
  list_*"，prefetch 下 LLM 发现 prompt 已含数据自然跳过）

### 当前 prompt 的关键段落（节选）

```
你是 GameGuard 的 TestGenAgent —— 一个资深游戏 QA 工程师，负责把不变式
编译成能在沙箱里真实跑通的测试用例。

## 工作流程

1. 调用 list_invariants 看所有不变式（按 kind 分组）
2. 调用 list_skills 查看技能数据（mp_cost / cast_time / cooldown）
3. 调用 list_characters 查看角色 mp / hp 初值
4. 对每一条不变式，设计一条或多条动作序列让它进入可观测窗口
5. 每条用 emit_testcase 提交；全部完成后调用 finalize

## 关键约束

- mp 必须够：cast 前先 check mp_cost
- cooldown 必须过：cast 后 wait 至少 cast_time + cooldown
- 不要引用 list_invariants 里没有的 id（invariant_ids 严格引用）
- assertions.when 选 END_OF_RUN 或 EVERY_TICK（看 invariant kind）
```

### Eval 发现的残余问题

**7 条用例 / 20% 召回** 远不及 baseline 的 12 / 100%。原因推测：

1. **上游 DesignDoc 漏抽 8 条 required invariant** → TestGen 没东西可测
2. **LLM 在编动作序列时经常算错 wait 时长**（eval 里 v1 pass 57% 说明有 3
   条用例本身有问题）
3. **LLM 不主动覆盖 edge case**（例如"两次 cast 之间刚好 cooldown 差 0.05s"这
   种边界）
4. **LLM 倾向于"一条用例测一个 invariant"**，不像人写测试会把多个 invariant
   塞进一个 setup

**下一步 prompt 改进**（部分已在考虑）：
1. 明确告诉 LLM："每个技能至少一条对应测试；每个 buff 至少一条 refresh 测试"
2. 在 prompt 里给 1-2 个 few-shot example（现在只有描述没有示例）
3. 让 Critic Agent 承担一部分"修 broken case"的责任——事实上 Critic eval 证明
   能 catch 大部分 broken case，recall 66.67%

---

## 3. ExploratoryAgent

**文件**：`gameguard/agents/exploratory.py`  
**当前成绩**：未单独 eval（D8 引入，功能完整但没大规模使用）

### 设计要点

这是 TestGenAgent 的"换皮不换骨"——两者共用同一组 tools（`list_invariants` /
`list_skills` / `list_characters` / `emit_testcase` / `finalize`），**只在
system prompt 上分道扬镳**。

| 维度 | TestGenAgent | ExploratoryAgent |
|---|---|---|
| 心态 | 契约驱动：对每条不变式写 focused 用例 | 对抗驱动：故意找边缘组合让 invariant 红 |
| 用例风格 | smoke / single-step / 流程化 | 多技能连发 / 打断 / 资源耗尽 |
| tags 标签 | `contract` | `exploratory` |

### 为什么不合并成一个 Agent 加开关？

试过。prompt 变得又长又割裂——契约思路和对抗思路的写法差距太大，塞一起
LLM 容易"风格漂移"（contract 的用例里夹杂对抗动作）。拆成两个 agent 文件
清爽很多，架构成本只多一个文件。

---

## 4. TriageAgent

**文件**：`gameguard/agents/triage.py`  
**当前成绩**：cluster_recall 100% / precision 100%（[`evals/triage/results.md`](../evals/triage/results.md)）

这是效果最好的 Agent。

### 两阶段设计

prompt 本身不做聚类决策，聚类分两步：

1. **规则阶段**（Python，确定性）：按 `(invariant_kind, invariant_id_prefix,
   actor, skill)` 把 N 条失败压成 M 个候选簇；ERROR 按 `error_message` 前
   80 字符 hash 分组。这一步**不调 LLM**。
2. **LLM judge 阶段**：对 size>1 的候选簇让 LLM 看失败证据（trace 尾部 20
   行 + assertion 详情），判断：
   - 簇内真的同根？还是规则误合？
   - 不同簇是否其实同根需要 merge？
   - 给每条最终 bug 起标题 / 写 repro_steps / 定 severity

### Prompt 关键约束

```
你是 GameGuard 的 TriageAgent —— 一个资深 QA 主管。

## 硬规则

- 不要直接读完整 trace：用 read_trace_tail(case_id, n=20) 按需拉
- 合并簇要谨慎：宁可多条 bug 也不要错合
- repro_steps 必须具体到动作（"cast fireball at t=0"，不要"施法"）
- severity 判断：hp_nonneg 红 = S1；replay_deterministic 红 = S0；
  cooldown / buff 漂移 = S2

## 输出流程

1. list_failures → 看所有失败 case 的摘要
2. 对每个候选簇调 inspect_cluster → 看详情
3. 有必要时 read_trace_tail 拉证据
4. emit_bug_report 或 merge_clusters
5. finalize
```

### 为什么 prompt 这么保守

最早版本（D7 写的）prompt 鼓励 LLM "主动挖掘跨簇关联"。结果：LLM 把 BUG-002
（buff refresh）和 BUG-004（DoT 浮点）合到一起，理由是"都和 buff 相关"。

现在的 prompt 里明确写"宁可多条 bug 也不要错合"——把默认行为从 aggressive
merge 改成 conservative split，eval 里 cluster_precision 就稳定在 100%。

---

## 5. CriticAgent

**文件**：`gameguard/agents/critic.py`  
**当前成绩**：accuracy 80% / precision 100% / recall 66.67%（[`evals/critic/results.md`](../evals/critic/results.md)）

### 职责边界

只做三件事：**accept / patch / drop**。严格禁止新增 case。

这个"边界纪律"是 prompt 里最重要的部分——前期试过让 Critic "fix 然后 improve"，
结果它开始凭空加 case，把 plan 膨胀一倍。现在 prompt 里把边界写得很死：

```
## 你的边界

- 不新增 case
- 不重新设计场景
- 只做三件事：accept / patch / drop

## 工作流程

1. list_cases 看所有待 review 的 case + 它们的静态校验问题数
2. 对每条 case：
   - 零 issue：直接调 accept_case（或不调，视为默认 accept）
   - 有 warn 但能跑通：accept
   - 有 error 但能 patch：用 inspect_case 看详情 → 用 patch_case
     提供修复后的 actions（典型修复：把 wait 时长拉长让 CD 过、删掉
     超 MP 的连续 cast、把 interrupt 从空 cast 状态去掉）
   - error 太多 / 无法救：调 drop_case 并写明 reason
3. 全部 case review 完后调用 finalize
```

### 静态校验在工具层 vs 决策在 LLM

关键设计：`inspect_case` 工具**先用 Python 做确定性静态校验**（MP 够不够、
CD 过没过、skill_id 存不存在），把结果作为 issue list 返回给 LLM。LLM 只
基于这个 issue list 决策 accept / patch / drop，**不用自己算数**。

这样的好处：
- MP / CD 这种数值计算让 Python 做，准确率 100%
- LLM 聚焦在"这个问题严重吗、该怎么修"——语义判断
- trace 里能清楚看到"工具给了什么 issue，LLM 做了什么决策"，debug 容易

### Eval 发现的残余问题

accuracy 80%（recall 66.67%）意味着 6 条 broken case 里漏了 2 条。排查 eval
trace 发现：
- 漏的两条是 `broken-timing-too-short`（wait 0.1s 但 cast_time 2s）和
  `broken-mp-too-many-casts`
- 这些 broken 点 Python 静态校验能查出来，但 Critic 在看 inspect_case 返回
  的 issue 时判断"影响不大，先 accept"

**下一步改进**：把 `broken_severity` 字段加到 issue 里（high=必须修，
medium=建议修，low=可选），prompt 里要求 high 必须 patch 或 drop。

---

## 跨 Agent 的共通设计模式（对面试有用的）

### 1. `emit + finalize` 两段式

所有 Agent 都有两个"关键工具"：
- `emit_xxx`：往 collector 里追加一条结构化数据
- `finalize`：告诉系统"我完事了"

`AgentLoop` 用 `stop_when=lambda r: r.tool_name == "finalize"` 捕获
finalize 调用立即退出。这治了 `tool_choice="required"` 的副作用——否则
LLM 调完 finalize 下一轮还被强制调一次工具，陷入死循环。

### 2. `tool_choice="required"` + `stop_when` 配对使用

不是"要开一起开"——是"`tool_choice=required` **必须配** `stop_when`"。
只开 required 不配 stop_when 会让 LLM finalize 后一直空调工具直到 max_steps。
`stop_when` 是 required 模式的必备配套，不是可选增强。

### 3. `disable_thinking` 要测

不同 provider 的 reasoning budget 行为不一样。Z.AI 的 `extra_body={
"thinking":{"type":"disabled"}}` 在单轮调用时能让 GLM-4.7 的 wall clock 从
15.6s 降到 1s、token 从 130k 降到 1k——A/B 实测的数字。但多轮 tool_history
场景下服务端忽略该参数。

**经验**：`disable_thinking` 是否生效不要问文档，跑一次看 `reasoning_content`
长度即可。

### 4. Prompt 里的"反例"比"正例"更有效

多个 Agent 的 prompt 都有类似 pattern：

```
## 反例（别这么做）

- 不要调用 emit_invariant 但 kind 字段拼错（会被 Pydantic 拒绝）
- 不要在 cast 期间立刻 emit_testcase 结束——至少给 cast_time + 1 tick wait
- 不要把 fireball 的 cooldown 写成 8.5（查数据表：是 8.0）
```

加了反例后 LLM 的 schema 错误率明显下降。

### 5. Cache-friendly prompt 设计

`LLMClient.chat` 的缓存 key 是 `hash(model, messages, tools, temperature,
tool_choice, ...)`——**任何 prompt 文本改动都会让缓存全失效**。

所以 prompt 调整有成本。最近几版都是增量式：新加的规则往尾部追加，不改已有
段落，这样大部分 request 还能命中缓存。dev 期间一次 full-regen 成本约 ¥1，
命中缓存后接近零。

---

## 还没解决的 prompt 问题

1. **DesignDoc 召回 55.6%**——漏 meta-invariant (I-07, I-09, I-10)。可能要在
   prompt 里显式列 invariant kind 白名单。
2. **TestGen 召回 20%**——生成少、质量差。可能要加 few-shot example + 硬约束
   "每个 skill 至少一条测试"。
3. **Critic recall 66.67%**——漏 broken 的 timing/MP 错误。要把 issue 分级传
   进来。
4. **Exploratory 没 eval**——需要 ground truth 难度大（不像 contract 那样有明
   确"应该覆盖什么"）。

这些都记在 [`EVAL.md`](../EVAL.md) 和 [`docs/dev-log.md`](dev-log.md) 里。
Stage 3 的 LLM 模型对比会给出"换模型能不能缓解"的答案。

---

## 附：几个值得读的代码位置

- [`gameguard/agents/base.py`](../gameguard/agents/base.py) — AgentLoop 主循环 + `tool_choice` + `stop_when`
- [`gameguard/llm/client.py`](../gameguard/llm/client.py) — `disable_thinking` + budget + cache
- [`gameguard/tools/schemas.py`](../gameguard/tools/schemas.py) — `ToolRegistry.dispatch` 错误结构化
- 各 Agent 的 `SYSTEM_PROMPT` 常量（每个 agent 文件开头附近）

---

*最后更新：2026-04-18*
