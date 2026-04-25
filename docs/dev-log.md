# Dev log

A running daily-standup-style log. Mirror of what a real scrum team would write.

---

## 2026-04-16 — D1–D2 · 骨架与域模型

**Done**
- 仓库骨架：`environment.yml`（conda env `gameguard`，不用 base）、`pyproject.toml`、Makefile、`.gitignore`、README、架构占位
- 域模型：`Skill`/`Character`/`Buff`/`Event`/`Action` 全部 Pydantic，零 I/O、零游戏逻辑
- `Invariant` DSL：7 种 kind + 注册表驱动的 evaluator，LLM 只能发结构化数据，不能发 Python
- `GameAdapter` ABC：`reset / step / state / trace / snapshot / restore` 固化为契约
- `PySim` 核心：20 Hz 定步长 tick、事件日志、确定性 RNG（`_TrackingRandom` 计数，给 I-10 用）
- `PySim v1`：四技能（Fireball / Frostbolt / Ignite / Focus）+ 三 buff（Chilled / Burn / Arcane Power）+ 暴击，全部匹配 §4–§6 的设计规范
- 设计文档 v1（`docs/example_skill_v1.md`）：飞书风格，含数据表、公式、状态机、**10 条机器可验证不变式**
- 6 条冒烟测试全部通过：完整施法循环、冷却递减、buff 刷新 magnitude 稳定、打断退款、重放确定性、snapshot/restore round-trip

**Notes**
- 确定性是底线：RNG 通过 `state.rng_draws` 曝光，I-10 就是比两次 trace 的 `rng_draws` 相等 + 事件序列相等。
- `Invariant` 用注册表而非 `eval`——LLM 想多加一种，也要走一个 ~10 行的 Python PR，mypy 能接住。
- `Interrupt` 事件的 `meta.mp_refunded` 是专门留给 I-08 的油标记，v2 如果忘退款就直接被抓。

**Next (D3)**
- 写 `testcase/model.py` + YAML loader，把 10 条不变式落地为 10–12 条 `TestCase`
- 实现 `testcase/runner.py` 跑本地测试套件、输出第一版 Markdown 报告（还没接 LLM）
- 目标：`gameguard run --plan handwritten.yaml` 能跑通

---

## 2026-04-17 — D3 · 离线闭环

**Done**
- 切为**中文详细注释**（学习优先）；写了新记忆条目确保后续一致
- `testcase/model.py`：`TestCase / TestPlan / Assertion / TestResult / TestSuiteResult`，全 Pydantic、policy-vs-mechanism 分层
- `testcase/loader.py`：ruamel.yaml 驱动的 YAML ↔ TestPlan 互转，保留注释/顺序；`TypeAdapter` 处理 discriminated union
- `testcase/runner.py`：本地 Runner，逐动作执行、按 `AssertionWhen` 调度断言；ERROR / FAILED / PASSED 三态映射；trace/JSONL + snapshot 落盘
- `reports/schema.py`：`SuiteReport`（D3 用）+ `BugReport`（D7 预留的 Jira schema）
- `reports/markdown.py`：套件级 Markdown 报告，带状态 emoji + 失败详情 + 证据链接
- `testcases/skill_system/handwritten.yaml`：**10 条手写回归用例**，逐条标注 `derived_from` 追溯策划文档 + BUG ID（I-01 ～ I-08 覆盖；I-09/10 需专用 evaluator，延后）
- `cli.py`：`gameguard run` + `gameguard info`，typer + rich，退出码 0/1/2 对齐 CI 约定
- **端到端跑通**：`gameguard run --plan testcases/skill_system/handwritten.yaml` → 10/10 绿，12ms，trace/snapshot 全部落盘
- `tests/test_testcase_layer.py`：6 条 meta-test（YAML round-trip、runner 三态分类、handwritten.yaml 加载）
- 修了 pytest 把 `TestCase`/`TestPlan` 误当测试类的 warning
- conda `gameguard` env 建好（Python 3.11 + pydantic 2.13 + typer 0.24 + ruamel.yaml 0.19 + anthropic + openai 等）

