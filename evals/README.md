# GameGuard Agent Eval Harness

给项目里 5 个 Agent 各配一套量化评估，让 "Agent 效果好不好" 从定性断言变成
可复现的数字。所有子目录共享一个约定：每套 eval 产出一份 `results.md`，根目录
的 `rollup.py` 把它们聚合到 `EVAL.md`（项目根目录）。

## 子目录

| 子目录 | 评估对象 | 核心指标 |
|---|---|---|
| `design_doc/` | DesignDocAgent | 召回率 / 准确率 / 漏抽列表 |
| `test_gen/` | TestGenAgent | v1 pass 率 / v2 bug 召回 / token 消耗 |
| `triage/` | TriageAgent | 聚类召回 / 聚类准确 / 标题质量 |
| `critic/` | CriticAgent | patch/drop 决策准确率 / 修复有效性 |

## 怎么跑

```bash
# 跑单个 eval
python -m evals.design_doc.eval_design_doc

# 跑全部 + 生成汇总
python -m evals.rollup
# 输出:
#   - evals/*/results.md  各自的数字
#   - EVAL.md             项目根的汇总
```

## 共同约定

1. **Fixture 用 YAML/JSON 明文存放** —— 像测试代码一样 reviewable
2. **每个 eval 默认跑 5 次取平均** —— LLM 有 variance，单次跑的数字不可信
3. **默认复用 LLMClient 的磁盘缓存** —— 命中缓存后零成本，方便反复跑
4. **真跑 LLM 前会打印 "estimated cost" 并等 3 秒** —— 避免误 burn token

## 成本估算

单次全套 eval（5 个 Agent × 5 次重复）在 DeepSeek 上约 ~1M token，¥2-3。
命中缓存后零成本。
