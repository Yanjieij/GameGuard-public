# GameGuard · Agent 效果评估

> 最近一次 rollup：2026-04-18
> 
> 这份文件是 `evals/` 目录下 4 份 `results.md` 的汇总。
> 复跑：`python -m evals.rollup`（依赖先跑各 Agent 的 eval 脚本）。

## 快速一览

下表是每个 Agent 评估的 mean 行（详情点对应章节）：

### DesignDocAgent

- 详细结果：[`evals/design_doc/results.md`](evals/design_doc/results.md)

| **mean** | **55.56%** (σ=0.00%) | **100.00%** (σ=0.00%) | — | 0 | $0.0000 | 0.0 |

### TestGenAgent

- 详细结果：[`evals/test_gen/results.md`](evals/test_gen/results.md)

| **Agent mean** | 7.0 | **57%** | — | **20%** | 115.1 |

### TriageAgent

- 详细结果：[`evals/triage/results.md`](evals/triage/results.md)

| **mean** | **100.00%** | **100.00%** | — | 74.7 |

### CriticAgent

- 详细结果：[`evals/critic/results.md`](evals/critic/results.md)

| **mean** | **80.00%** | **100.00%** | **66.67%** | — | — | — | 85.4 |

### LLM Provider 对比

- 详细结果：[`evals/compare_models/results.md`](evals/compare_models/results.md)
- （无 mean 行）

---

## 各 Agent 详细结果

下面是各 results.md 的原文拼接（标题层级降一级以便统一大纲）。

## DesignDocAgent

*来源：[`evals/design_doc/results.md`](evals/design_doc/results.md)*

- 文档：`docs/example_skill_v1.md`
- Golden required：18 条；optional：3 条
- Runs：1

#### 各次运行

| # | recall | precision | steps | tokens | USD | wall (s) |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 55.56% | 100.00% | 20 | 0 | $0.0000 | 0.0 |
| **mean** | **55.56%** (σ=0.00%) | **100.00%** (σ=0.00%) | — | 0 | $0.0000 | 0.0 |

### 被漏抽的 required invariant

| Invariant | 漏抽次数 (/N) |
|---|---:|
| `buff_stacks_within_limit  actor=dummy  buff=buff_burn` | 1/1 |
| `buff_stacks_within_limit  actor=p1  buff=buff_arcane_power` | 1/1 |
| `dot_total_damage_within_tolerance  actor=dummy  buff=buff_burn` | 1/1 |
| `interrupt_clears_casting  actor=p1` | 1/1 |
| `interrupt_refunds_mp  actor=p1  skill=skill_fireball` | 1/1 |
| `interrupt_refunds_mp  actor=p1  skill=skill_focus` | 1/1 |
| `interrupt_refunds_mp  actor=p1  skill=skill_frostbolt` | 1/1 |
| `replay_deterministic` | 1/1 |

### 结论

✗ 需优化——召回不足 75%

---

## TestGenAgent

*来源：[`evals/test_gen/results.md`](evals/test_gen/results.md)*

- 模式：discovery
- Runs：1

### Baseline（handwritten.yaml）

- 用例数：12
- v1 pass 率：100% (12/12)
- v2 抓到的 bugs：['BUG-001', 'BUG-002', 'BUG-003', 'BUG-004', 'BUG-005']
- v2 bug 召回：100% (5/5)

### Agent 生成 vs Baseline

| # | 用例数 | v1 pass% | v2 抓到 bugs | v2 召回 | wall (s) |
|---|---:|---:|---|---:|---:|
| baseline (handwritten) | 12 | 100% | ['BUG-001', 'BUG-002', 'BUG-003', 'BUG-004', 'BUG-005'] | 100% | — |
| Agent run 1 | 7 | 57% (4/7) | ['BUG-001'] | 20% | 115.1 |
| **Agent mean** | 7.0 | **57%** | — | **20%** | 115.1 |

### 每个 BUG 被 Agent 抓到的次数

| Bug | Agent 抓到次数 | baseline |
|---|---:|---|
| BUG-001 | 1/1 | ✓ |
| BUG-002 | 0/1 | ✓ |
| BUG-003 | 0/1 | ✓ |
| BUG-004 | 0/1 | ✓ |
| BUG-005 | 0/1 | ✓ |

### 结论

✗ Agent 明显不及 baseline：召回 20%

---

## TriageAgent

*来源：[`evals/triage/results.md`](evals/triage/results.md)*

- Fixture：handwritten.yaml 在 pysim:v2 上跑（真实 5-bug 失败）
- Ground truth bug 组：5

### Ground Truth

- **BUG-001**：`['cooldown-isolation-fireball-then-frostbolt']`
- **BUG-002**：`['buff-chilled-refresh-magnitude-stable']`
- **BUG-003**：`['interrupt-refunds-mp']`
- **BUG-004**：`['dot-burn-total-damage-predictable']`
- **BUG-005**：`['replay-determinism-fireball-frostbolt']`

### 各次运行

| # | cluster_recall | cluster_precision | agent clusters | wall (s) |
|---|---:|---:|---:|---:|
| 1 | 100.00% | 100.00% | 5 | 74.7 |
| **mean** | **100.00%** | **100.00%** | — | 74.7 |