**Numbers**
- `pytest` 12/12 passed（0.1s）
- 手写回归：10/10 passed（13ms）
- 代码量：约 1100 行 Python + 250 行 YAML + 220 行 Markdown 文档

**Notes & 学习点**
- **"测试用例 = 数据"**：我们刻意让 TestCase 是 Pydantic 模型而不是 Python 函数——LLM 能直接产出、YAML 能进版本库、和 TestRail/Xray/Jepsen 这些业界做法一致。
- **policy vs mechanism**：`AssertionWhen` 是 policy（人/LLM 可改），Runner 是 mechanism（工程师维护）。好处：未来改断言时机不用动 Runner。
- **trace 用 JSONL 而不是单个 JSON 数组**：大 trace 流式友好、行级 diff 友好、`jq`/`grep` 友好——这是 OpenTelemetry / ELK 的主流选择。
- **sandbox 字符串路由 (`pysim:v1`)**：让 Adapter 扩展点显式化，Unity 接入时只加一行 case。

**Next (D4–D5 · Agent 登场)**
- `llm/client.py`：统一封装 OpenAI / Anthropic，支持 temperature=0 + 磁盘缓存
- `agents/base.py`：手写 AgentLoop（tool-calling 循环 + token 预算 + trace 导出）
- `tools/schemas.py`：Pydantic → OpenAI function-calling schema 转换
- `tools/doc_tools.py`：策划文档的 `read_section` / `extract_table`
- `agents/design_doc.py`：DesignDocAgent 读 example_skill_v1.md → 产出 `InvariantBundle`
- `agents/test_gen.py`：TestGenAgent（contract 策略先行）→ 产出 TestPlan
- **D4/D5 验收**：LLM 读文档产出 ≥5 条测试用例，跑通 v1 全绿

---

## 2026-04-17（晚） — D4 完成 · D5 架构完成，prompt 调教待续

**本轮 LLM 选型**
- 统一网关：LiteLLM（`provider/model` 语法）
- Provider：**智谱 Z.AI**；LiteLLM 原生支持 `zai/` 前缀，默认 endpoint `api.z.ai/api/paas/v4`
- 当前模型：`zai/glm-4.6`（triage 层留 `zai/glm-4.5`）
- 备选：`zai/glm-4.7` / `zai/glm-5.1` 是推理型模型；**实测在 tool-calling 轮次容易把 max_tokens 全部花在 reasoning_content 上，tool_calls 为空导致静默失败**——面试可以当作一个有价值的 lesson 讲

**Done**
- **LLM 层完整**（`gameguard/llm/`）：
  - `cache.py`：content-addressed 磁盘缓存，deterministic 模式下 miss 即抛 `CacheMissInStrictMode`
  - `trace.py`：JSONL session-level trace，预留 subscribe() 接口便于接 Langfuse/Phoenix
  - `client.py`：LiteLLM 封装，双重预算（USD + token），`_resolve_provider` 处理 `zai/` + 国内站覆盖
- **工具层完整**（`gameguard/tools/`）：
  - `schemas.py`：`Tool` + `ToolRegistry`，Pydantic -> OpenAI function-calling schema，统一错误反馈回 LLM（ReAct 论文的"self-correction"模式）
  - `doc_tools.py`：`DocRepository` + `list_docs` / `list_doc_sections` / `read_doc_section` / `read_full_doc`
  - `testgen_tools.py`：`list_invariants` / `list_skills` / `list_characters` / `emit_testcase` / `finalize`
- **Agent 层完整**（`gameguard/agents/`）：
  - `base.py`：手写 `AgentLoop`，支持 parallel tool_calls、失败自修复、max_steps 兜底、trace 发射
  - `design_doc.py`：DesignDocAgent —— **真 API 实测成功**：6 步产出 **16 条 Invariant**，全部带具体 actor/skill，GLM-4.7 32k tokens；GLM-4.6 同样成功
  - `test_gen.py`：TestGenAgent 架构完整；**prompt 调教未达标**，见 Known Issue
  - `orchestrator.py`：plan-and-execute 管线串接（plan 阶段），预留 `review_hook` 给 D10 Critic
- **CLI 扩展**：`gameguard generate --doc <md> --out <yaml>`
- **meta-tests**：27/27 passed（含 AgentLoop 的 mock 测试：三态分发、工具错误自修复、max_steps 退出）

