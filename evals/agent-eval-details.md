# GameGuard · Agent 效果评估

> 最近一次 rollup：2026-04-29
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

- 详细结果：[`MODEL_COMPARISON.md`](MODEL_COMPARISON.md)
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

*来源：[`MODEL_COMPARISON.md`](MODEL_COMPARISON.md)*

> 最近更新：2026-04-26

跑 DesignDoc + TestGen 两个 eval 在多个 provider 上，对比不变式抽取能力、用例质量、成本、协议兼容性。

**方法**：temperature=0（GPT-5.5 除外，它只支持 temperature=1），单次跑。

### 参赛模型一览

| Key | Provider | Model | 推理 | Temp | 备注 |
|---|---|---|---|---|---|
| `gpt-5.5` | GPT-5.5 | `openai/gpt-5.5` | reasoning_effort=none | 1.0 | 最新旗舰 |
| `gpt-5.4` | GPT-5.4 | `openai/gpt-5.4` | — | 0.0 | OpenAI flagship |
| `gpt-4.1` | GPT-4.1 | `openai/gpt-4.1` | — | 0.0 | 2025 tool-use 优化款 |
| `glm-5.1` | GLM-5.1 | `zai/glm-5.1` | **开启** | 0.0 | 推理型，开推理后 v1 pass 78%→100% |
| `glm-4.7` | GLM-4.7 | `zai/glm-4.7` | disable_thinking | 0.0 | 推理型（暂未重测） |
| `glm-4.6` | GLM-4.6 | `zai/glm-4.6` | — | 0.0 | 非推理型；LiteLLM 协议错乱已禁用 |
| `deepseek-v4-pro` | DeepSeek-V4-Pro | `deepseek-v4/deepseek-v4-pro` | disable_thinking* | 0.0 | V4 高质量档 |
| `deepseek-v4-flash` | DeepSeek-V4-Flash | `deepseek-v4/deepseek-v4-flash` | disable_thinking* | 0.0 | V4 快速档 |
| `deepseek` | DeepSeek-chat | `deepseek/deepseek-chat` | — | 0.0 | 当前 baseline |
| `mimo-v2.5-pro` | MiMo-V2.5-Pro | `mimo/mimo-v2.5-pro` | — | 0.0 | 小米 MiMo，Token Plan 接入 |

> \* DeepSeek V4 的 `disable_thinking` 不仅是关推理，更是 API 变体选择——去掉后会路由到 `deepseek-reasoner`，而 reasoner 不支持 `tool_choice`，直接报错。因此必须保持。
>
> Gemini 2.5 Flash / Pro 曾参赛，因 LiteLLM 协议适配问题于 D19 下线。

---

### DesignDoc 任务对比（策划文档 → 不变式）

| Provider | Recall | Precision | Steps | Tokens | Wall | 日期 |
|---|---:|---:|---:|---:|---:|---|
| **GLM-5.1** 🔥 | **100%** | 100% | 4 | 30,261 | 221s | 04-26 |
| **MiMo-V2.5-Pro** 🆕 | **100%** | 100% | 5 | 44,314 | 180s | 04-26 |
| **GPT-5.5** | **100%** | 100% | 12 | 296,734 | 86s | 04-26 |
| **GPT-5.4** | **100%** | 100% | 9 | 144,905 | 112s | 04-24 |
| **DS-V4-Pro** | **100%** | 100% | 9 | 62,989 | 247s | 04-24 |
| GLM-5.1 (关推理) | 100% | 100% | 5 | 56,595 | 158s | 04-24 |
| DS-V4-Flash | 83% | 100% | 10 | 63,261 | 54s | 04-24 |
| GPT-4.1 | 56% | 100% | 11 | 104,038 | 30s | 04-18 |
| DeepSeek-chat | 56% | 100% | 20 | (cached) | (cached) | 04-18 |
| GLM-4.6 | 0% | 0% | 20 | 69,365 | 32s | 04-18 |

### TestGen 任务对比（不变式 → 测试用例 → v2 bug 召回）

| Provider | v2 Bug Recall | v1 Pass% | 用例数 | Steps | Tokens | Wall | 日期 |
|---|---:|---:|---:|---:|---:|---:|---|
| **GLM-5.1** 🔥 | 80% | **100%** | 9 | 3 | 20,603 | 166s | 04-26 |
| **MiMo-V2.5-Pro** 🆕 | 80% | 100% | 8 | 3 | 22,094 | 93s | 04-26 |
| GPT-5.4 | 80% | 100% | 8 | 3 | 13,222 | 26s | 04-24 |
| DS-V4-Pro | 80% | 100% | 8 | 3 | 20,302 | 134s | 04-24 |
| GPT-4.1 | 80% | 100% | 5 | 3 | — | 11s | 04-18 |
| GPT-5.5 | 80% | 87.5% | 8 | 5 | 186,957 | 64s | 04-26 |
| GLM-5.1 (关推理) | 80% | 77.8% | 9 | 3 | 18,249 | 80s | 04-24 |
| DS-V4-Flash | 40% | 100% | 9 | 4 | 25,188 | 54s | 04-24 |
| DeepSeek-chat | 20% | 57% | 7 | — | — | (cached) | 04-18 |
| *人工 handwritten* | *100%* | *100%* | *12* | — | — | — | — |

