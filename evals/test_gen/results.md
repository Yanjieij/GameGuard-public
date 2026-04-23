# TestGenAgent 评估结果

- 模式：discovery
- Runs：1

## Baseline（handwritten.yaml）

- 用例数：12
- v1 pass 率：100% (12/12)
- v2 抓到的 bugs：['BUG-001', 'BUG-002', 'BUG-003', 'BUG-004', 'BUG-005']
- v2 bug 召回：100% (5/5)

## Agent 生成 vs Baseline

| # | 用例数 | v1 pass% | v2 抓到 bugs | v2 召回 | wall (s) |
|---|---:|---:|---|---:|---:|
| baseline (handwritten) | 12 | 100% | ['BUG-001', 'BUG-002', 'BUG-003', 'BUG-004', 'BUG-005'] | 100% | — |
| Agent run 1 | 7 | 57% (4/7) | ['BUG-001'] | 20% | 115.1 |
| **Agent mean** | 7.0 | **57%** | — | **20%** | 115.1 |

## 每个 BUG 被 Agent 抓到的次数

| Bug | Agent 抓到次数 | baseline |
|---|---:|---|
| BUG-001 | 1/1 | ✓ |
| BUG-002 | 0/1 | ✓ |
| BUG-003 | 0/1 | ✓ |
| BUG-004 | 0/1 | ✓ |
| BUG-005 | 0/1 | ✓ |

## 结论

✗ Agent 明显不及 baseline：召回 20%