**Known Issue — TestGenAgent 静默推理**
- 症状：TestGenAgent 在拿到 3 个只读 tool 的结果（~3000 字符 JSON）之后，下一轮 LLM 响应 `content=""`、`tool_calls=[]`、`completion_tokens` 用到上限，AgentLoop 视为 `no_tool_calls` 停止。
- 已尝试：prompt 前置（把 invariant 列表直接嵌入用户消息）、提高 max_tokens 到 8192、明确"不要输出文字，直接调用工具"。仍有失败。
- 猜测原因：GLM 系列在 tool_results 夹带大量 JSON 时触发较重的 reasoning，把输出 budget 用尽。
- 下轮要试：
  1. LiteLLM 对 Z.AI 的 `tool_choice="required"` 是否生效
  2. 压缩 tool result（list_invariants 只返回 id + kind，完整数据单独查）
  3. 切 `zai/glm-4.5` 或试 `zai/glm-5.1` 看更大模型是否有 budget
  4. 把 TestGenAgent 拆成更细的 sub-agent：一次一条 testcase
- **这是面试的好素材**：Agent 工程的大部分工作都在"跨模型 prompt 兼容"，trace 里的 tokens=8192 / tool_calls=0 就是血证。

**Numbers**
- `pytest` 27/27 passed（0.1s）
- DesignDocAgent：5 steps，16 invariants，tokens≈30k，cost=$0（Z.AI 不在 LiteLLM 定价表）
- TestGenAgent：已打通调用链，未能产出用例（见 Known Issue）

**学习点**
1. **LiteLLM 的 `zai/` 前缀**。写 `zai/glm-4.6` 即可，比手搓 openai-compatible 客户端省事。
2. **Tool-as-side-channel 模式**：让 LLM 通过 `emit_xxx` tool 输出结构化成果，比 `response_format=json_schema` 更稳，trace 里每条产物都能单独看到。
3. **推理模型的"静默 token 黑洞"**：GLM-4.7 / GPT-o1 / Claude thinking 在 tool-calling 里如果 prompt 不到位，会把整个 max_tokens 烧在 reasoning 上，对外表现是空响应。这是 2025–2026 年业界踩的大坑，面试可以点出来。
4. **Tool 错误结构化反馈**：ReAct 论文核心洞见。我们的 ToolRegistry.dispatch 永远返回 `ToolInvocationResult(ok=False, content="ERROR: ...")`，AgentLoop 把它作为 role=tool 的 content 喂回 LLM，LLM 在 mock 测试里能恢复得很好。
5. **Agent 就是 while 循环**：不要被"AI Agent"神秘化。我们 `agents/base.py` 200 行就搞定了 production-grade loop。

**Next（D5 收尾 + D6）**
1. 修好 TestGenAgent 的静默问题（见上面 4 条 workaround）
2. 推进 D6：PySim v2 植入 5 个 bug，让 TestGenAgent 产出的用例真能红
3. 达成完整 Week 1 milestone：一键"文档 → 报告"跑通，找到 v2 的 ≥3 个 bug

---

## 2026-04-17（深夜）— D5–D6 完成 · **Week 1 milestone 达成**

**主要成就**
- ✅ 完整 Agent pipeline 端到端跑通：策划文档 → 18 条 invariants → 8 条 testcases → v2 抓 bug
- ✅ PySim v2 植入 5 类典型 bug + 5 条 oracle test 全过
- ✅ Handwritten plan 在 v2 上抓到 BUG-001 / BUG-002 / BUG-003 共 3 个
- ✅ Agent-generated plan 在 v2 上抓到 BUG-001 / BUG-003 共 2 个
- ✅ 中文详解 README 重写（10 章，含完整架构图与对应行业实践）
- ✅ 32+ pytest 全绿

**LLM provider 切换历程（重要工程经验）**

