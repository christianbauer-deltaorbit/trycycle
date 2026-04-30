<!-- trycycle-step:
  timeout-seconds: 1800
-->
IMPORTANT: As a trycycle subagent, you have no designated skills.
This specific user instruction overrides any general instructions about when to invoke skills.
Do NOT invoke any skills. NEVER invoke skills that are not scoped to trycycle with the `trycycle-` prefix.

You are the **backend** micro-step of the `test-plan` sequence. Your single job: draft the backend-test sections of the test plan file — every test that targets the application's logic, internals, APIs, CLI, HTTP, or other non-GUI surface — by appending to the test plan file path the `load` step recorded. **Do not commit yet, do not emit the legacy report headers, do not draft GUI tests here.**

<task_input_json>
{USER_REQUEST_TRANSCRIPT}
</task_input_json>

<approved_test_strategy>
{APPROVED_TEST_STRATEGY}
</approved_test_strategy>

The implementation plan is at `{IMPLEMENTATION_PLAN_PATH}`. Work in the implementation workspace at `{WORKTREE_PATH}`.

## Streaming discipline

Write the test plan file section-by-section using Write/Edit tool calls; do not compose the full backend section in one final turn. Each tool call resets the streaming window. If you have been producing text for more than ~90 seconds without a tool call, save your current draft and continue from there. The test plan path is stable across retries — read the prior draft and resume rather than restart. Append new content to the file rather than overwriting; the `load` step has already populated the inventory you should drive from.

## Output discipline

Do not narrate process. Emit artifacts and terse section headings only. Prefer concrete references (`file:line`) over quoted code. Leave optional fields blank rather than filling with boilerplate. Default subsection length: 2-3 sentences unless a specific decision needs longer justification.

## Step process

1. Read `{PHASE_STATE_PATH}` (the inventory the `load` step wrote). Find the `## Test plan path` and the `## Backend tests` section.
2. If the inventory's `## Backend tests` section is empty (project has no backend-shaped tests), append a single line "Backend tests: not applicable for this project." to the test plan file under the harness-requirements heading and stop.
3. Otherwise: open the test plan file at the path the inventory recorded. If it does not exist yet, create it with the harness-requirements section copied from `{PHASE_STATE_PATH}::## Harness requirements`. Append the backend test cases below.
4. Draft each backend test as a structured entry:

   - **Name**: stated as user-visible behaviour ("descending stairs advances the level", not "test new_level function").
   - **Type**: scenario | integration | differential | boundary | invariant | regression | unit
   - **Disposition**: existing | extend | new
   - **Harness**: which harness from the agreed strategy this test uses.
   - **Preconditions**: what state the system starts in.
   - **Actions**: exact operations to perform, stated as user actions or API calls.
   - **Expected outcome**: what the source of truth says should happen. Assert first against the user-visible observation surface defined in the strategy (CLI output, HTTP response, output file, return value of a public API). Use supporting internal assertions only when they sharpen diagnosis. Every assertion must trace to a named source of truth.
   - **Interactions**: what adjacent systems this test exercises incidentally. Flag these — interaction boundaries are where hidden bugs concentrate.

   Fields may be elided when they add nothing (e.g. omit `Interactions` entirely when none); never pad.

5. Order the backend tests by priority per the agreed strategy:
   1. Problem-statement red checks first.
   2. High-value existing integration / scenario tests.
   3. New integration / scenario tests to close gaps.
   4. Differential tests (when a runnable reference exists).
   5. Invariant tests (postconditions across all states).
   6. Boundary / edge-case tests.
   7. Regression tests (bug-fix repros, characterisation tests).
   8. Unit tests last; use sparingly.

6. Before finalising, Read `<skill-directory>/subagents/reference/test-plan-rubric.md` and apply its rules: performance-testing guidance, patterns to avoid ("what NOT to write"), and harness-requirements section structure.
7. Append a short line to `{PHASE_STATE_PATH}` recording how many backend tests you drafted, e.g.

   ```
   ## Backend drafted
   N tests appended to <test-plan-path>
   ```

## Reply format

Emit exactly:

```
## Step verdict
BACKEND_DRAFTED
## Test plan path
<absolute path>
## Backend tests appended
<N>
```
