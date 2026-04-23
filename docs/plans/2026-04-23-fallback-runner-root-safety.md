# Fallback-runner root safety + heartbeat enforcement

> **For agentic workers:** REQUIRED: Use trycycle-executing to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make trycycle's fallback-runner mode survive Claude Code subagent stream-idle timeouts on long phases. Specifically, fix the hard crash that happens when the coordinator runs as root, and enforce that every dispatched prompt carries the streaming-heartbeat rule introduced in `9bb41ce`.

**Architecture:** Two files change. (1) `orchestrator/subagent_runner.py` — `_claude_command` and `_claude_resume_command` stop emitting `--dangerously-skip-permissions` unconditionally; under root they emit `--allowedTools` instead, with an environment override for customisation and a clear `escalate_to_user` path when neither works. (2) `orchestrator/prompt_builder/validate_rendered.py` — adds a heartbeat check that rejects rendered prompts whose "Streaming discipline" section is missing or empty, wired into `run_phase.py prepare`. Tests follow both additions.

**Tech Stack:** Python 3, `unittest` (repo's existing test framework), `argparse`, `subprocess` (real, no mocks per the testing strategy).

---

## Design decisions

### Why detect root rather than always use `--allowedTools`

Backward compatibility is a stated constraint. Existing installs (non-root users, sandboxes without internet) expect `--dangerously-skip-permissions` and it works for them. Switching everyone to `--allowedTools` would be a behavior change in the common case. Root detection is the minimum branch: under `os.geteuid() == 0` we swap flags; otherwise we don't.

### Why `--allowedTools` with an explicit list, not `--permission-mode bypassPermissions`

`bypassPermissions` is a named alias for `--dangerously-skip-permissions` — same refusal under root. `--allowedTools` is the supported path the CLI offers for controlled unattended operation. The trycycle phases only need Read, Write, Edit, Bash, Grep, Glob; listing them explicitly is more auditable than "bypass everything."

### Why an environment override (`TRYCYCLE_CLAUDE_ALLOWED_TOOLS`)

Trycycle must work across projects and repos. Some subagents may need other tools (e.g. `WebFetch` during research phases). The override lets a user extend the list without patching the runner. Default covers the common phases.

### Why `escalate_to_user` when `--allowedTools` isn't accepted

The installed `claude` binary may be older than the `--allowedTools`-supporting version. Silently falling through to the unsafe default would crash under root anyway. Probing the help output once at dispatch time (already done in `_probe_claude` — we extend it with one required-token check) and escalating with a clear message keeps the failure mode actionable.

### Why extend `validate_rendered.py` rather than add a sibling file

The file already implements placeholder validation and non-empty-tag validation with a CLI, tests, and an importable `validate_rendered_prompt` function. The heartbeat check is semantically the same class of thing: "this rendered prompt has the content it's supposed to have." Keeping it in one module keeps the validator surface coherent.

### Why a section-name match rather than a regex for heartbeat content

"Streaming discipline" is a stable section header in every heavy prompt (added in `9bb41ce`). Requiring the heading plus a non-empty body catches tail-appended, missing, or accidentally-stripped cases. A semantic regex (looking for "heartbeat" or "90 seconds") would be brittle against wording drift and would false-positive if the phrase appeared in a code block.

### Why P2 is deferred

P2 (automatic re-dispatch on stream-idle via `--resume`) is cheap to add later on top of the P0+P1 fix, and the problem statement gates it on measurement: "out of scope unless measurements after P0+P1 still show >20% timeout rate." No guessing.

## File structure

- Modify: `orchestrator/subagent_runner.py`
  - Add `_claude_permission_args(probe_help_text)` helper that returns either `["--dangerously-skip-permissions"]` (non-root) or `["--allowedTools", "<csv>"]` (root), with env override via `TRYCYCLE_CLAUDE_ALLOWED_TOOLS`. Returns `None` on the escalate path.
  - Modify `_probe_claude` to also check for `--allowedTools` in help output and return that fact.
  - Modify `_claude_command` and `_claude_resume_command` to take the probe result and call the helper.
  - Modify `_run_backend` / `_resume_backend` / `_command_run` / `_command_resume` so the escalate path surfaces a clean `dispatch.status: "escalate_to_user"` payload.
- Modify: `orchestrator/prompt_builder/validate_rendered.py`
  - Add `validate_heartbeat_rule(prompt_text, section_header)` function.
  - Wire into `validate_rendered_prompt` via a new optional parameter.
  - Add CLI flag `--require-heartbeat-section SECTION` (repeatable).
- Modify: `orchestrator/run_phase.py`
  - In `prepare` command, after rendering the prompt for heavy phases, invoke `validate_rendered_prompt(..., required_heartbeat_sections=["Streaming discipline"])`. Heavy phases are the ones that received the heartbeat block in `9bb41ce`: `test-strategy`, `test-plan`, `executing`, `post-implementation-review`, `planning-initial`, `planning-edit`.
- Add: `tests/test_subagent_runner_claude_command.py`
  - Tests for `_claude_command` / `_claude_resume_command` vector shape under root vs. non-root, with and without `TRYCYCLE_CLAUDE_ALLOWED_TOOLS`, with probe reporting `--allowedTools` unsupported.
- Modify: `tests/test_validate_rendered_prompt.py`
  - Tests for the new heartbeat validator.

## Strategy gate

- **Is this the right problem?** Yes. The user's session log cites both the stream-idle timeouts (which `9bb41ce` addresses in prose) and a hard crash in fallback mode under root (which no code currently addresses). Together they explain the fallback-to-direct-coordinator behavior.
- **Is the architecture right?** Yes. A narrow behaviour switch guarded on `os.geteuid()` is reversible, backward-compatible, and auditable. The heartbeat validator reuses existing infrastructure (a CLI flag, an importable function, a test file).
- **Could we do less?** We could just flip the default to `--allowedTools` without a root check. That would break non-root users who rely on `--dangerously-skip-permissions`. Rejected.
- **Could we do more?** P2 (automatic resume on stream-idle). Explicitly deferred.

---

### Task 1: `_probe_claude` reports `--allowedTools` support

**Files:**
- Modify: `orchestrator/subagent_runner.py`

- [ ] **Step 1: Add `supports_allowed_tools` to the probe payload**

In `_probe_claude`, after the existing `required_tokens` check, additionally scan for `--allowedTools` in the help output. Return the boolean as `supports_allowed_tools` in the payload. Do not fail the probe if missing — only the root branch depends on it.

- [ ] **Step 2: Verify no other probe consumers break**

Grep for `supports_resume` usage to confirm adding a new sibling field is harmless. `_resolve_backend_selection` and the `_command_run` / `_command_resume` paths only read `available` and `supports_resume`. No changes needed beyond the probe.

### Task 2: `_claude_permission_args` helper

**Files:**
- Modify: `orchestrator/subagent_runner.py`

- [ ] **Step 1: Add the helper**

```python
def _claude_permission_args(*, supports_allowed_tools: bool) -> list[str] | str:
    """Return the CLI args controlling claude permission handling.

    Non-root: always ["--dangerously-skip-permissions"].
    Root:     ["--allowedTools", "<csv>"] if the CLI supports it, else an
              error string the caller surfaces as escalate_to_user.
    """
    if not _is_root():
        return ["--dangerously-skip-permissions"]
    if not supports_allowed_tools:
        return (
            "Cannot run as root: installed claude CLI does not accept "
            "--allowedTools. Upgrade claude or set "
            "TRYCYCLE_CLAUDE_ALLOWED_TOOLS to override, or run as a "
            "non-root user."
        )
    tools = _read_nonempty_env("TRYCYCLE_CLAUDE_ALLOWED_TOOLS") or (
        "Read,Write,Edit,Bash,Grep,Glob"
    )
    return ["--allowedTools", tools]
```

`_is_root()` returns `os.geteuid() == 0` on POSIX, `False` on Windows (`os` has no `geteuid` there; use `hasattr`).

- [ ] **Step 2: Thread into `_claude_command` and `_claude_resume_command`**

Both functions gain a new keyword-only parameter `supports_allowed_tools: bool`. They call `_claude_permission_args` and, if the return value is a list, splice it in place of the current hardcoded `--dangerously-skip-permissions`. If it's a string, they raise a new `_RootPermissionError(str)` carrying the message.

### Task 3: Callers surface the escalate path cleanly

**Files:**
- Modify: `orchestrator/subagent_runner.py`

- [ ] **Step 1: Catch `_RootPermissionError` in `_run_backend` and `_resume_backend`**

Wrap the `_claude_command` / `_claude_resume_command` call in a try/except. On error, return a `run_result` dict with `"permission_error": str(exc)` set.

- [ ] **Step 2: Translate in `_command_run` / `_command_resume`**

In `_classify_run_result` (or a new tiny helper invoked before it), if `permission_error` is set, return `("escalate_to_user", permission_error)`. The final payload emits `status: "escalate_to_user"` with a clear `message`.

### Task 4: Unit tests for the command vector

**Files:**
- Add: `tests/test_subagent_runner_claude_command.py`

- [ ] **Step 1: Test non-root still emits `--dangerously-skip-permissions`**

Monkey-patch `os.geteuid` to return `1000`. Call `_claude_command(binary="claude", effort=None, model=None, supports_allowed_tools=True)`. Assert `"--dangerously-skip-permissions" in argv` and `"--allowedTools" not in argv`.

- [ ] **Step 2: Test root swaps to `--allowedTools` with the default list**

Monkey-patch `os.geteuid` to return `0`. Unset `TRYCYCLE_CLAUDE_ALLOWED_TOOLS`. Assert `"--dangerously-skip-permissions" not in argv`, `"--allowedTools" in argv`, and the value immediately after is `"Read,Write,Edit,Bash,Grep,Glob"`.

- [ ] **Step 3: Test root honours `TRYCYCLE_CLAUDE_ALLOWED_TOOLS`**

Monkey-patch `os.geteuid` to return `0`. Set the env var to `"Read,Write"`. Assert the argv contains exactly that csv.

- [ ] **Step 4: Test root + unsupported `--allowedTools` raises `_RootPermissionError`**

Monkey-patch `os.geteuid` to return `0`. Call with `supports_allowed_tools=False`. Assert `_RootPermissionError` is raised and its message mentions `TRYCYCLE_CLAUDE_ALLOWED_TOOLS`.

- [ ] **Step 5: Same four cases for `_claude_resume_command`**

Covers the parallel path.

### Task 5: Heartbeat validator

**Files:**
- Modify: `orchestrator/prompt_builder/validate_rendered.py`

- [ ] **Step 1: Add `validate_heartbeat_rule`**

```python
HEARTBEAT_SECTION_RE_TEMPLATE = (
    r"(?P<header>^##\s+{section}\s*$)\n+"
    r"(?P<body>(?:(?!^##\s+).)*)"
)

def validate_heartbeat_rule(prompt_text: str, section: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]*", section):
        raise ValidationError(f"invalid section name: {section!r}")
    pattern = re.compile(
        HEARTBEAT_SECTION_RE_TEMPLATE.format(section=re.escape(section)),
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(prompt_text)
    if not match:
        raise ValidationError(
            f"rendered prompt is missing required section: {section!r}"
        )
    if not match.group("body").strip():
        raise ValidationError(
            f"rendered prompt has empty {section!r} section"
        )
```

- [ ] **Step 2: Wire into `validate_rendered_prompt`**

Add `required_heartbeat_sections: list[str] | None = None` parameter. Iterate and call `validate_heartbeat_rule`.

- [ ] **Step 3: Add CLI flag**

`--require-heartbeat-section SECTION`, `action="append"`. Pass through to `validate_rendered_prompt`.

### Task 6: Heartbeat validator tests

**Files:**
- Modify: `tests/test_validate_rendered_prompt.py`

- [ ] **Step 1: Accepts prompt with non-empty Streaming discipline section**
- [ ] **Step 2: Rejects prompt missing the section**
- [ ] **Step 3: Rejects prompt where the section is empty (header only, followed by another `##` or EOF)**
- [ ] **Step 4: Accepts when the section body has trailing whitespace/newlines but substantive text**

### Task 7: Wire heartbeat check into `run_phase.py prepare`

**Files:**
- Modify: `orchestrator/run_phase.py`

- [ ] **Step 1: Define the heavy-phase allowlist**

At module scope:
```python
HEAVY_PHASES_REQUIRING_HEARTBEAT = frozenset({
    "test-strategy", "test-plan", "executing",
    "post-implementation-review", "planning-initial", "planning-edit",
})
```

- [ ] **Step 2: After rendering, validate**

After the rendered prompt is written and before the prepare command returns the `prompt_path`, call `validate_rendered.validate_rendered_prompt(rendered_text, required_heartbeat_sections=["Streaming discipline"])` iff the phase is in the allowlist. On `ValidationError`, return a JSON error payload with clear `status: "prompt_validation_failed"` and message; do not dispatch.

- [ ] **Step 3: Leave the `run` (fallback-runner) path alone**

It already delegates to `prepare`; the validation runs once there.

### Task 8: Full regression + E2E dry-run

- [ ] **Step 1: Run `/root/.local/bin/pytest` across the whole repo**

Expect 88 + new-tests to pass. Investigate and fix any failure.

- [ ] **Step 2: Dry-run end-to-end**

```bash
python3 orchestrator/subagent_runner.py run \
  --phase planning-initial \
  --prompt-file /tmp/prompt.txt \
  --workdir /tmp \
  --backend claude \
  --dry-run
```

Inspect the JSON `process.command` — expect no `--dangerously-skip-permissions` and the presence of `--allowedTools Read,Write,Edit,Bash,Grep,Glob`. Record a before/after screenshot in the PR summary.

### Task 9: Commit per task, push, merge

- [ ] **Step 1-N: one commit per completed task**

Per the override in `subagents/prompt-executing.md` and `maintenance/skill-instructions/trycycle-executing.txt` (both landed in `9bb41ce`).

- [ ] **Step Final: Merge to main**

Fetch origin, ff-merge the feature branch onto main, push origin main.
