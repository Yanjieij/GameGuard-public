# 技能系统设计规范 v1.0

> **状态**：已评审 · **版本**：v1.0 · **owner**：战斗策划组 · **last-mod**：2026-04-10
> 本文档为 GameGuard demo 所用的**虚构**技能系统规范，格式参考米哈游内部设计文档的常见结构（基础数据表 + 公式 + 状态机 + 不变式）。在真实项目中，这类文档通常存放在飞书 Docs，Agent 会通过 OpenAPI 拉取。

## 1. 背景与目标

v1 版本提供一套最小可玩的法术技能（四个），用于验证战斗系统核心循环：
- 伤害结算（含暴击）
- 冷却与资源管理
- Buff/Debuff 叠加规则
- 施法状态机（含打断）

本文档是**唯一事实源**（Single Source of Truth, SSoT）。任何实现细节如与本文冲突，以本文为准；如本文缺失，需补充条款再实现。

## 2. 术语

| 术语 | 含义 |
|---|---|
| Caster | 施法者 |
| Target | 受击者 |
| Cast time | 施法前摇（秒） |
| Cooldown (CD) | 冷却时间。冷却**从施法完成瞬间**开始计时 |
| MP | 法力值；技能消耗 MP，不足时无法释放 |
| Crit | 暴击；默认 20% 概率，1.5× 伤害 |
| DoT | Damage over Time，持续伤害型 debuff |

## 3. 角色基础数据

| ID | 名称 | HP | MP |
|---|---|---|---|
| `p1` | Player | 500 | 100 |
| `dummy` | Training Dummy | 1000 | 0 |

## 4. 技能数据表

| ID | 名称 | MP 消耗 | Cast time | CD | 基础伤害 | 伤害系 | Self Buff | Target Buff |
|---|---|---|---|---|---|---|---|---|
| `skill_fireball` | 火球术 | 30 | 1.0s | 8s | 50 | Fire | — | — |
| `skill_frostbolt` | 寒冰箭 | 25 | 1.5s | 6s | 40 | Frost | — | `buff_chilled` |
| `skill_ignite` | 点燃 | 40 | 0.0s (瞬发) | 12s | 0 | Fire | — | `buff_burn` |
| `skill_focus` | 奥术凝神 | 20 | 2.0s | 20s | 0 | — | `buff_arcane_power` | — |

### 4.1 伤害公式

对于所有造成直接伤害的技能：

```
final_damage = base_damage * crit_mult * (1 + arcane_power_bonus)
where
  crit_mult          = 1.5 if crit else 1.0
  arcane_power_bonus = 0.2 if caster has buff_arcane_power else 0.0
```

- `base_damage` 读表（skill_fireball = 50，skill_frostbolt = 40，等）
- `crit` ∼ Bernoulli(0.2)；同一个技能的**每次**伤害结算独立掷骰
- 多个 Arcane Power 实例**不叠加**（stack_rule = refresh）

### 4.2 DoT 公式（`buff_burn`）

```
damage_per_tick = magnitude * tick_dt     # 10 * 0.05 = 0.5 per tick
total_damage    = magnitude * duration    # 10 * 4.0 = 40 over full duration
```

> **实现要点**：DoT 每 sandbox tick 扣一次血，单 tick 伤害等于 `magnitude * tick_dt`。全程总伤 `magnitude * duration`，**与 tick_dt 无关**（即提高帧率不应累计额外伤害）。这是 BUG-004 的不变式。

## 5. Buff/Debuff 数据表

| ID | 名称 | Magnitude | Duration | Stack Rule | Max Stacks | 说明 |
|---|---|---|---|---|---|---|
| `buff_chilled` | 冰寒 | 0.3 | 8.0s | refresh | 1 | 减速 30%（debuff） |
| `buff_burn` | 燃烧 | 10 dmg/sec | 4.0s | refresh | 1 | DoT |
| `buff_arcane_power` | 奥术充能 | 0.2 | 10.0s | refresh | 1 | 下次施法 +20% 伤害 |

### 5.1 Stack Rule 语义

