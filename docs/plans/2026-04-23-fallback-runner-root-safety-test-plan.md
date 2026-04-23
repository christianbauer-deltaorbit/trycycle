# Test plan — Fallback-runner root safety + heartbeat enforcement

Companion to `2026-04-23-fallback-runner-root-safety.md`.

## Harness requirements

- **unittest** — the repo's existing test framework (all tests under `tests/` use `unittest.TestCase` subclasses). Invoked via `/root/.local/bin/pytest tests/` (pytest discovers unittest classes natively; no conversion needed).
- **pytest** — already installed at `/root/.local/bin/pytest`. Used as the runner only.
- **Dry-run dispatch harness** — `subagent_runner.py run --dry-run` builds the argv without subprocessing claude. This is the primary integration harness because it exercises the real command-builder code with the real probe output.
- **`claude --help` probe** — reads real flag support from the installed CLI. Not mocked, per the user's testing strategy ("spawn a real `claude --help` probe if needed to verify `--allowedTools` is accepted").

No new harness code needs to be written. All prerequisites exist.

## Test plan

### 1. Root detection swaps the permission flag (P0 core)

- **Name**: Under root, the built claude argv does not contain `--dangerously-skip-permissions` and does contain `--allowedTools` with the default tool list.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API (import `_claude_command` from `subagent_runner`).
- **Preconditions**: `os.geteuid` monkey-patched to return `0`; `TRYCYCLE_CLAUDE_ALLOWED_TOOLS` unset; `supports_allowed_tools=True`.
- **Actions**: call `_claude_command(binary="claude", effort=None, model=None, supports_allowed_tools=True)`.
- **Expected outcome**: returned argv contains `"--allowedTools"` followed by `"Read,Write,Edit,Bash,Grep,Glob"`, and does NOT contain `"--dangerously-skip-permissions"`.

### 2. Non-root preserves backward-compatible flag (P0 regression guard)

- **Name**: As non-root, the built claude argv still contains `--dangerously-skip-permissions` and does not contain `--allowedTools`.
- **Type**: regression
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions**: `os.geteuid` monkey-patched to return `1000`.
- **Actions**: same call as test 1.
- **Expected outcome**: argv contains `--dangerously-skip-permissions`, does not contain `--allowedTools`.

### 3. Environment override wins over the default list (P0)

- **Name**: `TRYCYCLE_CLAUDE_ALLOWED_TOOLS` customises the allowed-tools csv.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions**: root; env var set to `"Read,Write"`.
- **Actions**: call `_claude_command(...)`.
- **Expected outcome**: argv contains `"--allowedTools"` followed by exactly `"Read,Write"`.

### 4. Unsupported `--allowedTools` raises `_RootPermissionError` (P0 escalation)

- **Name**: When the CLI doesn't support `--allowedTools`, the root path raises a clear error.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions**: root; `supports_allowed_tools=False`.
- **Actions**: call `_claude_command(...)`.
- **Expected outcome**: `_RootPermissionError` raised; message mentions `TRYCYCLE_CLAUDE_ALLOWED_TOOLS`.

### 5. Same four cases for `_claude_resume_command` (P0 parallel path)

- **Name**: Resume command honours the same root/non-root/override/unsupported matrix.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions/Actions/Expected**: parametrised mirror of tests 1-4 against `_claude_resume_command`.

### 6. Probe payload exposes `supports_allowed_tools` (P0 plumbing)

- **Name**: `_probe_claude` returns `supports_allowed_tools: True` against the installed claude CLI (which accepts the flag, as verified by `claude --help`).
- **Type**: integration
- **Disposition**: new
- **Harness**: real `claude --help` probe (not mocked).
- **Preconditions**: `/opt/node22/bin/claude` on PATH.
- **Actions**: call `_probe_claude("claude")`.
- **Expected outcome**: `available: True`, `supports_allowed_tools: True`.

### 7. End-to-end dry-run as root shows the new argv (P0 acceptance)

- **Name**: `subagent_runner.py run --dry-run --backend claude --phase planning-initial` (as root) yields a `process.command` that contains `--allowedTools` and does NOT contain `--dangerously-skip-permissions`.
- **Type**: scenario
- **Disposition**: new
- **Harness**: dry-run dispatch harness (subprocess spawn of `python3 subagent_runner.py run ... --dry-run`).
- **Preconditions**: running as root (true in this environment); claude CLI installed; a trivial 10-line prompt file written to a temp path.
- **Actions**: spawn the subprocess; capture stdout JSON.
- **Expected outcome**: parsed JSON's `process.command` list excludes `"--dangerously-skip-permissions"`, includes `"--allowedTools"` followed by a non-empty string.

