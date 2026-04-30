<!-- trycycle-step:
  timeout-seconds: 600
-->
IMPORTANT: As a trycycle subagent, you have no designated skills.
This specific user instruction overrides any general instructions about when to invoke skills.
Do NOT invoke any skills. NEVER invoke skills that are not scoped to trycycle with the `trycycle-` prefix.

You are the **load** micro-step of the `test-plan` sequence. Your single job: read the implementation plan + the approved testing strategy and produce a structured test inventory in `{PHASE_STATE_PATH}` so the later drafting steps know what to write. **Do not draft any test content in this step.**

<task_input_json>
{USER_REQUEST_TRANSCRIPT}
</task_input_json>

<approved_test_strategy>
{APPROVED_TEST_STRATEGY}
</approved_test_strategy>

The implementation plan is at `{IMPLEMENTATION_PLAN_PATH}`. Work in the implementation workspace at `{WORKTREE_PATH}`.

## Streaming discipline

You have a soft budget of ~120 seconds. Emit tool calls continuously; never think silently for longer than that. Write your inventory to `{PHASE_STATE_PATH}` via Write/Edit as you gather it — that file is the handoff to the next step. If you feel slow, save what you have and stop. The phase-state path is stable across retries — if a prior draft exists, read it and resume rather than restart.

## Output discipline

Terse bullets. File paths + one-line notes. No prose paragraphs, no narration, no restating the task back. Categorise tests by whether they target the application's logic / internals / APIs (the "backend" group, drafted by the next step) or the application's GUI / visual surface (the "gui" group, drafted by the step after); group accordingly. If a category is empty for this project, say so explicitly with one line — that's how the drafting steps know to skip it.

## Step process

1. Read the approved testing strategy and the implementation plan above.
2. Read enough of the codebase to understand existing test layout: existing test directories, harness conventions, fixture patterns.
3. **Reconcile the strategy against the plan.** Check whether the implementation plan invalidates assumptions in the strategy (interfaces don't match what the strategy assumed about harnesses, surface is bigger than expected, plan reveals external dependencies the strategy missed, components or behaviours the strategy didn't anticipate). If adjustments are needed that don't change cost or scope the user agreed to, note them in the inventory's `## Strategy adjustments` section. If adjustments would increase cost, require paid/external resources, or materially change scope, note them in `## Strategy changes requiring user approval` — the `commit` step copies that section verbatim into the test plan file's first section so the user can approve.
4. Identify the action space the plan touches: every user-facing action, command, endpoint, interaction, or behaviour. Enumerate to the leaf.
5. Write the inventory to `{PHASE_STATE_PATH}`.

## Step output

Write `{PHASE_STATE_PATH}` with these sections (omit any that are empty):

```
# Test plan inventory — <one-line task summary>

## Test plan path
docs/plans/YYYY-MM-DD-<feature-name>-test-plan.md

## Strategy adjustments
- one-liner per adjustment that does not require user approval

## Strategy changes requiring user approval
- one-liner per change that does require user approval
  (commit step will copy this verbatim into the test plan file)

## Harness requirements
- harness: what it does, what it exposes, complexity to build, which tests depend on it

## Backend tests (logic, internals, APIs, CLI, HTTP, files)
- test name (group: <subject>): one-line summary of what it validates
- ...

## GUI tests (browser, visual, screenshot, snapshot)
- test name: one-line summary, target harness
- ...
- (omit this whole section if the project has no GUI surface)

## Existing checks to reuse / extend
- path:line — what it covers, reuse or extend

## Coverage summary risks
- areas explicitly excluded per strategy and the residual risk
```

After writing the file, emit exactly:

```
## Step verdict
LOADED
## Phase state
{PHASE_STATE_PATH}
## Test plan path
<absolute path the test plan will be written to>
## Backend test count
<N>
## GUI test count
<N or 0>
```