### 结论

✓ Triage 聚类质量可用

---

## CriticAgent

*来源：[`evals/critic/results.md`](evals/critic/results.md)*

- Fixture：6 条 broken + 4 条 correct
- Runs：1

### Fixture 明细

| Case ID | 期望 |
|---|---|
| `broken-mp-exhaust` | broken→修 |
| `broken-skill-typo` | broken→修 |
| `broken-interrupt-idle` | broken→修 |
| `broken-timing-too-short` | broken→修 |
| `broken-mp-too-many-casts` | broken→修 |
| `broken-cd-blocked` | broken→修 |
| `correct-fireball-single` | correct→接受 |
| `correct-fireball-double-with-gap` | correct→接受 |
| `correct-fireball-interrupt` | correct→接受 |
| `correct-ignite-dot` | correct→接受 |

### 各次运行

| # | accuracy | precision | recall | tp | fp | fn | wall (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 80.00% | 100.00% | 66.67% | 4 | 0 | 2 | 85.4 |
| **mean** | **80.00%** | **100.00%** | **66.67%** | — | — | — | 85.4 |

### 结论

△ Critic 能识别大部分问题（accuracy 80%）

---

## LLM Provider 对比

*来源：[`evals/compare_models/results.md`](evals/compare_models/results.md)*

跑 DesignDoc + TestGen 两个 eval 在 5 个 provider 上，看谁能抓到多少 invariant
/ 用例质量 / 成本 / 协议兼容性。

**方法**：temperature=0，单次跑，看绝对数字和协议兼容性。同 provider 的 variance
在这里不重要（temperature=0 + 缓存命中后完全确定）；重要的是不同 provider 在
**同一任务、同一 prompt、同一工具集**下的对比。

### 参赛 provider 说明

| Provider | Model | 走谁 | disable_thinking | 备注 |
|---|---|---|---|---|
| DeepSeek-chat | `deepseek/deepseek-chat` | LiteLLM | — | 非推理型，当前 default |
| GLM-4.6 | `zai/glm-4.6` | LiteLLM | — | 非推理型，智谱当家 |
| GLM-5.1 | `zai/glm-5.1` | LiteLLM | ✓ | 推理型，disable_thinking 标配 |
| GPT-4.1 | `openai/gpt-4.1` | LiteLLM | — | OpenAI 2025 tool-use 优化款，function-calling 原产地 |

> Gemini 2.5 Flash / Pro 曾在参赛池，因协议适配问题下线；详见文末 **负面结果 · Gemini 调研**。

---

### DesignDoc 任务对比（从策划文档抽 invariant）

| Provider | recall | precision | steps | tokens | wall (s) | 备注 |
|---|---:|---:|---:|---:|---:|---|
| **GLM-5.1** | **83.33%** | **89.47%** | 5 | 21,288 | 96.3 | 🏆 最佳召回：一次 emit 17 条，17/18 required 命中 |
| GPT-4.1 | 55.56% | 100.00% | 11 | 104,038 | 30.1 | 中文文档召回差 GLM-5.1 近 28pp；precision 完美 |
| DeepSeek-chat | 55.56% | 100% | 20 | (cached) | (cached) | 稳：召回一般但 precision 完美 |
| GLM-4.6 | 0.00% | 0.00% | 20 | 69,365 | 32.1 | ✗ LiteLLM 协议错乱 |

### TestGen 任务对比（完整 plan 流水线）

| Provider | v2 bug recall | v1 pass% | 生成用例数 | steps | wall (s) | 备注 |
|---|---:|---:|---:|---:|---:|---|
| **GPT-4.1** | **80.00%** | **100%** | 5 | 3 | 10.6 | 🏆 2× GLM-5.1 的 bug 召回，v1 pass +12.5pp，10 秒跑完 |
| GLM-5.1 | 40% | 87.5% | 8 | — | 285.5 | 慢但也能跑 |
| DeepSeek-chat | 20% | 57% | 7 | — | (cached) | baseline |
| Handwritten baseline | 100% | 100% | 12 | — | 0 | 人类 ground truth |

GLM-4.6 上游 DesignDoc 已 0%，不值得跑 TestGen。

---

### 一句话结论

> **两阶段用两家**：DesignDoc 用 **GLM-5.1**（83% recall > GPT-4.1 的 55.6%，
> 中文策划文档语境差距显著），TestGen 用 **GPT-4.1**（80% v2 bug 召回、
> 100% v1 pass、10s 跑完——碾压 GLM-5.1 在这一环的 40% / 87.5% / 285s）。
>
> **如果只能选一家**：GPT-4.1 综合最强——DesignDoc 55.6% 虽非最好但 precision
> 100%，下游 TestGen 能补回；TestGen 的 80% v2 bug 召回是 GameGuard 最关键的
> 业务指标（"能不能抓住回归 bug"），比 DesignDoc 的"抽全不变式" 更接近 QA
> 系统的真实产出。

---

### 踩过的坑（每一条都是面试素材）

#### 坑 1 · GLM-4.6 多轮 tool-calling 协议错乱

