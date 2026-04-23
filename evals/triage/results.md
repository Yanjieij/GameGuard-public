# TriageAgent 评估结果

- Fixture：handwritten.yaml 在 pysim:v2 上跑（真实 5-bug 失败）
- Ground truth bug 组：5

## Ground Truth

- **BUG-001**：`['cooldown-isolation-fireball-then-frostbolt']`
- **BUG-002**：`['buff-chilled-refresh-magnitude-stable']`
- **BUG-003**：`['interrupt-refunds-mp']`
- **BUG-004**：`['dot-burn-total-damage-predictable']`
- **BUG-005**：`['replay-determinism-fireball-frostbolt']`

## 各次运行

| # | cluster_recall | cluster_precision | agent clusters | wall (s) |
|---|---:|---:|---:|---:|
| 1 | 100.00% | 100.00% | 5 | 74.7 |
| **mean** | **100.00%** | **100.00%** | — | 74.7 |

## 结论

✓ Triage 聚类质量可用