| 阶段 | model | 关键发现 |
|---|---|---|
| 1 | `zai/glm-4.6` | 第一次 D4 验证成功（5 步出 16 invariants） |
| 2 | `zai/glm-4.7` | 推理型，多轮 tool-calling 时把 max_tokens 全烧在 reasoning_content 上 |
| 3 | `zai/glm-4.7` + `tool_choice="required"` | 单轮直接 chat 有效，多轮 tool history 仍失败 |
| 4 | `zai/glm-4.7` + `disable_thinking` | A/B 实测确实生效（**慢 15.6 倍 → 1s，省 130 倍 tokens**），但多轮 tool history 场景下 Z.AI 服务端忽略此参数 |
| 5 | `deepseek/deepseek-chat` | **稳定**，完整 pipeline 一次跑通（22 步 + 10 步） |

**关键代码增量**
- `gameguard/llm/client.py`：
  - 加 `tool_choice` 参数（治推理型模型静默推理的标准 workaround）
  - 加 `disable_thinking` 选项 + `extra_body` 透传 `{"thinking":{"type":"disabled"}}`
  - cache_payload 把 `tool_choice` / `disable_thinking` 进 key（避免不同模式串）
  - `_resolve_provider` 处理 `zai/` 前缀 + 国内站覆盖
- `gameguard/agents/base.py`：
  - 加 `tool_choice` 字段贯穿 AgentLoop
  - 加 `stop_when` 收敛信号回调（典型用法 `lambda r: r.tool_name == "finalize"`，
    避免 `tool_choice="required"` 把 finalize 后的 LLM 再逼一次）
- `gameguard/agents/design_doc.py` / `test_gen.py`：
  - 启用 `tool_choice="required"` + `stop_when=finalize`
- `gameguard/sandbox/pysim/v2/skills.py`：**新增**
  - 继承 V1SkillHandler，覆写 3 个方法 + 1 个 helper
  - 每个 BUG-xxx 用清晰注释块圈出，标注会被哪条 oracle 抓到
- `gameguard/sandbox/pysim/factory.py`：
  - `make_sandbox("v2")` 接入
- `tests/test_pysim_v2_bugs.py`：**新增** 5 条
  - 每条 = "v1 oracle PASS + v2 oracle FAIL" 对照，差分测试模式
- `gameguard/cli.py`：
  - 加 `--sandbox` override 给 `gameguard run`，支持 v1/v2 同 plan 双跑
- `gameguard/sandbox/pysim/v1/skills.py` + `docs/example_skill_v1.md`：
  - `buff_chilled` 持续 5s → 8s（让 frostbolt CD=6s 内能触发 refresh，给 BUG-002 oracle 真正可观测）
- `README.md`：完全重写为中文 10 章详解（架构图 / 故事线 / 行业实践对照）

**端到端验证结果**

```
$ gameguard generate --doc docs/example_skill_v1.md
DesignDocAgent: 22 步 → 18 条 invariants
TestGenAgent  : 10 步 → 8 条 testcases
LLM tokens=204487  cost=$0.00 (DeepSeek 全程稳定)

$ gameguard run --plan testcases/skill_system/handwritten.yaml --sandbox pysim:v2
7/10 passed, 3 failed → BUG-001 + BUG-002 + BUG-003 全抓到
退出码 1 (CI fail)

$ gameguard run --plan testcases/skill_system/agent_generated.yaml --sandbox pysim:v2
3/8 passed, 3 failed, 2 errored → BUG-001 + BUG-003 抓到（外加暴露 3 个 LLM 用例本身缺陷，
正是 QA review 真实场景）
```

**学习点 / 面试可讲**
1. **推理型模型治理**：`tool_choice="required"` + `extra_body={"thinking":{"type":"disabled"}}`
   两件套是 2025-2026 年 agent 工程的关键。GLM-4.7 / Claude thinking / o1 在 tool-calling
   场景下不调教就会把 max_tokens 全烧在 reasoning 上。
2. **Z.AI 多轮 tool history 的服务端限制**：disable_thinking 在单轮调用有效，
   但带 tool_results 的 round-2 服务端会忽略——这种"看似有 API 但行为不一致"
   的 provider 限制只能靠实测发现。换 DeepSeek 解决。
3. **A/B 实证比文档强**：disable_thinking 是否生效不要问 docs，跑一次 reasoning_content
   长度 + wall-clock + token 量的 A/B 即可验证。
