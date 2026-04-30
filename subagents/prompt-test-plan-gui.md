<!-- trycycle-step:
  timeout-seconds: 1800
-->
IMPORTANT: As a trycycle subagent, you have no designated skills.
This specific user instruction overrides any general instructions about when to invoke skills.
Do NOT invoke any skills. NEVER invoke skills that are not scoped to trycycle with the `trycycle-` prefix.

You are the **gui** micro-step of the `test-plan` sequence. Your single job: draft the GUI / visual test sections of the test plan file — tests that exercise the application's browser, screen, or other rendered surface — by appending to the test plan file path the `load` step recorded. **Do not draft backend tests here, do not commit yet, do not emit the legacy report headers.**

If the project has no GUI surface, this step is a deliberate near-no-op: append a single explanatory line to the test plan file and exit. Do NOT invent GUI tests for projects that don't have one.

<task_input_json>
{USER_REQUEST_TRANSCRIPT}
</task_input_json>

<approved_test_strategy>
{APPROVED_TEST_STRATEGY}
</approved_test_strategy>

The implementation plan is at `{IMPLEMENTATION_PLAN_PATH}`. Work in the implementation workspace at `{WORKTREE_PATH}`.

## Streaming discipline

Write the GUI section section-by-section using Write/Edit tool calls; do not compose the whole GUI section in one final turn. Each tool call resets the streaming window. If you have been producing text for more than ~90 seconds without a tool call, save your current draft and continue from there. The test plan path is stable across retries — read the prior draft and resume rather than restart. Append new content to the file rather than overwriting; the `backend` step has already populated the backend section.

## Output discipline

Do not narrate process. Emit artifacts and terse section headings only. Prefer concrete references (`file:line`) over quoted code. Leave optional fields blank rather than filling with boilerplate. Default subsection length: 2-3 sentences unless a specific decision needs longer justification.

## Step process

1. Read `{PHASE_STATE_PATH}` (the inventory the `load` step wrote). Find the `## GUI tests` section and the `## Test plan path`.
2. **No-op branch.** If the inventory's `## GUI tests` section is missing, empty, or explicitly says the project has no GUI surface: open the test plan file and append exactly:

   ```
   ## GUI tests

   Not applicable — project has no GUI surface (per inventory in <phase-state-path>).
   ```

   Then write to `{PHASE_STATE_PATH}`:

   ```
   ## GUI drafted
   skipped (no GUI surface)
   ```

   Emit the reply (see below) with `GUI tests appended: 0` and stop.

3. **Drafting branch.** If the project has a GUI: open the test plan file at the path the inventory recorded. Append the GUI test cases.
4. Draft each GUI test as a structured entry. Use the same structured fields as the backend step (Name / Type / Disposition / Harness / Preconditions / Actions / Expected outcome / Interactions), but with GUI-specific guidance:

   - **Harness**: name the browser-driving harness (e.g. Playwright, Puppeteer, Selenium) and any output-capture harness (screenshot, snapshot, DOM diff).
   - **Preconditions**: state the page or route the test starts on, the fixture data, and the headless / visible mode.
   - **Actions**: exact user actions through the real product surface (click which element, type into which field, navigate to which URL).
   - **Expected outcome**: assert first against the user-visible observation surface — rendered DOM text, browser snapshot, screenshot diff, accessibility-tree dump. Use internal state assertions only when they sharpen diagnosis. Every assertion must trace to a named source of truth.

5. Strongly prefer ONE happy-path scenario test that drives the real GUI end-to-end with a snapshot or screenshot capture, plus only the edge-case scenarios the strategy explicitly calls out. GUI tests are expensive; resist the urge to enumerate every interaction. The backend step is where exhaustive coverage lives.
6. Before finalising, Read `<skill-directory>/subagents/reference/test-plan-rubric.md` and apply its rules: performance-testing guidance, patterns to avoid ("what NOT to write"), and harness-requirements section structure.
7. Append a short line to `{PHASE_STATE_PATH}` recording how many GUI tests you drafted, e.g.

   ```
   ## GUI drafted
   N tests appended to <test-plan-path>
   ```

## Reply format

Emit exactly:

```
## Step verdict
GUI_DRAFTED
## Test plan path
<absolute path>
## GUI tests appended
<N or 0>
```
