# GameGuard

[![CI](https://github.com/Yanjieij/GameGuard-public/actions/workflows/ci.yml/badge.svg)](https://github.com/Yanjieij/GameGuard-public/actions/workflows/ci.yml)
![pytest](https://img.shields.io/badge/pytest-162%20passed%20%2B%203%20skipped-brightgreen)
![python](https://img.shields.io/badge/python-3.11-blue)
[![Evaluation](https://img.shields.io/badge/eval-EVAL.md-0F766E)](EVAL.md)
[![Demo](https://img.shields.io/badge/demo-DEMO.md-1D4ED8)](DEMO.md)

[中文 README](README.md)

GameGuard is an LLM-agent-based automated QA framework for games. It reads
design documents, turns them into executable test plans, runs them in
deterministic sandboxes, and produces regression reports together with
Jira-style bug summaries.

The system is built around a complete loop:

`design doc -> invariants -> test plan -> sandbox execution -> regression diff -> bug report`

The project is designed to demonstrate how AI agents can be integrated into a
real engineering workflow rather than used as isolated prompt demos.

## At A Glance

| Area | What GameGuard provides |
|---|---|
| Planning | Design-doc parsing, invariant extraction, test generation, exploratory generation |
| Execution | Deterministic runs, replay, snapshots, trace capture |
| Regression | Baseline vs candidate diffing, stable/new/fixed failure analysis |
| Triage | Failure clustering and Jira-compatible bug reports |
| Coverage | Skill/combat sandbox, quest/3D sandbox, Unity-facing adapter path |

## Showcase Highlights

- Five-agent pipeline: `DesignDoc`, `TestGen`, `Exploratory`, `Triage`, `Critic`
- Reviewable intermediate artifacts using YAML test plans and structured invariants
- Two sandbox families:
  - `PySim` for skill and combat-system testing
  - `QuestSim` for quest, dialogue, navigation, save/load, and 3D interaction testing
- Deterministic execution with replay, caching, budget control, and trace-based debugging
- Markdown and HTML regression reports
- Unity integration path through a gRPC adapter and mock server

## What Makes It Interesting

GameGuard focuses on a practical QA workflow that is often missing from
AI-agent demos:

- agents generate structured artifacts instead of free-form text
- execution is deterministic and replayable
- regressions are compared across baseline and candidate builds
- failures are clustered into bug reports instead of left as raw logs

This keeps the project closer to a real internal tool than to a one-off
showcase script.

## Architecture

![GameGuard Architecture](docs/architecture.drawio.png)

GameGuard is organized into a small set of stable layers:

| Layer | Responsibility |
|---|---|
| `CLI` | Entry points such as `generate`, `run`, `regress`, `triage`, `info` |
| `Orchestrator` | Coordinates the plan-and-execute pipeline |
| `Agents` | Specialized planning and triage roles built on a shared loop |
| `Tools` | Pydantic-backed structured tool interfaces |
| `Domain` | Pure data models for skills, quests, actions, events, and invariants |
| `Sandbox` | Deterministic execution backends behind a common adapter contract |
| `Reports` | Regression summaries, bug reports, and HTML rendering |
| `LLM Stack` | Provider abstraction, caching, budgeting, and JSONL traces |

The source diagram is [`docs/architecture.drawio`](docs/architecture.drawio), so
the architecture figure can be edited directly in draw.io.

## Why This Design

Most of the design choices are there to keep the system inspectable and
repeatable:

- Planning and execution are separated so expensive LLM work produces durable artifacts.
- Test plans live in YAML so humans and agents can review the same representation.
- Both sandboxes implement the same adapter contract, which keeps upper layers stable.
- Determinism is treated as a feature, so failures can be replayed exactly.

This makes the project feel closer to an internal engineering toolchain than a
single-run agent experiment.

## Demo Scenarios

### Skill-System Regression

Given a skill-design document, the agent pipeline generates test cases and then
compares a baseline sandbox against a candidate sandbox with seeded bugs.

```bash
gameguard generate --doc docs/example_skill_v1.md \
                   --out testcases/skill_system/agent_generated.yaml

gameguard regress --plan testcases/skill_system/handwritten.yaml \
                  --baseline pysim:v1 --candidate pysim:v2 \
                  --html artifacts/reports/regress.html
```

The `pysim:v2` sandbox contains five representative regressions:

| Bug ID | Failure type | Example regression |
|---|---|---|
| `BUG-001` | State pollution | Switching skills clears unrelated cooldowns |
| `BUG-002` | Numeric logic error | Buff refresh stacks magnitude instead of replacing it |
| `BUG-003` | State-machine leak | Interrupted casts fail to refund MP |
| `BUG-004` | Precision bug | DoT damage takes a floating-point accumulation path |
| `BUG-005` | Determinism break | RNG bypasses the sandbox seed |

### Quest and 3D Regression

The `QuestSim` sandbox models branching quest logic, dialogue, navigation,
save/load behavior, and interactive scene progression.

![Harbor Quest DAG](docs/harbor_quest_dag.drawio.png)

```bash
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml \
                  --baseline questsim:v1-harbor --candidate questsim:v2-harbor \
                  --html artifacts/reports/regress_quest.html
```

The candidate quest sandbox contains five seeded regressions across collision,
branch flags, NPC reset behavior, serialization, and navigation setup.

### Invariant Coverage

![Bug Invariant Matrix](docs/bug_invariant_matrix.drawio.png)

Each seeded bug is covered by at least one invariant. The regression flow is
structured so that `v1` passes while `v2` produces targeted new failures, which
makes failure attribution much clearer during demos and reviews.

## Plan-And-Execute Pipeline

![Agent Pipeline](docs/agent_pipeline.drawio.png)

The pipeline is intentionally split into two phases:

- `Plan`: LLM agents read documents, emit invariants, and build test plans.
- `Execute`: the runner evaluates those plans in deterministic sandboxes and produces traces and reports.

This keeps the generated artifacts durable and makes execution cheap, cacheable,
and CI-friendly.

## Core Components

| Component | Purpose |
|---|---|
| `DesignDocAgent` | Extracts machine-checkable invariants from design documents |
| `TestGenAgent` | Converts invariants into executable YAML test plans |
| `ExploratoryAgent` | Generates adversarial or failure-seeking test cases |
| `TriageAgent` | Clusters failures and emits Jira-compatible bug reports |
| `CriticAgent` | Reviews and patches low-quality generated test cases |
| `Runner` | Executes plans deterministically against a chosen sandbox |
| `Reports` | Produces Markdown and HTML summaries for review and demos |

## Evaluation

The repository includes both end-to-end demos and evaluation scripts. Detailed
results live in [EVAL.md](EVAL.md) and the [`evals/`](evals) directory.

High-level takeaways:

- handwritten regression suites achieve full recall on the seeded sandbox bugs
- the planning pipeline is fully runnable from document to report
- provider behavior differs meaningfully on tool-calling workloads
- agent-generated plans are useful, but still materially below curated suites

That gap is intentional to show a realistic engineering story: agent output is
valuable, reviewable, and improvable rather than magically perfect.

## Quick Start

Requirements:

- Python 3.11
- Conda recommended

```bash
conda env create -f environment.yml
conda activate gameguard
pip install -e ".[dev]"
pytest -q
gameguard info
```

Optional extras:

```bash
pip install -e ".[dev,physics]"
pip install -e ".[dev,unity]"
```

To enable LLM-backed planning:

```bash
cp .env.example .env
```

Then configure at least one supported provider in `.env`.

Expected local validation result:

- `162 passed + 3 skipped` on the current test suite

## Useful Commands

```bash
gameguard info
gameguard generate --doc docs/example_skill_v1.md --out testcases/skill_system/agent_generated.yaml
gameguard regress --plan testcases/skill_system/handwritten.yaml --baseline pysim:v1 --candidate pysim:v2 --html artifacts/reports/regress.html
gameguard regress --plan testcases/quest_system/harbor_handwritten.yaml --baseline questsim:v1-harbor --candidate questsim:v2-harbor --html artifacts/reports/regress_quest.html
```

Exit codes:

- `0`: all tests passed
- `1`: failures detected
- `2`: execution error

## Supported Sandboxes

| Sandbox | Purpose |
|---|---|
| `pysim:v1` / `pysim:v2` | Skill-system testing with deterministic combat logic |
| `questsim:v1-harbor` / `questsim:v2-harbor` | Quest, dialogue, navigation, and scene regression |
| `questsim:v1+pybullet` | Quest sandbox with optional physics backend |
| `unity:mock` | Pre-recorded Unity-facing trace adapter |
| `unity:headless` | gRPC path through the mock Unity server |
| `unity:headless+pysim:v2` / `unity:headless+questsim` | Explicit backend selection behind the headless adapter |

## Repository Structure

```text
gameguard/     core package
tests/         automated tests
testcases/     executable YAML plans
docs/          architecture and integration material
evals/         evaluation scripts and result summaries
artifacts/     generated outputs, ignored except placeholders
```

## Use Cases

- Generate test plans from game-design documents
- Run deterministic regression checks across sandbox versions
- Demonstrate bug discovery and triage in interview settings
- Evaluate model behavior on tool-calling-heavy agent workflows
- Prototype Unity-facing QA automation with a gRPC integration path

## Limitations

- This is a portfolio-grade prototype, not a production game-testing platform.
- Visual fidelity, animation quality, and engine-specific rendering correctness are out of scope.
- Agent-generated test quality still trails carefully authored regression suites.

## License

See `LICENSE`.