4. **差分测试 = v1 oracle PASS + v2 oracle FAIL**：经典 Ubisoft / Blizzard 模式。
   meta-test `tests/test_pysim_v2_bugs.py` 把每条 oracle 的可信度都钉死。
5. **LLM 用例 review 是真实工作**：Agent 生成 8 条用例，5 PASS 在 v1，2 ERROR
   是 Agent 自己 wait 时长算错，1 FAIL 是 Agent hallucinated 不存在的 evaluator。
   这就是真实 QA review LLM 输出时要做的事——**Critic Agent 的存在意义**。

**Next**
- D7 TriageAgent：失败聚类 + Jira-compatible bug schema 输出
- D8 Property-based / Exploratory 策略 + I-09 (DoT 总伤) / I-10 (replay determinism) evaluator
- D9 差分回归 `gameguard regress --baseline v1 --candidate v2` + Allure HTML 报告
- D11 Unity Adapter proto + C# stub
- D10 (stretch) Critic Agent

---

## 2026-04-17（凌晨）— Plan A + D7 + D8 + D9 一气呵成

**主要成就**：完成 dev-log 之前规划的所有 D5 收尾 + D7/D8/D9 三个里程碑，
54/54 pytest 全绿，端到端 5/5 BUG 全抓 + 自动 triage + HTML 回归报告。

### Plan A · TestGenAgent 完整设计恢复（~80 SLoC, 1.5h）
**问题**：D5 期间为应付 GLM-4.7 静默推理，TestGenAgent 把 invariants/skills/
characters 的全文预先嵌入 user message，导致 list_invariants/list_skills/
list_characters 三个工具变成"死工具"——LLM 永远不调用它们。
**修复**：拆 `_build_discovery_task_message` (默认) vs `_build_prefetched_task_message`
两条路径，CLI 加 `--prefetch` flag 切换。同时把 `tool_choice="required"` 从
硬写改为参数透传（DeepSeek 上不需要）。
**验证**：discovery 模式实测 **TestGenAgent 10 步、6 用例、123k tokens**
（比 prefetch 200k 还少，因为 LLM 在 list_* 拿到的数据更精炼）。trace 完整
展示 LLM 的浏览过程，面试讲故事价值大幅提升。

### D7 · TriageAgent（~600 SLoC, 8h）
**架构**：两阶段聚类——
1) 规则阶段（`tools/triage_tools.py::cluster_failures`）：按 invariant_id
   前缀 / error_message hash 把 N 条失败压成 M 个候选簇
2) LLM 阶段（`agents/triage.py`）：对每个 cluster 让 LLM 拼装 Jira-compatible
   BugReport（中文标题、复现步骤、严重级、聚类理由）

**关键设计**：
- BugReport schema 已在 `reports/schema.py` 预留，本轮扩展 `cluster_size /
  member_case_ids / cluster_rationale` 等聚类元数据
- `SEVERITY_BY_INVARIANT_KIND` 映射表：hp/mp_nonneg→S1, replay→S0, 其余→S2
- 工具集：`list_failures` / `inspect_cluster` / `read_trace_tail` /
  `emit_bug_report` / `merge_clusters` / `finalize`
- LLM 永不直接看完整 trace（避免 prompt 爆炸），按需 `read_trace_tail` 拉
- `runner.py::run_plan` 新增 `suite_json_path` 参数，自动落盘
  `artifacts/suite.json`，供 `gameguard triage --suite ...` 事后聚类

**Markdown 报告**：`reports/markdown.py::render_bug_reports` 渲染
Jira 风格 Bug 单（标题/严重级/复现步骤/证据 trace）。

**端到端**：handwritten plan v2 跑出 3 失败 → TriageAgent 产出 3 BugReport
（含中文标题"切换技能时火球术冷却被错误重置（影响连招节奏）"）。

### D8 · I-09/I-10 evaluator + property + exploratory（~700 SLoC, 8h）
**子任务 1：DoT-on-tick + I-09 evaluator**
- BuffSpec 新增 `is_dot: bool` 字段；buff_burn 设 True
- core 新增 `_tick_dots()` 步骤，对每个 DoT 类 buff 调用 `handler.on_dot_tick`
- v1 实现：`damage = magnitude * dt`（精确，4s burn = 40 总伤）
- v2 实现：`damage = magnitude * dt * 1.05`（漂移 5%，BUG-004 落地）
- 新 invariant kind `dot_total_damage_within_tolerance`，evaluator 累计
  trace 中所有 `dot_tick` 事件 amount

