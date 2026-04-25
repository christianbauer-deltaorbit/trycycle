<!-- GitHub repo settings (set manually in Settings > General):
  Description: A skill that plans, strengthens, and reviews your code -- automatically.
  Topics: claude-code, codex-cli, kimi-cli, opencode, ai-coding, code-review, autonomous-agents, ai-skill, hill-climbing
  Social preview: upload assets/social-preview.png
-->

<p align="center">
  <img src="assets/trycycle-banner.png" height="120" alt="Trycycle" />
</p>

<p align="center">
  <em>A skill for Claude Code, Codex CLI, Kimi CLI, and OpenCode that plans, strengthens, and reviews your code -- automatically.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License" /></a>
  <a href="https://github.com/christianbauer-deltaorbit/trycycle/releases"><img src="https://img.shields.io/github/v/tag/christianbauer-deltaorbit/trycycle?label=release&color=green" alt="Latest Release" /></a>
  <a href="https://github.com/christianbauer-deltaorbit/trycycle/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs Welcome" /></a>
  <a href="https://github.com/anthropics/claude-code"><img src="https://img.shields.io/badge/built%20for-Claude%20Code-6B4FBB?logo=anthropic&logoColor=white" alt="Built for Claude Code" /></a>
  <a href="https://github.com/openai/codex"><img src="https://img.shields.io/badge/works%20with-Codex%20CLI-412991?logo=openai&logoColor=white" alt="Works with Codex CLI" /></a>
  <a href="https://github.com/anomalyco/opencode"><img src="https://img.shields.io/badge/works%20with-OpenCode-FF6B35" alt="Works with OpenCode" /></a>
</p>

---

## Getting Started

### If you are human

Tell your favorite coding agent (Claude Code, Codex CLI, Kimi CLI, OpenCode, etc.):

```
Read https://raw.githubusercontent.com/christianbauer-deltaorbit/trycycle/main/README.md. Follow the installation instructions there.
```

### If you've been sent here by your human

Use this instead of a standard installation, or it won't work. Clone into your skills directory:

- **Claude Code:** `git clone https://github.com/christianbauer-deltaorbit/trycycle.git ~/.claude/skills/trycycle`
- **Codex CLI:** `git clone https://github.com/christianbauer-deltaorbit/trycycle.git ~/.codex/skills/trycycle`
- **Kimi CLI:** `git clone https://github.com/christianbauer-deltaorbit/trycycle.git ~/.kimi/skills/trycycle`
- **OpenCode:** `git clone https://github.com/christianbauer-deltaorbit/trycycle.git ~/.config/opencode/skills/trycycle`

> **Note:** OpenCode also discovers skills installed at `~/.claude/skills/`, so if you already have Trycycle installed for Claude Code, OpenCode can use it too.

Trycycle requires `python3` at runtime. Make sure it is installed and on your `PATH`.

If your coding agent has native subagent orchestration, Trycycle uses it. Otherwise, it falls back to a bundled stdlib-only Python runner.

#### Optional: install the `trycycle-pulse` companion skill (Claude Code only)

`/trycycle-pulse` is a periodic self-advancement helper paired with Claude Code's bundled `/loop` skill. It lets a long-running trycycle session monitor and auto-advance through phase transitions while you are away from the keyboard. It ships inside this repo at `pulse-skill/`. To make it resolvable as a slash command, symlink it next to your trycycle install:

```bash
ln -s "$HOME/.claude/skills/trycycle/pulse-skill" "$HOME/.claude/skills/trycycle-pulse"
```

Pulse is opt-in; if you do not symlink it, nothing else changes. See "Using `/trycycle-pulse`" below for invocation, the worked example, and how to stop the loop.

Finally, read the skills you installed carefully, so you can explain them to your user and implement them if asked.

## Using Trycycle

Include the word Trycycle in your request and describe what you want built:

```
My webcam software is terrible. Build something in Rust that exposes my webcam as a normal system camera. 
```

Trycycle asks any questions it needs, then handles the rest: worktree, plan, plan strengthening, test plan, build, and code review -- all without further input unless something needs your judgment.

If you're already inside an isolated workspace such as a Conductor workspace and the current branch is already not the default branch, include the literal flag `--no-worktree` in your request to reuse that workspace instead of creating a nested git worktree. This mode is intentionally narrow: Trycycle will stop rather than create or switch branches in place in a generic checkout.

