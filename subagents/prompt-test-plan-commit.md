<!-- trycycle-step:
  timeout-seconds: 1800
-->
IMPORTANT: As a trycycle subagent, you have no designated skills.
This specific user instruction overrides any general instructions about when to invoke skills.
Do NOT invoke any skills. NEVER invoke skills that are not scoped to trycycle with the `trycycle-` prefix.

You are the **commit** micro-step of the `test-plan` sequence. Your single job: polish the test plan file the `backend` and `gui` steps drafted, prepend the `## Strategy changes requiring user approval` section if the inventory recorded one, write the legacy report headers the coordinator parses, then commit the file on the active worktree branch. **Do not draft new test cases here.**

<task_input_json>
{USER_REQUEST_TRANSCRIPT}
</task_input_json>

<approved_test_strategy>
{APPROVED_TEST_STRATEGY}
</approved_test_strategy>

The implementation plan is at `{IMPLEMENTATION_PLAN_PATH}`. Work in the implementation workspace at `{WORKTREE_PATH}`.

## Streaming discipline

This step is short — read the phase-state, polish the file in place via Edit calls, then a couple of git commands. Emit tool calls promptly; there should be no long thinking turns.

## Output discipline

Terse. The reply is parsed by the coordinator — match the sections below exactly.

## Step process

1. Read `{PHASE_STATE_PATH}` (inventory + drafting markers from prior steps). Read the test plan file at `{PHASE_STATE_PATH}::## Test plan path`.

2. **Strategy-changes pass-through.** If `{PHASE_STATE_PATH}::## Strategy changes requiring user approval` is non-empty, ensure that exact section is the FIRST section of the test plan file. Use Edit to prepend it if it is not already there. The orchestrator presents that section to the user verbatim — do not reword.

3. **Coverage summary.** Append a `## Coverage summary` section at the end of the test plan file capturing:
   - which areas of the action space the test plan covers,
   - which areas are explicitly excluded per the agreed strategy, and
   - what residual risks the exclusions carry.

   Pull the inputs from `{PHASE_STATE_PATH}::## Coverage summary risks`.

4. **Quick consistency pass.** Skim the assembled file. Remove duplicate entries the backend / gui steps may have both included. Confirm the harness-requirements section is intact at the top, that test cases are ordered by priority within their section, and that every assertion traces to a named source of truth (per `<skill-directory>/subagents/reference/test-plan-rubric.md`).

5. **Commit the test plan file.** In `{WORKTREE_PATH}`:
   - `git add <test-plan-path>` (explicit; do not `git add -A`)
   - `git commit -m "test-plan: <short task description>"`
   - Capture the short HEAD hash with `git rev-parse --short HEAD`.
   - Capture the changed file list with `git diff --name-only HEAD^..HEAD`.

6. Append the sequence-done sentinel to `{PHASE_STATE_PATH}`:

   ```
   ## Sequence done
   [[TRYCYCLE_SEQUENCE_DONE]]
   ```

## Reply format

Emit exactly (the coordinator parses these section headings; if a strategy-changes section is present, lead with it verbatim and follow with the rest):

```
## Strategy changes requiring user approval
<verbatim copy of the section, OR omit this whole block when there are none>

## Test plan path
<absolute path to the test plan file>

## Commit
<short hash>

## Changed files
<one path per line>
```

Nothing else.