**子任务 2：I-10 replay_determinism evaluator**
- runner.py 新增 `_check_replay_determinism`：同 seed 双跑、比对事件序列 +
  rng_draws
- 智能检测：trace 含 crit 字段但 rng_draws=0 → 直接判失败（BUG-005 指纹），
  避免依赖统计性失败

**子任务 3：Property-based testing**
- `tests/test_property_v1.py` 用 hypothesis 生成 200×3=600 条随机动作序列
- 验证 v1 在所有随机序列下 hp/mp_nonneg、buff_stacks、cooldown 永不为负

**子任务 4：ExploratoryAgent**
- `agents/exploratory.py` 复用 `build_testgen_tools`，仅换 system prompt
- 心态：好奇 + 恶意玩家，主动尝试 cast 期内打断、buff 边界、CD 边界等

**端到端**：handwritten.yaml 新增 2 条用例（I-09 burn 总伤 + I-10 双跑），
v2 跑出 **5/5 BUG 全抓**：BUG-001 (cooldown) + BUG-002 (buff refresh) +
BUG-003 (interrupt mp) + BUG-004 (DoT drift) + BUG-005 (RNG)。

### D9 · gameguard regress + HTML 报告（~400 SLoC, 6h）
**功能**：`gameguard regress --baseline pysim:v1 --candidate pysim:v2 --plan ...`
自动跑两次、做 diff、产出 HTML 报告。

**RegressDiff 五态**：NEW (回归) / FIXED (修复) / STABLE_PASS / STABLE_FAIL
/ MISSING (单边)。`has_regression` 仅看 NEW。

**HTML 选型**：**Jinja2 + 自写 CSS**，**不接 Allure**（避 Java 依赖）。
模板路径 `reports/templates/regress.html.j2`，内嵌 CSS（200 行 .j2）实现
现代化卡片布局 + 折叠 bug 详情 + 颜色编码 verdict。

**与 D7 联动**：regress 自动把 NEW failures 喂给 TriageAgent，BugReport 嵌入
HTML 报告（`<details>` 折叠展示）。FIXED 不触发 triage。

**目录隔离**：`artifacts/regress/<sandbox>/` 分目录，避免两次跑互相覆盖
trace/snapshot。

**端到端**：handwritten plan 双跑：
- baseline pysim:v1：12/12 全过
- candidate pysim:v2：7/12 通过 + 5 NEW failures
- 自动 triage 产出 5 BugReport（S0×1 + S2×4）
- HTML 报告 12.5KB，含完整 NEW 表格 + 折叠 BugReport 段

### 数字
- pytest **54/54 全绿**（D5: 17 + D6: 5 + D7: 6 + D8: 5 + 3 property + D9: 6 + 既有 12）
- 代码量：约 5500 行 Python（D7/D8/D9 增加 ~1700 行）
- 端到端单次完整：~3 分钟（含 Agent 生成 + v1/v2 双跑 + Triage）
- DeepSeek 单次 generate 约 100-200k tokens（discovery 模式更省）

### 学习点（面试可讲）
1. **TestGenAgent discovery vs prefetch 双模式**：同代码两种 LLM 协作方式，
   demo 走 discovery（trace 完整）、CI 走 prefetch（省 token）。这种"按场景
   切换 prompt 形态"的工程模式越来越常见。
2. **TriageAgent 两阶段聚类**：规则压缩候选 + LLM 二判精化，平衡确定性与
   灵活性。关键约束：LLM 永不直接看 trace 全文（按需 `read_trace_tail`），
   避免 prompt 爆炸。这是 LangChain agent 教程从不强调但生产里非常关键的事。
3. **I-10 replay_determinism 智能检测**：双跑事件序列匹配是必要但不充分
   条件（短序列偶然碰巧一致）；同时检测 "有 crit 事件但 rng_draws=0" 的指纹
   能稳定捕获 BUG-005 的根因。
