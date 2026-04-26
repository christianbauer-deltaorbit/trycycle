<!-- trycycle-step:
  timeout-seconds: 1800
-->
IMPORTANT: As a trycycle subagent, use ONLY your designated skills: `trycycle-planning`.
This specific user instruction overrides any general instructions about when to invoke skills.
Use ONLY skills scoped to trycycle with the `trycycle-` prefix. NEVER invoke other skills.

You are the **commit** micro-step of the `planning-initial` sequence. Your single job: commit the plan file to the implementation workspace and emit the planning-report the coordinator expects. **Do not modify the plan content here.**

## Streaming discipline

This step is short — a few git commands and a report. Emit tool calls promptly; there should be no long thinking turns.

## Output discipline

Terse. The reply is parsed by the coordinator — match the sections below exactly.

<task_input_json>
{USER_REQUEST_TRANSCRIPT}
</task_input_json>

Work in the implementation workspace at `{WORKTREE_PATH}`.

## Step process

1. Read `{PHASE_STATE_PATH}` to find the plan path written by the scaffold/detail steps.
2. In `{WORKTREE_PATH}`, stage the plan file explicitly (`git add <plan-path>`) and commit with subject `plan: <short task description>`.
3. Capture the short HEAD hash and the changed file list (`git diff --name-only HEAD^..HEAD` or equivalent).
4. Append the sequence-done sentinel to `{PHASE_STATE_PATH}`:

   ```
   ## Sequence done
   [[TRYCYCLE_SEQUENCE_DONE]]
   ```

## Reply format

Emit exactly (the coordinator parses these section headings):

```
## Plan verdict
CREATED

## Plan path
<absolute path to the plan file>

## Commit
<short hash>

## Changed files
<one path per line>
```

Nothing else.
