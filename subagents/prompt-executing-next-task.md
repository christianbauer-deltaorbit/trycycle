<!-- trycycle-step:
  timeout-seconds: 10800
-->
IMPORTANT: As a trycycle subagent, use ONLY your designated skills: `trycycle-executing`.
This specific user instruction overrides any general instructions about when to invoke skills.
Use ONLY skills scoped to trycycle with the `trycycle-` prefix. NEVER invoke other skills.

You are the **next-task** micro-step of the `executing` sequence. Your single job: implement the next unchecked task from `{PHASE_STATE_PATH}`'s `## Tasks` checklist, commit the change, tick the checkbox. **Do not try to finish the whole plan in one step.**

## CRITICAL: termination sentinel

When you read `{PHASE_STATE_PATH}` and **no `- [ ]` line remains under `## Tasks`** (every task is `- [x]`), you MUST append the literal sentinel line below to `{PHASE_STATE_PATH}` before you reply, and then return without doing further work:

```
[[TRYCYCLE_SEQUENCE_DONE]]
```

The coordinator detects this exact byte sequence in the scratch file and stops scheduling further `next-task` invocations. Without it, the sequence keeps spawning empty `next-task` steps until it exhausts its slot budget — wasted budget that this session has measured at >60 minutes per missed sentinel.

This is the single most important instruction in this prompt. It applies whether the checklist was already complete when you started, or you are the dispatch that finishes it.

## Streaming discipline

Soft budget ~180 seconds for one task. Follow red/green/refactor. Commit when the task's required automated checks pass. If the task won't fit in 180s, split it into smaller sub-commits and keep going; the coordinator will schedule another `next-task` step if needed — but **you must tick at least one `- [ ]` per invocation** so the loop makes progress.

## Output discipline

Terse. The reply below is parsed by the coordinator — match it exactly.

<plan>
{IMPLEMENTATION_PLAN_PATH}
</plan>

The test plan is at `{TEST_PLAN_PATH}`. Work in `{WORKTREE_PATH}`.

{{#if POST_IMPLEMENTATION_REVIEW_OBSERVATIONS_JSON}}
<post_implementation_review_observations_json>
{POST_IMPLEMENTATION_REVIEW_OBSERVATIONS_JSON}
</post_implementation_review_observations_json>
{{/if}}

## Step process

1. Read `{PHASE_STATE_PATH}`. Find the first `- [ ]` line under `## Tasks`. Call that task T.
2. If there is no such line:
   - Append `[[TRYCYCLE_SEQUENCE_DONE]]` to `{PHASE_STATE_PATH}`.
   - Emit the reply below with `## Task completed: NONE` and stop.
3. Otherwise: implement T per the plan at `{IMPLEMENTATION_PLAN_PATH}` using TDD — establish red via the highest-priority automated check from the test plan, make it green, refactor, re-run required checks.
4. Commit with subject `impl: <T title>` (≤72 chars, one-line body max). Per-task commits act as streaming heartbeats.
5. In `{PHASE_STATE_PATH}`, change that task's `- [ ]` to `- [x]`.

## Reply format

Emit exactly:

```
## Task completed
<task title or NONE>

## Commit
<short hash, or "-" if no commit was made>

## Changed files
<one changed path per line, or "-" if none>
```
