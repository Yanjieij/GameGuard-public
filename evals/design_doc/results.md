# DesignDocAgent 评估结果

- 文档：`docs/example_skill_v1.md`
- Golden required：18 条；optional：3 条
- Runs：1

### 各次运行

| # | recall | precision | steps | tokens | USD | wall (s) |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 55.56% | 100.00% | 20 | 0 | $0.0000 | 0.0 |
| **mean** | **55.56%** (σ=0.00%) | **100.00%** (σ=0.00%) | — | 0 | $0.0000 | 0.0 |

## 被漏抽的 required invariant

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

## 结论

✗ 需优化——召回不足 75%