### 8. Heartbeat validator accepts a prompt with a non-empty section (P1 core)

- **Name**: `validate_heartbeat_rule` returns without raising when `## Streaming discipline\n\nnon-empty body` is present.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions**: prompt text containing the section with substantive body.
- **Actions**: call `validate_heartbeat_rule(prompt_text, "Streaming discipline")`.
- **Expected outcome**: no exception.

### 9. Heartbeat validator rejects missing section (P1)

- **Name**: `validate_heartbeat_rule` raises `ValidationError` when the section header is absent.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions**: prompt text with no `## Streaming discipline` header.
- **Actions**: call the validator.
- **Expected outcome**: `ValidationError` mentioning the missing section.

### 10. Heartbeat validator rejects empty section (P1)

- **Name**: `validate_heartbeat_rule` raises when the header exists but body is whitespace-only.
- **Type**: unit
- **Disposition**: new
- **Harness**: direct-API.
- **Preconditions**: prompt text `"## Streaming discipline\n\n\n## Next section\nfoo"`.
- **Actions**: call the validator.
- **Expected outcome**: `ValidationError` mentioning empty section.

### 11. All six shipped prompts pass the heartbeat check (P1 regression guard)

- **Name**: Every rendered subagent prompt from `9bb41ce` has a non-empty `Streaming discipline` section.
- **Type**: scenario
- **Disposition**: new
- **Harness**: direct-API; read each of `prompt-test-strategy.md`, `prompt-test-plan.md`, `prompt-executing.md`, `prompt-post-impl-review.md`, `prompt-planning-initial.md`, `prompt-planning-edit.md`.
- **Preconditions**: files exist under `subagents/`.
- **Actions**: for each, read and call `validate_heartbeat_rule(..., "Streaming discipline")`.
- **Expected outcome**: all six pass.

### 12. CLI `--require-heartbeat-section` exits 1 on a bad prompt (P1 CLI)

- **Name**: `python3 validate_rendered.py --prompt-file <bad> --require-heartbeat-section "Streaming discipline"` exits with code 1 and prints the error.
- **Type**: scenario
- **Disposition**: new
- **Harness**: subprocess spawn.
- **Preconditions**: a prompt file with no heartbeat section written to a temp path.
- **Actions**: spawn the CLI.
- **Expected outcome**: returncode `1`; stderr contains "Streaming discipline".

### 13. `run_phase.py prepare` fails for a heavy phase with a non-heartbeat prompt (P1 wiring)

- **Name**: Rendering a prompt without heartbeat content for `--phase planning-initial` fails prepare with a validation error.
- **Type**: scenario
- **Disposition**: new
- **Harness**: subprocess spawn of `run_phase.py prepare`.
- **Preconditions**: a fake template without heartbeat section in a temp path.
- **Actions**: spawn prepare pointing at that template.
- **Expected outcome**: non-zero exit; stderr/stdout surfaces the validation failure; no `prompt_path` printed.

### 14. Full pre-existing test suite stays green (regression gate)

- **Name**: `/root/.local/bin/pytest tests/` exits 0 across all 88 pre-existing tests plus new ones.
- **Type**: regression
- **Disposition**: existing + new
- **Harness**: pytest.
- **Actions**: run pytest on the full tests directory.
- **Expected outcome**: zero failures.

## Coverage summary

Covered:
- Command-vector shape under every combination (root / non-root × env override set/unset × supports_allowed_tools true/false).
- Probe plumbing against the real installed CLI.
- End-to-end dry-run under root (primary acceptance gate).
- Heartbeat-section validator (present / missing / empty).
- All six real prompts pass the heartbeat check (guards against future drift).
- CLI exit code contract.
- Integration of heartbeat check into the dispatch pathway.
- Full regression suite.

Explicitly excluded (per the user's gating and the P2-deferred decision):
- Live stream-idle recovery. Requires repeated network failures to trigger and real Claude API access.
- Automatic `--resume` on stream-idle (P2). Deferred; no tests because no code.
- Windows behavior. `_is_root` returns `False` there; we rely on the existing non-root code path and a unit test would be a hypothetical. Documented in code comment.

Residual risk: the real stream-idle scenario requires a live, time-pressured Claude API session to reproduce. We mitigate via (a) the dry-run end-to-end that proves the argv shape, (b) the six-prompt heartbeat regression test that proves the in-prompt fix from `9bb41ce` is still live, and (c) the escalate-to-user path that makes a real root failure legible instead of silent.
