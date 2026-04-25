---
name: trycycle-pulse
description: One-tick liveness check + auto-advance for long-running trycycle phases. Designed to be paired with /loop (e.g. `/loop 10m /trycycle-pulse`) so a trycycle session self-advances through phase transitions while the user is away from the keyboard. Invoke only when a trycycle session is in flight.
---

# trycycle-pulse

A single tick of trycycle's missing wake-up loop. The Claude Code harness only delivers background-task notifications at agent-turn boundaries, so the trycycle skill's "monitor every 5 minutes" rule is structurally unreachable without a recurring slash command. This is that command.

## What this slash command does on each invocation

1. Discovers the most recent fallback-runner dispatch under `/tmp/trycycle-{phase,seq}-*` (sorted by mtime).
2. Calls `lifesigns.py check-fallback` against the latest dispatch directory.
3. Branches on the result:
   - **alive** — emits a one-line status and exits.
   - **complete-success** — auto-advances to the next trycycle phase via a backgrounded `run_phase.py` invocation, when the transition is gate-free.
   - **complete-failure / escalate** — emits a `=== STOP THE LOOP ===` banner naming the failure.
   - **hard-gate** — same banner, naming the gate the user must resolve.

## How to use

Inside a trycycle session, after you've dispatched a long phase (planning-initial or executing) into the fallback runner:

```
/loop 10m /trycycle-pulse
```

`/loop` is a Claude Code bundled skill that synthesises an agent turn at the given interval and runs the slash command on it. Each tick produces a structured one-screen summary you can scan back through.

To stop the loop, send a normal message in your trycycle session — `/loop` documents its own stop convention. Pulse never cancels the loop programmatically.

## Implementation

Pulse is a thin slash-command wrapper around `python3 ~/.claude/skills/trycycle/orchestrator/pulse.py`. When this slash command fires, run the helper script and emit its stdout verbatim:

```bash
python3 "$HOME/.claude/skills/trycycle/orchestrator/pulse.py" "$@"
```

The helper handles discovery, lifesigns invocation, decision logic, and auto-advance dispatch on its own. Do not interpret or re-summarise its output — it is already structured for the user.

If `~/.claude/skills/trycycle/orchestrator/pulse.py` does not exist, the user has not installed the trycycle skill at the expected path. Tell them to install it or symlink it; do not try to fall back.

### Auto-advance scope

Pulse drives these transitions automatically when the latest dispatch reports `status: ok`:

- `planning-initial` → `planning-edit` (round 1)
- `planning-edit` REVISED/CREATED → `planning-edit` (next round; capped at 5 per SKILL.md)
- `planning-edit` READY → `test-plan`
- `test-plan` (no `## Strategy changes requiring user approval` section) → `executing` (run-sequence)
- `executing` → `post-implementation-review`
- `post-implementation-review` with `blocking_issue_count > 0` → `executing` fix-round (capped at 8 per SKILL.md)

Pulse stops at every hard gate the trycycle skill defines:

- `test-strategy` and `finish` phases (always require approval).
- Any reply containing `USER DECISION REQUIRED:`.
- 5-round plan-editor cap reached without READY.
- 8-round review cap reached.
- `post-implementation-review` with `blocking_issue_count == 0` (proceed to `finish`, which itself blocks).
- Any non-ok dispatch status.
- Any auto-advance whose required bindings (plan path, test plan path, transcript, observations file) cannot be resolved from `/tmp`.

### Limitations

- **Fallback-runner mode only.** Native-Agent dispatches do not write to `/tmp/trycycle-*`; pulse cannot observe them. Document the limitation in the trycycle SKILL.md's "Periodic self-advancement" section.
- **No transcript canary lookup.** Pulse reuses an existing `inputs/USER_REQUEST_TRANSCRIPT.txt` from a prior phase's prepare. The first phase that needs a transcript must still be dispatched by the trycycle session itself.
- **No programmatic loop cancellation.** Pulse can only print a clear stop banner; the user must address the gate.
- **Single tmp root.** Pulse looks under `/tmp` by default. Pass `--tmp-root` if your environment is different.

## Installation

This skill ships inside the trycycle repo at `pulse-skill/`. To make `/trycycle-pulse` resolvable from Claude Code, symlink it next to your trycycle install:

```bash
ln -s "$HOME/.claude/skills/trycycle/pulse-skill" "$HOME/.claude/skills/trycycle-pulse"
```

The trycycle install path itself is `~/.claude/skills/trycycle/` per the trycycle README. If you cloned trycycle elsewhere, adjust the symlink target.
