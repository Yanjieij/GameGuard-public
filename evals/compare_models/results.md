# LLM Provider 对比实验

跑 DesignDoc + TestGen 两个 eval 在 4 个 provider 上（DeepSeek / GLM-4.6 /
GLM-5.1 / GPT-4.1），看谁能抓到多少 invariant / 用例质量 / 成本 / 协议兼容性。

**方法**：temperature=0，单次跑，看绝对数字和协议兼容性。同 provider 的
variance 在这里不重要（temperature=0 + 缓存命中后完全确定）；重要的是不同
provider 在**同一任务、同一 prompt、同一工具集**下的对比。

## 参赛 provider 说明

| Provider | Model | 走谁 | disable_thinking | 备注 |
|---|---|---|---|---|
| DeepSeek-chat | `deepseek/deepseek-chat` | LiteLLM | — | 非推理型，当前 default |
| GLM-4.6 | `zai/glm-4.6` | LiteLLM | — | 非推理型，智谱当家 |
| GLM-5.1 | `zai/glm-5.1` | LiteLLM | ✓ | 推理型，disable_thinking 标配 |
| GPT-4.1 | `openai/gpt-4.1` | LiteLLM | — | OpenAI 2025 tool-use 优化款 |

> Gemini 2.5 Flash / Pro 曾在参赛池，因协议适配问题下线；详见文末
> **负面结果 · Gemini 调研**。

---

## DesignDoc 任务对比（从策划文档抽 invariant）

| Provider | recall | precision | steps | tokens | wall (s) | 备注 |
|---|---:|---:|---:|---:|---:|---|
| **GLM-5.1** | **83.33%** | **89.47%** | 5 | 21,288 | 96.3 | 🏆 最佳召回：一次 emit 17 条，17/18 required 命中 |
| GPT-4.1 | 55.56% | 100.00% | 11 | 104,038 | 30.1 | 中文文档召回差 GLM-5.1 近 28pp；precision 完美 |
| DeepSeek-chat | 55.56% | 100.00% | 20 | (cached) | (cached) | 稳：召回一般但 precision 完美 |
| GLM-4.6 | 0.00% | 0.00% | 20 | 69,365 | 32.1 | ✗ LiteLLM 协议错乱（见坑 1） |

## TestGen 任务对比（完整 plan 流水线）

| Provider | v2 bug recall | v1 pass% | 生成用例数 | steps | wall (s) | 备注 |
|---|---:|---:|---:|---:|---:|---|
| **GPT-4.1** | **80.00%** | **100.00%** | 5 | 3 | 10.6 | 🏆 2× GLM-5.1 的 bug 召回，v1 pass +12.5pp，10 秒跑完 |
| GLM-5.1 | 40.00% | 87.50% | 8 | — | 285.5 | 慢但也能跑，生成用例多 |
| DeepSeek-chat | 20.00% | 57.00% | 7 | — | (cached) | baseline |
| Handwritten baseline | 100% | 100% | 12 | — | 0 | 人类 ground truth |

GLM-4.6 上游 DesignDoc 已 0%，不值得跑 TestGen。

---

## 一句话结论

> **两阶段用两家**：DesignDoc 阶段用 **GLM-5.1**（83% recall > GPT-4.1 的
> 55.6%，中文策划文档语境下差距显著）；TestGen 阶段用 **GPT-4.1**（80% v2 bug
> 召回、100% v1 pass、10 秒跑完——碾压 GLM-5.1 在这一环的 40% / 87.5% / 285s）。
>
> **如果只能选一家**：GPT-4.1 综合最强。DesignDoc 55.6% 虽不是最好但
> precision 100%，下游 TestGen 能补回——TestGen 的 80% v2 bug 召回是
> GameGuard 最关键的业务指标（"能不能抓住回归 bug"），比 DesignDoc 的
> "有没有抽全不变式" 更接近 QA 系统的真实产出。

---

## 踩过的坑

### 坑 1 · GLM-4.6 多轮 tool-calling 协议错乱

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

### 负面结果 · Gemini 调研（已下线）

Gemini 2.5 Flash / Pro 曾在参赛池，踩到两层协议适配坑后下线：

1. **LiteLLM 不翻译 `tool_choice="required"`**：OpenAI 语义应映射到 Gemini 的
   `functionCallingConfig.mode="ANY"`，LiteLLM 1.83 没做此翻译，LLM 读完文档就
   `no_tool_calls` 早退，recall 0%。
