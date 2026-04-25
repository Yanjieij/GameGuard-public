# 技能系统扩展规范 v2.0

> 状态：评测用 fixture · 版本：v2.0 · owner：战斗策划组

本文档用于评估 DesignDocAgent 是否能从更长、更分散的策划文档中抽取机器可验证不变式。
它包含多角色、多个 DoT、refresh / add / independent stack rule，以及全局 replay 要求。

## 1. 目标

v2 在 v1 法术系统上增加团队战斗语义：
- 玩家 `p1` 可以攻击 `dummy`，也可以给盟友 `ally` 加 buff。
- 部分 buff 只刷新时长，部分 buff 可以叠层，部分 DoT 必须按生命周期总量结算。
- 所有正确实现都必须能在同一 seed 下重放出相同事件序列。

## 2. 角色数据

| ID | 名称 | HP | MP |
|---|---|---:|---:|
| `p1` | Player | 520 | 120 |
| `ally` | Ally | 420 | 80 |
| `dummy` | Training Dummy | 1200 | 0 |

## 3. 技能数据表

| ID | 名称 | MP 消耗 | Cast time | CD | 基础伤害 | Self Buff | Target Buff |
|---|---|---:|---:|---:|---:|---|---|
| `skill_fireball` | Fireball | 30 | 1.0s | 8s | 50 | - | - |
| `skill_poison` | Poison Dart | 35 | 0.8s | 10s | 10 | - | `buff_poison` |
| `skill_barrier` | Barrier | 25 | 0.0s | 14s | 0 | `buff_barrier` | - |
| `skill_focus` | Focus | 20 | 2.0s | 20s | 0 | `buff_arcane_power` | - |

## 4. Buff 数据表

| ID | Magnitude | Duration | Stack Rule | Max Stacks | 说明 |
|---|---:|---:|---|---:|---|
| `buff_poison` | 8 dmg/sec | 5.0s | refresh | 1 | DoT，不允许刷新时叠加 magnitude |
| `buff_barrier` | 50 shield | 6.0s | refresh | 1 | 护盾，刷新只重置 duration |
| `buff_arcane_power` | 0.25 | 10.0s | refresh | 1 | 下次直接伤害 +25% |
| `buff_bleed` | 4 dmg/sec | 6.0s | add | 3 | DoT，可叠层但最多 3 层 |

### 4.1 DoT 结算

`buff_poison` 的完整生命周期总伤必须为 `magnitude * duration = 8 * 5.0 = 40`，误差不超过 0.5。
`buff_bleed` 每层完整生命周期总伤为 `4 * 6.0 = 24`，但本轮评测只要求抽取 stack 上限，不要求抽取总伤。

### 4.2 Stack 语义

- `refresh`：重新施加时只刷新 duration，magnitude 保持表值，stacks 保持 1。
- `add`：重新施加时 stacks +1，但不得超过 max_stacks。
- `independent`：每次施加生成独立实例，各自倒计时。

## 5. 施法状态机

- 进入 CASTING 时立即扣除 MP，并记录 `casting_skill`。
- 正常完成时清除 `casting_skill`，写入技能 CD，并施加伤害和 buff。
- 被打断时清除 `casting_skill`，全额退还本次技能 MP，且不进入 CD。
- 瞬发技能的 `cast_time = 0`，不进入可打断的 CASTING 窗口。

## 6. 冷却规则

- 冷却从施法完成 tick 开始递减。
- 同一个 actor 释放 B 技能不得修改 A 技能仍在进行中的 cooldown。

## 7. 机器可验证不变式

1. **A-01 生命非负**：ALWAYS 所有角色 `hp >= 0`。
2. **A-02 法力非负**：ALWAYS 所有角色 `mp >= 0`。
3. **A-03 冷却递减**：AFTER `cast_complete(actor, skill)`，对应技能冷却从表值线性递减到 0。
4. **A-04 refresh magnitude 稳定**：所有 `stack_rule=refresh` 的 buff 刷新后 magnitude 等于表值。
5. **A-05 stack 上限**：所有 buff 的 stacks 不得超过表中 `max_stacks`。
6. **A-06 打断清状态**：AFTER `cast_interrupted(actor)`，`casting_skill is None` 且 `state == IDLE`。
7. **A-07 打断退款**：AFTER `cast_interrupted(actor, skill)`，MP 全额退还。只适用于 `cast_time > 0` 的技能。
8. **A-08 Poison 总伤**：ON `buff_poison` expired on target，总伤约等于 40，误差不超过 0.5。
9. **A-09 重放确定性**：同一 seed、同一动作序列，两次 EventLog 严格一致（忽略 wall-clock 时间戳）。

## 8. 非目标

- 本 fixture 不评估护盾吸收顺序。
- 本 fixture 不评估 `buff_bleed` 的 DoT 总伤。