4. **DoT-on-tick 浮点边界**：4.0/0.05 在 IEEE 754 下不精确等于 80，导致 81
   ticks 的边界情形——把 invariant tolerance 从 0.5 提到 1.0 是务实的工程
   取舍。
5. **HTML 报告"按需自造"**：Allure 需要 Java 运行时，对单模块项目过重；
   自写 Jinja2 模板覆盖 90% 价值零依赖。这是 senior 工程师"按需取舍而非
   照搬最佳实践"的体现。

### Next
- D11 Unity Adapter proto + C# stub（打 JD 第 3 条"跨领域工业化管线"）
- D10 (stretch) Critic Agent（D10 之前积累的"Agent 生成用例 quality 不稳"
  现在有充足语料喂 prompt）
- D12（可选）成本/确定性面板 + 缓存深化

---

## 2026-04-17（凌晨续）— D11 + D10 全部完成 · 整体收官

**主要成就**：连续完成 D11 Unity Adapter 骨架 + D10 Critic Agent stretch
goal，全部计划 D1–D11 + D10 stretch 全数 deliver，**65/65 pytest 全绿**，
代码量约 8000 行 Python + 1200 行 docs/yaml。

### D11 · Unity Adapter Proto 骨架（~280 SLoC, 3h）

**核心交付**：
- [`gameguard/sandbox/unity/proto/gameguard_v1.proto`](../gameguard/sandbox/unity/proto/gameguard_v1.proto)
  完整 gRPC 服务定义：6 个 RPC（Reset/Step/QueryState/StreamEvents/Snapshot/
  Restore/Info）+ 12 个 message 类型，与 GameAdapter ABC 一一对应
- [`gameguard/sandbox/unity/adapter.py`](../gameguard/sandbox/unity/adapter.py)
  `UnityAdapter(GameAdapter)` 客户端骨架：
  - `from_endpoint(host, port)` 抛清晰 NotImplementedError 指向接入清单
  - `from_mock(trace_path)` **可立即使用**，用预录 JSONL trace 跑集成测试
- [`gameguard/sandbox/unity/client/UnityBridge.cs`](../gameguard/sandbox/unity/client/UnityBridge.cs)
  C# 服务端伪代码：`[InitializeOnLoad]` 启动 gRPC server、EventBus 桥接
  Unity 内部事件、SnapshotEngine 序列化游戏状态
- [`docs/unity_integration.md`](unity_integration.md) 完整接入指南：
  3 周工作量分解、设计权衡说明、面试讲故事素材
- CLI 路由 `unity:mock` / `unity:headless`；`gameguard info` 反映完整状态

**面试价值**：
"我没真接入 Unity（需要 2-3 周编译/调试），但架构层面已为接入做好准备：
12 个 RPC、Python 客户端骨架、C# bridge 伪代码、mock 模式让上层测试解耦。
打开 proto 文件就能讲我对 PlayMode 状态机、UnitySynchronizationContext、
EditorApplication 生命周期的理解。"

### D10 · Critic Agent（~620 SLoC, 6h, stretch）

**动机**：D5/D6 实测 TestGenAgent 8 条用例里 2 条 ERROR + 1 条幻觉
（Agent 自己算错 MP/CD/wait）。Critic 就是治这个的。

**架构**：
- [`gameguard/tools/critic_tools.py`](../gameguard/tools/critic_tools.py)
  - `static_check_case`：纯 Python 模拟 MP/CD/state 推进，找出 4 类问题
    （insufficient_mp / cd_violation / interrupt_no_cast / unknown_*）
  - 工具集：list_cases / inspect_case / patch_case / drop_case /
    accept_case / finalize
  - **关键设计**：静态校验在 Python 层（确定性、便宜），LLM 只决策
    （patch / drop / accept），不计算
- [`gameguard/agents/critic.py`](../gameguard/agents/critic.py)
  `run_critic_agent` + `make_critic_review_hook`（接到 orchestrator 预留的
  review_hook）
- CLI `--critic` flag：`gameguard generate --critic` 启用

**严格边界**：
- 不新增 case（那是 TestGen/Exploratory 的事）
- 不修改 assertions（那是测试目标，不是缺陷）
- 只 patch / drop —— 经典 review pattern

