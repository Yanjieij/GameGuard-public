# CriticAgent 评估结果

- Fixture：6 条 broken + 4 条 correct
- Runs：1

## Fixture 明细

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

## 各次运行

| # | accuracy | precision | recall | tp | fp | fn | wall (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 80.00% | 100.00% | 66.67% | 4 | 0 | 2 | 85.4 |
| **mean** | **80.00%** | **100.00%** | **66.67%** | — | — | — | 85.4 |

## 结论

△ Critic 能识别大部分问题（accuracy 80%）
