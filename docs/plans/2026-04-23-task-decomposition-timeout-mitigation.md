# Task-decomposition-based timeout mitigation

> **For agentic workers:** REQUIRED: Use trycycle-executing to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a decomposition mechanism to trycycle's long phases so that `planning-initial` and `executing` no longer exceed the Anthropic streaming-API's per-turn limits. Split each long phase into a sequence of short subagent dispatches with state handoff via a scratch file; keep the single-dispatch mode available as `--single-shot` for short tasks.

**Evidence base:** `docs/tc-timeout-evidence.tar.gz`. Four consecutive `planning-initial` failures — two native-Agent, two fallback-runner. Stream-idle timeout reproduced in the fallback-runner subprocess, confirming the failure is at the Anthropic streaming API layer, below both Claude Code's Agent tool and our subprocess boundary. Prompt-level heartbeat prose alone is insufficient; 63 tool_uses / 359 s still died.

**Architecture:**
- `orchestrator/run_phase.py` gains a `run-sequence` subcommand that accepts a list of step names and dispatches them in order through the existing subagent runner. Between steps, it pre-seeds a scratch file path as `{PHASE_STATE_PATH}`; subagents read and extend it. Coordinator never reads the scratch body.
- Each micro-step has its own prompt template under `subagents/prompt-<phase>-<step>.md`. Bounded wall clock (60-180 s target) per step.
- `--single-shot TEMPLATE` flag restores legacy single-dispatch behaviour.
- `executing` has variable task count → micro-step templates emit a `[[TRYCYCLE_SEQUENCE_DONE]]` sentinel into the scratch file when the task list is fully processed; `run-sequence` short-circuits remaining steps when it sees the sentinel.

**Tech Stack:** Python 3, existing `subagent_runner.py`, existing prompt builder, existing heartbeat validator.

---

## Design decisions

### Why a new `run-sequence` verb rather than overloading `run`