2. **绕开 LiteLLM 直调 google-genai SDK 后撞 infinite emit**：mode=ANY 下
   Gemini 一轮并发 28 个 `emit_invariant` 且不主动 finalize，step=167 时撞
   1M tokens/min 限流。prompt 层短期无解。

**结论**：问题在 Gemini 侧"ANY 模式不主动收敛"的行为，DeepSeek / GLM / GPT-4.1
在同 prompt 下都能正常 finalize。相关代码已于 D19 整体下线。

---

## 为什么 GLM-5.1 在 DesignDoc 强而在 TestGen 弱

DesignDoc 和 TestGen 对 LLM 的要求其实完全不同：

- **DesignDoc**：从长中文文档里抽结构化信息——考**阅读理解 + 中文语感 +
  一次性 emit 17 条的规划能力**。GLM-5.1 的推理型 + 中文语料占比高，正好
  吃透这个场景。
- **TestGen**：给定 invariant 清单生成可执行 YAML 测试用例——考**tool-calling
  稳定性 + 少量工具的紧凑规划 + 不生成 broken case 的静态正确性**。GPT-4.1
  作为 function-calling 协议原产地 + 2025 tool-use 优化款，在这个环节
  形态全对。GLM-5.1 的推理能力在这反而是负担——思考过多、生成用例数多
  （8 vs 5）、但 v1 pass 掉到 87.5%（GPT-4.1 是 100%）。

## 为什么 GPT-4.1 TestGen 能 3 步 / 10 秒跑完

猜测（需要更多 eval 验证）：

1. **上游 DesignDoc 已缓存**：跑 DesignDoc 时已产出 10 条 invariant，
   TestGen 阶段直接读缓存，steps=3 只是它"扫一遍 invariants → 一次 emit
   5 条 testcase → finalize"。
2. **function-calling 原生训练**：GPT-4.1 的 tool-use 训练强度足以让
   "一口气生成 5 条结构正确的 testcase" 成为 default 行为，不需要多轮
   self-correction。
3. **不过度 emit**：只生成 5 条（GLM-5.1 生成 8 条），其中 4 条命中 v2
   bug，recall 80%。GLM-5.1 虽然生成 8 条但只有 4 条真的挑出 bug。

## 成本考量（估算）

| Provider | DesignDoc tokens | TestGen tokens | 粗估单次成本 |
|---|---:|---:|---:|
| GLM-5.1 | 21k | — | ~¥0.4 |
| GPT-4.1 | 104k | 10k | ~$0.6（¥4.3） |
| DeepSeek-chat | — (cached) | — (cached) | ¥0.02-0.1 |

GPT-4.1 是 DeepSeek 的 50-200×、GLM-5.1 的 10×。但在 TestGen 这一环
(80% vs 20% vs 40%) 的质量差，对 QA production 场景——production 里漏抓
一个 regression bug 的代价远高于单次实验的 $0.5——显然质量优先。

---

## 还没测的 provider（有 key 之后可补）

| Provider | 预期 | 为什么想跑 |
|---|---|---|
| Claude Sonnet 4 | recall 75-85% | tool-calling 行业金标准；贵 3-5× |
| GPT-5 | recall 85%+ | 2025 旗舰，比 gpt-4.1 再高几 pp |
| OpenAI o4-mini | recall 72-82% | 推理型 + 便宜，对比 GLM-5.1 的推理型优势是否普遍 |

---

## 方法论 / 实验的局限

1. **N=1 不足以看 variance**——temperature=0 + 缓存的情况下单次跑就能
   代表平均，但 LLM 有日常波动（模型版本升级、后端路由变化）。生产要长期
   监控。
2. **goldens 偏严格**——18 条 required 是我手工标注的"理想抽取"，可能过度
   理想。
3. **LiteLLM 的 USD 报告对 OpenAI 失灵**——GPT-4.1 实际成本约 $0.6（按
   input $2.5/1M + output $10/1M 的公开定价 + 104k input + 2k output
   估算），但 `client.used_usd` 报 $0。下一版 compare_models 要修。
4. **TestGen 缓存了 DesignDoc 可能对 GPT-4.1 有利**——steps=3 / 10.6s
   的惊人效率部分来自缓存命中。若清缓存跑整个 pipeline，GPT-4.1 总 wall
   大概是 40-50s（DesignDoc 30s + TestGen 10s），仍然远快于 GLM-5.1。

这些限制是**项目保持诚实的一部分**——面试讲"这是我当前能看到的数字 + 已
知的边界"比吹"100% 召回"有说服力得多。