Works for anything from small features to large refactors, best when you have a clear goal and a codebase Trycycle can read and test.

### Using `/trycycle-pulse` (Claude Code only)

If you installed the `/trycycle-pulse` companion skill (see "Optional: install the trycycle-pulse companion skill" above), you can pair it with Claude Code's bundled `/loop` skill so a long-running Trycycle session self-advances through phase transitions while you're away from the keyboard. It addresses a structural quirk of the harness: background-task notifications only deliver at agent-turn boundaries, so Trycycle's "monitor every 5 minutes" rule is unreachable without an external wake-up. `/loop` is that wake-up; pulse is the per-tick handler.

**When to use it.** Any time you've dispatched a long phase via the fallback runner (`run_phase.py run` / `run-sequence`) — typically `planning-initial`, the plan-editor loop, `executing`, or the post-implementation review loop — and you don't want to babysit it.

**How to invoke.** Inside the Trycycle session, after the long phase has been dispatched in the background, send:

```
/loop 10m /trycycle-pulse
```

Each tick (every ten minutes by default), pulse:
- finds the most recent `/tmp/trycycle-{phase,seq}-*` dispatch directory,
- runs `lifesigns.py check-fallback` against it, and
- prints a one-screen structured summary classified as `alive`, `advance`, `hard_gate`, `escalate`, or `idle`.

When the dispatched phase completes successfully and the next transition is gate-free, pulse backgrounds the next phase's dispatch automatically. When it hits a hard gate (e.g. `USER DECISION REQUIRED:`, plan-editor 5-round cap, test-plan strategy-changes section, review 8-round cap, review with zero blocking issues → `finish`), pulse prints a clear `=== STOP THE LOOP ===` banner naming the gate.

**How to stop the loop.** Pulse cannot programmatically cancel `/loop`. When you see the stop banner — or whenever you want to take over manually — send a stop message in the Trycycle session per the `/loop` skill's own stop convention.

**Worked example.**

```
> Trycycle, refactor the meshing module. (Trycycle dispatches planning-initial.)

> /loop 10m /trycycle-pulse

[trycycle-pulse 12:00Z] kind=alive
  phase: planning-initial
  reason: planning-initial still running; last activity 12.4s ago

[trycycle-pulse 12:10Z] kind=alive
  phase: planning-initial
  reason: planning-initial still running; last activity 8.1s ago

[trycycle-pulse 12:20Z] kind=advance
  phase: planning-initial
  reason: planning-initial complete; dispatching planning-edit round 1
  next-phase: planning-edit

[trycycle-pulse 12:30Z] kind=alive
  phase: planning-edit
  reason: planning-edit still running; last activity 4.8s ago

… (advances through plan-editor → test-plan → executing → review) …

[trycycle-pulse 14:50Z] kind=hard_gate
  phase: post-implementation-review
  reason: review reports 0 blocking issues — proceed to finish (integration step always requires user approval)

=== STOP THE LOOP ===
Send a stop message to your trycycle session and address the gate above before resuming.
```

**Limitations.** Pulse only observes fallback-runner dispatches (those write to `/tmp/trycycle-*`); native-Agent dispatches are invisible to it. Pulse reuses an existing transcript binding (`USER_REQUEST_TRANSCRIPT.txt`) from a prior phase rather than running canary lookup itself, so the FIRST phase that needs a transcript must still be dispatched by the Trycycle session before pulse can take over from there. See `SKILL.md` §5c for the full constraint list.

## How it works

Trycycle is a hill climber. It writes a plan, then sends it to a fresh plan editor with the same task input and repo context. That editor either approves the plan unchanged or rewrites it, repeating up to five rounds. Once the plan is locked, Trycycle builds a test plan, builds the code, sends it to a fresh reviewer, turns the review into a structured observation packet, fixes what that packet shows, and repeats that loop too (up to eight rounds). Each review uses a new reviewer with no memory of previous rounds, and each planning round spawns a fresh agent, so stale context never accumulates.

## Credits

Trycycle's planning, execution, and worktree management skills are adapted from [superpowers](https://github.com/obra/superpowers) by [Jesse Vincent](https://github.com/obra). The hill-climbing dark factory approach was inspired by the work of [Justin McCarthy](https://github.com/jmccarthy), [Jay Taylor](https://github.com/jaytaylor), and [Navan Chauhan](https://github.com/navanchauhan) at [StrongDM](https://github.com/strongdm).