---

### 推理开关的影响 (2026-04-26 实验)

#### GLM-5.1：开推理全方位优于关推理

| GLM-5.1 | DD Recall | DD Tokens | DD Wall | TG Bug Recall | TG v1 Pass | TG Wall |
|---|---|---|---|---|---|---|
| 关推理 | 100% | 56,595 | 158s | 80% | 77.8% | 80s |
| **开推理** | 100% | **30,261** (-47%) | 221s (+40%) | 80% | **100%** (+22pp) | 166s (+108%) |

**结论**：GLM-5.1 应该保持开推理。Token 省了将近一半，v1 pass 从 77.8% 拉到 100%（生成的用例不再有错误），静默冻结没有触发。唯一代价是 wall clock 翻倍，但对于 CI/批跑场景，质量优先于速度。

#### GPT-5.5：关推理有损

| GPT-5.5 | TG Bug Recall | TG v1 Pass |
|---|---|---|
| 默认推理 | 100% | 100% |
| reasoning_effort=none | 80% | 87.5% |

GPT-5.5 开推理时是唯一达到 TestGen 双 100% 的模型。但当前配置为 `reasoning_effort=none`（按你的偏好）。

#### DeepSeek V4：无法对比

`disable_thinking` 去掉后 API 路由到 `deepseek-reasoner`，而 reasoner 不支持 `tool_choice`，直接报错。这说明 `disable_thinking` 对 DeepSeek V4 不仅是关推理，更是 API 变体选择器——**必须保持 True**。

#### MiMo-V2.5-Pro：首次参赛即达一流

| MiMo-V2.5-Pro | DD Recall | DD Tokens | DD Steps | TG Bug Recall | TG v1 Pass | TG Wall |
|---|---|---|---|---|---|---|
| 首次跑 | 100% | 44,314 | 5 | 80% | 100% | 93s |

DesignDoc 100% 双满分，效率仅次于 GLM-5.1（44k vs 30k tokens）。TestGen 达到 80% 天花板且 v1 pass 100%（与 GPT-5.4 和 DS-V4-Pro 同档）。综合来看跻身第一梯队，性价比优秀。

---

### 关键发现

#### 1. DesignDoc 天花板已到——需要更难的数据集

GLM-5.1 / GPT-5.5 / GPT-5.4 / DS-V4-Pro 四家全部 100% recall + 100% precision。当前 40 条 golden 区分度不够。

**性价比排名**：GLM-5.1 开推理 (30k tokens, 4步) > DS-V4-Pro (63k, 9步) > GPT-5.4 (145k, 9步) > GPT-5.5 (297k, 12步)

#### 2. TestGen v2 bug recall 天花板是 80%

所有模型全部卡在 80%，没有任何模型突破。BUG-002（cooldown isolation）是系统性盲区——需要更场景化的测试用例策略，不是换模型能解决的。

#### 3. 推理能力对测试生成质量有明确帮助

GLM-5.1 和 GPT-5.5 的数据一致表明：开推理 → v1 pass 显著提升（生成的用例更正确），但对 bug recall 的提升有限（BUG-002 仍是盲区）。

#### 4. 成本估算

| Provider | DesignDoc | TestGen | 单次总成本（粗估） |
|---|---:|---:|---|
| GPT-5.5 | 297k | 187k | ~$3-5 |
| GPT-5.4 | 145k | 13k | ~$1-2 |
| GPT-4.1 | 104k | 10k | ~$0.6 |
| GLM-5.1 开推理 | 30k | 21k | ~¥0.5 |
| DS-V4-Pro | 63k | 20k | ~¥0.3 |

---

### 实验局限

1. **N=1**：单次跑，temperature=0 下波动小
2. **Golden 上限**：当前 40 条区分度不够，4 家已达 100% 天花板
3. **GPT-5.5 temperature=1**：与其他模型不完全可比
4. **DeepSeek V4 推理无法关闭**：API 层面的变体选择限制

---

### 负面结果

#### GLM-4.6 多轮 tool-calling 协议错乱

第 3 步后返回 XML 格式 tool_call arguments，LiteLLM 未归一化，Pydantic 校验全部拒绝。

#### DeepSeek V4 无法切到推理变体

去掉 `disable_thinking` 后路由到 `deepseek-reasoner`，报错 `does not support this tool_choice`。`disable_thinking=True` 是必需的 API 变体选择器。

#### Gemini 调研（已下线）

LiteLLM 不翻译 `tool_choice="required"` → recall 0%。直调 google-genai SDK → infinite emit → 撞 1M tokens/min 限流。

---