`run` is a clean one-shot dispatcher: prepare prompt → spawn subagent → return result. Decomposition is sequential state: prepare → dispatch → check status → prepare next → dispatch next. Different control flow. Separating verbs keeps the simple case simple (current `run` users aren't affected) and the new case explicit.

### Why a file-based scratch rather than stdout chaining

Each subagent's reply must survive the streaming-API deadline. A file handoff has three advantages over chaining stdout: (1) partial writes survive a killed stream — the next step can read whatever was committed; (2) subagents can modify state in place (append a row, tick a checkbox) rather than rewriting the whole body from their context; (3) debugging a failed sequence is trivial — inspect the scratch file.

### Why `{PHASE_STATE_PATH}` as a placeholder, not a hardcoded path

The scratch file lives under `artifacts_dir`, which the coordinator creates per-dispatch. Hardcoding would couple subagents to a path scheme. Threading it through the prompt-builder placeholder machinery is consistent with how we already bind `{WORKTREE_PATH}`, `{IMPLEMENTATION_PLAN_PATH}`, etc.

### Why a sentinel string for early-terminate rather than reply-parsing

`executing` has variable task count. The coordinator can't know in advance how many `execute-next-task` steps to schedule. Options: (a) parse every reply for a completion marker; (b) have the subagent write a sentinel to the scratch file. (b) wins because: scratch is the durable state anyway; the coordinator can read one byte sequence from disk cheaply; and it decouples reply formatting from control flow.

Sentinel chosen: literal string `[[TRYCYCLE_SEQUENCE_DONE]]` anywhere in the scratch file. Uncommon enough to not false-positive; documented in the micro-step templates.

### Why decompose planning-initial into survey → scaffold → detail → commit

Natural ordering of a plan document's construction. Survey understands the codebase without writing a plan. Scaffold produces the plan skeleton (goal, architecture, task headings). Detail fills each task's steps. Commit produces the final git commit and the report the orchestrator expects. Each step targets 60-180 s.

### Why decompose executing into load → execute_next_task (×N) → finalize

Load reads both plans, builds the task checklist, emits an empty `phase-state.md` with one line per task. Each `execute_next_task` step picks the next `[ ]` task, implements with TDD red/green/refactor, commits, ticks it to `[x]`. When no `[ ]` remain, it writes the sentinel. Finalize runs the whole test suite and emits the implementation-summary markdown the orchestrator expects.

### Why `--single-shot TEMPLATE` rather than dropping the verb split

Some tasks are genuinely short (a typo fix, a one-line config change). The extra overhead of 5+ dispatches isn't warranted. `--single-shot` lets the orchestrator fall back to a single dispatch against an arbitrary template without exiting the `run-sequence` code path.

## File structure

- Add: `orchestrator/run_phase.py::_command_run_sequence` and supporting helpers.
- Add: `subagents/prompt-planning-initial-survey.md`
- Add: `subagents/prompt-planning-initial-scaffold.md`
- Add: `subagents/prompt-planning-initial-detail.md`
- Add: `subagents/prompt-planning-initial-commit.md`
- Add: `subagents/prompt-executing-load.md`
- Add: `subagents/prompt-executing-next-task.md`
- Add: `subagents/prompt-executing-finalize.md`
- Modify: `SKILL.md` steps 6 (planning-initial) and 9 (executing) to call `run-sequence` by default; document `--single-shot`.
- Add: `tests/test_run_phase_sequence.py`
- Add: `tests/test_integration_decomposition.py`

## Strategy gate

- **Is this the right problem?** Yes. Evidence tarball shows the failure is below the subprocess boundary; heartbeat prose alone can't prevent it; only per-dispatch work reduction can.
- **Is the architecture right?** Yes — scratch-file handoff preserves partial progress across killed streams, and fixed-list-with-sentinel keeps the coordinator logic minimal while supporting variable-count phases like executing.
- **Could we do less?** Skip executing decomposition. But the evidence shows executing is the second-biggest timeout risk (180-min orchestrator timeout exists for a reason) — leaving it single-shot would invalidate the pattern for the most common heavy phase.
- **Could we do more?** Add rate-limit retry, automatic resume on stream-idle, adaptive step duration. Explicitly out of scope per the user's instructions.

---

### Task 1: `run-sequence` verb + `--single-shot` + scratch-file handoff

**Files:**
- Modify: `orchestrator/run_phase.py`

- [ ] **Step 1:** Add `SEQUENCE_DONE_SENTINEL = "[[TRYCYCLE_SEQUENCE_DONE]]"` module constant.

- [ ] **Step 2:** New `_command_run_sequence(args)` function. Accepts `--phase`, `--steps STEPS` (comma-separated), `--template-dir DIR` (defaults to `subagents/`), `--workdir`, `--backend`, shared `--set`/`--set-file`/`--transcript-placeholder`, `--require-nonempty-tag`, `--ignore-tag-for-placeholders`, `--artifacts-dir`, `--single-shot TEMPLATE_PATH`, `--max-sequence-seconds SECONDS` (overall deadline; default 600).

- [ ] **Step 3:** If `--single-shot` passed, treat as a 1-step sequence using the provided template. Otherwise derive per-step template paths as `<template-dir>/prompt-<phase>-<step>.md`.

- [ ] **Step 4:** Seed a scratch file at `<artifacts_dir>/scratch/phase-state.md` (empty, created if absent). Add `PHASE_STATE_PATH` to every micro-step dispatch's `--set`.

- [ ] **Step 5:** Iterate steps. For each step: prepare prompt (via existing `_prepare_phase` path, but targeted at the per-step template), dispatch via subagent_runner (single-session, same backend), capture dispatch status. If status ≠ "ok", stop sequence, emit final payload with preserved scratch. If scratch contains `SEQUENCE_DONE_SENTINEL`, short-circuit remaining steps.

- [ ] **Step 6:** Final payload aggregates per-step results under `steps: [{step, status, dispatch: {...}}, ...]` and echoes the final step's reply as the phase reply.

- [ ] **Step 7:** Register subparser `run-sequence` with argparse; wire `set_defaults(func=_command_run_sequence)`.

### Task 2: planning-initial micro-step templates

**Files:**
- Add: `subagents/prompt-planning-initial-survey.md`
- Add: `subagents/prompt-planning-initial-scaffold.md`
- Add: `subagents/prompt-planning-initial-detail.md`
- Add: `subagents/prompt-planning-initial-commit.md`

- [ ] **Step 1:** Each template carries the same `## Streaming discipline` block (short: "You have 120 s. Write early via Write/Edit. If you feel slow, stop and hand off via `PHASE_STATE_PATH`.") so heartbeat validation passes.
- [ ] **Step 2:** Each template takes `{USER_REQUEST_TRANSCRIPT}` wrapped in `<task_input_json>...`, plus `{WORKTREE_PATH}` and `{PHASE_STATE_PATH}`.
- [ ] **Step 3:** Survey reads the worktree, writes a terse inventory (relevant files, contracts, likely pitfalls) to `{PHASE_STATE_PATH}`. Returns `## Step verdict: SURVEYED`.
- [ ] **Step 4:** Scaffold reads phase-state, writes a plan skeleton (goal, architecture, file structure, ordered task headings only) into `docs/plans/YYYY-MM-DD-<slug>.md` AND appends "## Scaffold complete" to phase-state.
- [ ] **Step 5:** Detail reads phase-state + plan path, fills each task with step-by-step actions.
- [ ] **Step 6:** Commit runs `git add` + commits the plan file; emits the existing `## Plan verdict / ## Plan path / ## Commit / ## Changed files` report the coordinator expects. Appends `SEQUENCE_DONE_SENTINEL` to phase-state.

### Task 3: run-sequence unit tests

**Files:**
- Add: `tests/test_run_phase_sequence.py`

- [ ] **Step 1:** Happy-path test: fake binary that writes known text to phase-state; 3-step sequence completes; final payload has `status: "ok"` and `steps[i].status: "ok"` for all i.
- [ ] **Step 2:** Mid-sequence failure test: step 2's fake binary exits non-zero. Sequence stops at step 2; payload reports step 2 status and preserves phase-state content.
- [ ] **Step 3:** Single-shot test: `--single-shot <template>` path dispatches exactly once against the provided template; scratch created and PHASE_STATE_PATH injected.
- [ ] **Step 4:** Sentinel test: step 1's fake binary writes the sentinel to phase-state; step 2 (which would have failed) is skipped; final status is "ok".
- [ ] **Step 5:** Scratch-lifecycle test: scratch file created before step 1; survives all steps; visible under `<artifacts_dir>/scratch/phase-state.md` after completion.

### Task 4: planning-initial commit (infra + templates + tests)

- [ ] Single commit. Tests green before committing.

### Task 5: executing micro-step templates

**Files:**
- Add: `subagents/prompt-executing-load.md`
- Add: `subagents/prompt-executing-next-task.md`
- Add: `subagents/prompt-executing-finalize.md`

- [ ] **Step 1:** `load` reads `{IMPLEMENTATION_PLAN_PATH}` and `{TEST_PLAN_PATH}`, emits a `## Task checklist` section into phase-state with one `[ ] <task name>` per plan task.
- [ ] **Step 2:** `next-task` reads phase-state, finds the first `[ ]`. If none, writes `SEQUENCE_DONE_SENTINEL` and returns. Otherwise: implements that task via TDD cycle, commits, ticks it to `[x]`, returns "## Task completed: <name>".
- [ ] **Step 3:** `finalize` reads phase-state, asserts all `[x]`, runs the full test suite (per the existing executing subskill's guidance), returns the implementation-summary / verification-results / commit / changed-files report.

### Task 6: run-sequence sentinel handling

**Files:**
- Modify: `orchestrator/run_phase.py`

- [ ] Already covered in Task 1 Step 5; verify by the sentinel test in Task 3 Step 4.

### Task 7: executing tests

**Files:**
- Modify: `tests/test_run_phase_sequence.py`

- [ ] **Step 1:** Test: a "load + execute × 3 + finalize" sequence where `execute` step 2 writes the sentinel → steps 3 and finalize still execute (sentinel short-circuits only steps that haven't started yet? wait — semantics check in Task 1 Step 5: sentinel short-circuits REMAINING steps. For executing, finalize must still run. Refine: sentinel short-circuits any step whose name matches a pattern indicating it's the repeatable body. Simplest: sentinel short-circuits ONLY the specific step name "execute-next-task"; finalize still runs. Update Task 1 Step 5 to match.)

Revised semantics:
- `--steps load,next-task,next-task,next-task,finalize` → if sentinel appears after `load` or any `next-task`, remaining `next-task` entries are skipped but `finalize` still runs. Implementation: maintain a set `--short-circuit-on-sentinel STEP_NAME` (defaults to `next-task`). Only steps in that set are skipped when sentinel seen.

### Task 8: executing commit

- [ ] Single commit for executing decomposition.

### Task 9: SKILL.md updates

**Files:**
- Modify: `SKILL.md`

- [ ] **Step 1:** Rewrite step 6 (plan) to invoke `run-sequence --phase planning-initial --steps survey,scaffold,detail,commit`. Keep the report-shape expectations (## Plan path, ## Commit, ## Changed files) unchanged.
- [ ] **Step 2:** Rewrite step 9 (execute) to invoke `run-sequence --phase executing --steps load,<next-task>×N,finalize --short-circuit-on-sentinel next-task` where N is a practical upper bound (default 30).
- [ ] **Step 3:** Add a short subsection under "Phase wrapper helper" documenting `--single-shot` for short tasks.

### Task 10: integration test

**Files:**
- Add: `tests/test_integration_decomposition.py`

- [ ] A 3-step sequence against real `claude` CLI dispatching trivial prompts. Skippable if `claude` not on PATH. Assert final reply contains a marker string produced by the last step.

### Task 11: full regression + finish

- [ ] `/root/.local/bin/pytest tests/` — 103 existing + new = green.
- [ ] Commit SKILL.md + integration test + any docs.
- [ ] Push feature branch; ff-merge to main; push main.

---

## Testing strategy

Per the user's spec (evidence README): regression is the existing `tests/` directory (103 tests). New tests in `tests/test_run_phase_sequence.py` cover the new verb and sentinel semantics using fake binaries (no subprocess mocking of the real model). Integration test spawns real claude if available.

Acceptance against the meshing-module task cannot be reproduced in-sandbox (that repo isn't accessible here). The user runs the before/after comparison on their side; I produce sandbox evidence of the machinery working on a synthetic 3-step task plus full regression green.

## Verification

1. `/root/.local/bin/pytest tests/` passes.
2. `python3 orchestrator/run_phase.py run-sequence --phase planning-initial --steps survey,scaffold,detail,commit --template-dir subagents/ --workdir /tmp --backend claude --dry-run` prints a 4-step dry-run payload under root without `--dangerously-skip-permissions`.
3. (User-side) `run-sequence` against the meshing-module task completes all steps under the 10-minute wall-clock cap with no stream-idle failure.
