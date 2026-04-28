# GameGuard · Agent 效果评估

> 最近更新：2026-04-29

## 当前数据

模型对比数据是项目持续追踪的核心指标，详见 **[MODEL_COMPARISON.md](MODEL_COMPARISON.md)**。

### 当前最佳成绩

| 任务 | 最佳模型 | 成绩 | 天花板 |
|---|---|---|---|
| DesignDoc（文档→不变式） | GLM-5.1 / MiMo-V2.5-Pro / GPT-5.4 / GPT-5.5 / DS-V4-Pro | **100% recall, 100% precision** | 40 条 golden 上限已饱和 |
| TestGen（不变式→用例→bug 召回） | GPT-4.1 / GPT-5.4 / GLM-5.1 / DS-V4-Pro / MiMo | **80% v2 bug recall, 100% v1 pass** | BUG-002 是系统性盲区 |
| Triage（失败聚类→Bug 单） | — | **100% cluster recall / precision** | 规则+LLM 两阶段可用 |
| Critic（用例 review） | — | **80% accuracy, 100% precision** | patch > drop 策略有效 |

### 参赛模型：10+ 个 provider

DeepSeek-chat / DeepSeek-V4-Flash / DeepSeek-V4-Pro / GLM-4.6 / GLM-4.7 / GLM-5.1 / GPT-4.1 / GPT-5.4 / GPT-5.5 / MiMo-V2.5-Pro。（Gemini 2.5 曾参赛，因 LiteLLM 协议兼容问题下线。）

---

## Agent 独立 Eval（历史诊断数据）

以下数据来自各 Agent 独立 eval，最后一次完整跑是 **2026-04-18**。当时的 prompt 版本下 DesignDoc recall 仅 55.56%、TestGen v2 recall 仅 20%。经过 prompt 强化（codex-strengthen-design-doc-evals branch），当前 recall 已达 100% / 80%（见 MODEL_COMPARISON.md）。

详细运行时数据见 [`evals/agent-eval-details.md`](evals/agent-eval-details.md)（`make eval` 或 `python -m evals.rollup` 自动生成）。

---

## Eval 运行

```bash
# 跑全部 agent eval + rollup（费钱费时，仅开发诊断用）
make eval

# 跑模型对比（推荐，持续追踪用）
python -m evals.compare_models --models glm-5.1,mimo-v2.5-pro --runs 1
```