**现象**：GLM-4.6 在第 3 步以后返回非 JSON 的 tool_call arguments：

```
<tool_call>
<arg_key>doc_id</arg_key><arg_value>example_skill_v1</arg_value>
<arg_key>heading</arg_key><arg_value>4. 技能数据表</arg_value>
</tool_call>
```

这是 GLM 自家的 XML 协议，不是 OpenAI function-calling 标准 JSON。LiteLLM
1.83 没做归一化，Pydantic 校验全部拒绝。20 步里 15 步 schema error，从未成
功调到 emit_invariant。

**根因**：Z.AI 服务端在 tool_history 变长时会切换到自家协议，LiteLLM
没捕获这个切换。

**解决**：暂时没有。要么等 LiteLLM 修，要么用 Z.AI 原生 SDK 自写归一化。

#### 负面结果 · Gemini 调研（已下线）

Gemini 2.5 Flash / Pro 曾在参赛池，踩到两层协议适配坑后下线：

1. **LiteLLM 不翻译 `tool_choice="required"`**：OpenAI 语义应映射到 Gemini 的
   `functionCallingConfig.mode="ANY"`，LiteLLM 1.83 没做此翻译，LLM 读完文档就
   `no_tool_calls` 早退，recall 0%。
2. **绕开 LiteLLM 直调 google-genai SDK 后撞 infinite emit**：mode=ANY 下
   Gemini 一轮并发 28 个 `emit_invariant` 且不主动 finalize，step=167 时撞
   1M tokens/min 限流。prompt 层短期无解。

**结论**：问题在 Gemini 侧"ANY 模式不主动收敛"的行为，DeepSeek / GLM 在同
prompt 下都能正常 finalize。GLM-5.1 已经足够强（83% DesignDoc recall），修
Gemini 边际价值低于维护成本，相关代码（`gemini_native.py`、`[gemini]` 可选
依赖、REGISTRY 条目）于 D19 整体下线。

---

### 为什么两个任务有两个不同的赢家

DesignDoc 和 TestGen 对 LLM 的要求其实完全不同：

- **DesignDoc**：从长中文文档里抽结构化信息——考**阅读理解 + 中文语感 +
  一次性 emit 17 条的规划能力**。GLM-5.1 的推理型架构 + 中文语料占比高，
  正好吃透这个场景，5 步一次 emit 完事（GPT-4.1 要 11 步）。
- **TestGen**：给定 invariant 清单生成可执行 YAML 测试用例——考**tool-calling
  稳定性 + 紧凑工具规划 + 不生成 broken case 的静态正确性**。GPT-4.1 作为
  function-calling 协议原产地 + 2025 tool-use 优化款，在这个环节形态全对：
  3 步 / 10 秒 / 5 条用例 / 100% v1 pass / 80% v2 bug 召回。GLM-5.1 的推
  理型反而在这是负担——思考过多、生成 8 条用例但 v1 pass 掉到 87.5%。

**关键 insight**：**别用一家模型打整个 pipeline**。GameGuard 的 orchestrator
天然按 Agent 分阶段，每个阶段挑最合适的 provider：DesignDoc Agent 用 GLM-5.1、
TestGen Agent 用 GPT-4.1、Triage / Critic 的轻量场景用 DeepSeek 省钱。`.env`
里的 `GAMEGUARD_MODEL` / `GAMEGUARD_MODEL_TRIAGE` 双字段就是为这个留的接口。

**成本考量（估算）**：GPT-4.1 单次 DesignDoc 约 $0.6（¥4.3），GLM-5.1 约 ¥0.4，
DeepSeek ¥0.02。production 里漏抓一个 regression bug 的代价远高于单次实验的
$0.5——对 TestGen 这一环 quality 绝对优先。

---

### 还没测的 provider（有 key 之后可补）

| Provider | 预期 | 为什么想跑 |
|---|---|---|
| Claude Sonnet 4.5+ | recall 80-90% | tool-calling 行业金标准；想对比 GPT-4.1 |
| GPT-5 | recall 85%+ | 2025 旗舰，看 TestGen 上能否再上 80% |
| OpenAI o4-mini | recall 72-82% | 推理型 + 便宜，对比 GLM-5.1 的推理型优势是否普遍 |

---

### 方法论 / 实验的局限

1. **N=1 不足以看 variance**——temperature=0 + 缓存的情况下单次跑就能
   代表平均，但 LLM 有日常波动（模型版本升级、后端路由变化）。生产要长期
   监控。
2. **goldens 偏严格**——18 条 required 是我手工标注的"理想抽取"，可能过度
   理想。DeepSeek 55% 看起来低，但人工审查漏的那几条确实是 prompt 要重点
   强调才能抽到的（比如 `replay_deterministic` 这种 meta-invariant）。
3. **评估指标单维度**——我们只看 recall / precision / v2 bug 召回。实际
   还应看"生成用例的可读性"、"trace 能否讲故事"这类定性维度。

这些限制是**项目保持诚实的一部分**——面试讲"这是我当前能看到的数字 + 已
知的边界"比吹"100% 召回"有说服力得多。

---