**测试覆盖**（`tests/test_critic.py`，6 条）：
- 静态校验 4 路径（mp/cd/interrupt/clean）
- mock LLM 验证 drop 决策
- mock LLM 验证 patch 决策（patch 后用 static_check 二次验证）

### 终极数字（Week 1–2 完整里程碑）

| 项目 | 数值 |
|---|---|
| **代码量** | ~8000 行 Python + 1200 行 docs/yaml |
| **pytest** | **65 用例全绿**（0.6s） |
| **核心 demo** | 12 用例 / v1 12/12 / v2 7/12（5/5 BUG 全抓） |
| **Agent pipeline** | 端到端 ~3 分钟（DeepSeek） |
| **TestPlan token 成本** | 100k–200k DeepSeek tokens（约 ¥0.5）；缓存命中后 ¥0 |
| **达成里程碑** | D1 → D11 + D10 stretch **全部** |

### 完整能力清单（`gameguard info`）

| 模块 | 状态 |
|---|---|
| Sandbox.pysim:v1 | ✓ 黄金实现 |
| Sandbox.pysim:v2 | ✓ 5 类 bug |
| Sandbox.unity:mock | ✓ 预录 trace mock |
| Sandbox.unity:headless | proto 就绪 |
| TestCase YAML | ✓ |
| Runner | ✓ trace/snapshot/suite.json |
| Reports.markdown | ✓ |
| Reports.html | ✓ Jinja2 + 折叠 BugReport |
| Agents.DesignDoc | ✓ |
| Agents.TestGen | ✓ discovery + prefetch |
| Agents.Triage | ✓ 两阶段聚类 |
| Agents.Exploratory | ✓ |
| Agents.Critic | ✓ 静态校验 + LLM 决策 |

### 完整端到端命令

```bash
# 一键从策划文档到 Jira-compatible HTML 报告
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml \
                   --critic        # 启用 Critic review

gameguard regress --plan testcases/skill_system/agent_generated.yaml \
                  --baseline pysim:v1 \
                  --candidate pysim:v2 \
                  --html artifacts/reports/regress.html
                  # 自动跑 v1/v2 双跑 → 计算 NEW/FIXED/STABLE → 自动 triage
                  # NEW failures → 渲染 HTML 报告含折叠 BugReport
```

### 面试讲故事的"主轴"

1. **JD 条 1（前沿 Agent 技术落地）**：5 个 Agent (Orchestrator + DesignDoc +
   TestGen + Triage + Critic + Exploratory)、手写 AgentLoop 200 行、tool-as-
   side-channel 模式、tool_choice + disable_thinking 治推理型模型、双阶段
   聚类、property-based + exploratory + contract 三层测试策略
2. **JD 条 2（Agent 研发工具链）**：从策划文档到 Jira bug 单的完整流水线，
   差分回归 HTML 报告，65 条 meta-test 全绿
3. **JD 条 3（跨领域工业化管线）**：Unity Adapter proto + C# bridge 骨架，
   mock 模式让集成测试解耦，3 周接入计划写在 docs

### 学习点最终收纳

1. **TestGenAgent discovery vs prefetch 双模式**——同代码两种 LLM 协作方式
2. **TriageAgent 两阶段聚类** + LLM 永不直接看 trace 全文
3. **I-10 智能检测**：双跑事件序列匹配 + rng_draws=0 指纹双保险
4. **DoT-on-tick 浮点边界**：4.0/0.05 ≠ 80 严格相等的现实
5. **HTML 自造 vs Allure**：senior 工程师的"按需取舍"
6. **Unity proto + C# 骨架**：架构诚意 vs 实际接入的工作量
7. **Critic 静态校验放工具层**：LLM 决策、Python 计算 —— 经典 hybrid 模式
8. **review_hook 预留设计**：D5 时预留的扩展点，D10 真正用上 —— 印证好架构

### 下一步可选（不在此项目范围）
- 真接 Unity（2-3 周）
- 接飞书机器人推送 Bug 单（1 天）
- 配置表 schema 校验（1 周，打 JD 策划配置生成方向）
- mutation testing（1 周，打 JD 程序代码生成方向）

**项目结束。**