- **refresh**：重新施加时，duration 刷新为满值，magnitude **保持不变**（= 表值），stacks 保持 1。
- **replace**：重新施加时，duration & magnitude 替换为表值。
- **add**：stacks +1（不超过 max_stacks），duration 刷新。
- **independent**：每次施加产生独立实例，各自计时。

> **关键不变式（BUG-002 用）**：在 `refresh` 语义下，**不能**将新旧 magnitude 相加；多次刷新后 magnitude 必须仍等于表值。

## 6. 施法状态机

```
IDLE ──cast──► CASTING ─(cast_time elapsed)─► apply damage/buffs ─► IDLE (start CD)
                 │
                 ├─(interrupt)──► IDLE (refund MP, no CD)
                 │
                 └─(stunned)────► STUNNED (same as interrupt)
```

### 6.1 状态转换规则

- **进入 CASTING**：扣除 `mp_cost`；`casting_skill = skill_id`；`cast_remaining = max(cast_time, tick_dt)`。
- **正常完成**：
  1. `cooldowns[skill_id] = cooldown_from_table`（**冷却从此刻开始**）
  2. 结算伤害（见 §4.1）
  3. 施加 Self Buff / Target Buff（见 §5.1）
  4. `state = IDLE`; `casting_skill = None`; `cast_remaining = 0`
- **被打断 / 被眩晕**：
  1. `mp += mp_cost`（**全额退还**）
  2. `state = IDLE`; `casting_skill = None`; `cast_remaining = 0`
  3. **不进入冷却**
  4. 事件 `cast_interrupted` 必须带 `meta.mp_refunded = True`

### 6.2 冷却计时

- 冷却**仅**在施法完成的 tick 开始递减；每 tick 递减 `tick_dt`，到 0 清除。
- 同一个 Caster 在 A 技能冷却期间释放 B 技能 **不影响** A 的冷却（这是 BUG-001 的不变式）。

## 7. 不变式（Invariants, Given-When-Then）

以下条款**机器可验证**，将被 DesignDocAgent 解析为结构化 `Invariant`，并由 TestGenAgent 生成对应用例。

1. **I-01 HP 非负**：ALWAYS 所有角色的 `hp >= 0`。
2. **I-02 MP 非负**：ALWAYS 所有角色的 `mp >= 0`。
3. **I-03 冷却递减**：AFTER `cast_complete(actor, skill, t_cast)` → 在 `t_cast + cooldown` 之前，`cooldowns[skill]` 持续从 `cooldown` 线性递减到 0；误差 ≤ 1 tick_dt。
4. **I-04 冷却隔离**：AFTER `cast_complete(actor, A, t_A)`：在 `t_A + cooldown_A` 内，对同一 actor 释放 B 不得改变 A 的剩余冷却（允许误差 ≤ 1 tick_dt）。
5. **I-05 Buff 刷新不叠加 magnitude**：ALWAYS 对于 `stack_rule=refresh` 的 buff，其活动实例的 `magnitude` 等于表值（tolerance 1e-6）。
6. **I-06 Buff stack 上限**：ALWAYS 对任意角色，`sum(b.stacks for b in buffs if b.spec_id==X) <= max_stacks(X)`。
7. **I-07 打断清除施法字段**：AFTER `cast_interrupted(actor)`：`casting_skill is None` 且 `state == IDLE`。
8. **I-08 打断退款 MP**：AFTER `cast_interrupted(actor, skill)`：事件 `meta.mp_refunded == True` 且角色 MP 值反映退款。
9. **I-09 DoT 总伤可预测**：ON `buff_burn` expired on target：该 buff 生命周期内对 target 累积 burn 伤害 ≈ `magnitude * duration`（误差 ≤ 0.5）。
10. **I-10 重放确定性**：同一 seed 跑两次相同动作序列，产生的 `EventLog` 事件序列严格相等（忽略 wall-clock 时间戳）。

## 8. 非目标

- 不模拟位置、碰撞、射线检测——这些在 v1 属于战斗以外的系统。
- 不模拟网络同步 / rollback——v1 单机语义。
- 不模拟动画事件时间窗（`damage_event_at` 字段仅用于未来扩展）。

## 9. 变更历史

| 版本 | 日期 | 变更 | Owner |
|---|---|---|---|
| 1.0 | 2026-04-10 | 初版发布 | 战斗策划组 |
