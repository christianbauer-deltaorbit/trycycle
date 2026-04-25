#!/usr/bin/env python3
"""trycycle-pulse — periodic self-advancement helper.

Paired with Claude Code's bundled `/loop` skill, pulse turns the trycycle
"monitor every 5 minutes" rule from a structurally-unreachable instruction
into something the harness actually executes. The user runs

    /loop 10m /trycycle-pulse

and on each tick this script:

  1. Discovers the most recent fallback-runner dispatch under
     /tmp/trycycle-{phase,seq}-*.
  2. Calls orchestrator/lifesigns.py to determine whether the dispatched
     subagent is still alive.
  3. Branches:
       alive            -> one-line status, return.
       complete-success -> auto-advance to the next phase if the
                           transition is gate-free, else print STOP.
       complete-failure -> print STOP banner with the failure reason.
       hard-gate        -> print STOP banner explaining what the user
                           must decide.

Out-of-scope: native-Agent dispatches (no /tmp artifact dir to scan), and
any phase that itself requires user approval to launch (test-strategy,
finish step, USER DECISION REQUIRED replies).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]

# Phase chains that pulse can advance without user input.
# Maps the phase name written to the dispatched artifacts dir
# (`/tmp/trycycle-phase-<phase>-…` or `/tmp/trycycle-seq-<phase>-…`)
# to the next-phase descriptor.
ADVANCEABLE_FROM = {
    "planning-initial": "planning-edit",
    "planning-edit": "planning-edit-or-test-plan",  # branch on Plan verdict
    "test-plan": "executing",
    "executing": "post-implementation-review",
    "post-implementation-review": "executing-or-finish",  # branch on issues
}

# Hard gates that pulse must NEVER auto-traverse.
HARD_GATE_PHASES = frozenset(
    {
        "test-strategy",  # always blocks for user approval
        "finish",  # always blocks for integration choice
    }
)

# Limits inherited from SKILL.md.
PLANNING_EDIT_ROUND_CAP = 5
REVIEW_ROUND_CAP = 8

# Stable patterns the trycycle skill uses for its temp dirs.
PHASE_DIR_PATTERNS = ("trycycle-phase-", "trycycle-seq-")

STOP_LOOP_BANNER = "=== STOP THE LOOP ==="


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


@dataclass
class DispatchSummary:
    """One in-flight or completed trycycle dispatch found on disk."""

    artifacts_dir: Path
    phase: str  # e.g. "planning-initial", "executing"
    is_sequence: bool  # True when artifacts_dir came from run-sequence
    last_mtime: float
    result: dict[str, Any] | None = None  # parsed result.json, if present
    dispatch_dir: Path | None = None  # the dir lifesigns should inspect


def _phase_from_dirname(name: str) -> tuple[str, bool] | None:
    """Extract (phase, is_sequence) from a /tmp dir like
    'trycycle-phase-planning-initial-XXXXX' or 'trycycle-seq-executing-XXXXX'.
    Returns None if the dir name does not match the expected shape.
    """
    for prefix, is_seq in (
        ("trycycle-seq-", True),
        ("trycycle-phase-", False),
    ):
        if name.startswith(prefix):
            tail = name[len(prefix) :]
            # Trailing random suffix — strip everything from the LAST '-' on
            # if it looks like a tempfile mkdtemp suffix (8+ chars of mixed
            # alphanumerics). We accept that simple heuristic; phase names
            # only contain lowercase letters and hyphens.
            parts = tail.rsplit("-", 1)
            if len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9_]{6,}", parts[1]):
                return parts[0], is_seq
            return tail, is_seq
    return None


def _newest_dispatch_dir(tmp_root: Path) -> DispatchSummary | None:
    """Walk tmp_root for trycycle dispatch dirs and return the most recently
    modified one as a DispatchSummary."""
    best: DispatchSummary | None = None
    try:
        entries = list(tmp_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        if not any(entry.name.startswith(p) for p in PHASE_DIR_PATTERNS):
            continue
        parsed = _phase_from_dirname(entry.name)
        if parsed is None:
            continue
        phase, is_sequence = parsed
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best.last_mtime:
            best = DispatchSummary(
                artifacts_dir=entry,
                phase=phase,
                is_sequence=is_sequence,
                last_mtime=mtime,
            )
    return best


def _resolve_dispatch_dir(summary: DispatchSummary) -> Path | None:
    """Locate the leaf dispatch dir lifesigns should poll.

    For a `run` dispatch the layout is `<artifacts_dir>/dispatch/`.
    For a `run-sequence` dispatch we want the most recent step's
    dispatch dir under `<artifacts_dir>/steps/<step>/dispatch/`.
    """
    if not summary.is_sequence:
        candidate = summary.artifacts_dir / "dispatch"
        return candidate if candidate.exists() else None
    steps_dir = summary.artifacts_dir / "steps"
    if not steps_dir.exists():
        return None
    newest_dispatch: Path | None = None
    newest_mtime: float = -1
    for step_dir in steps_dir.iterdir():
        dispatch = step_dir / "dispatch"
        if not dispatch.exists():
            continue
        try:
            mtime = dispatch.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_dispatch = dispatch
    return newest_dispatch


def _load_result_json(summary: DispatchSummary) -> dict[str, Any] | None:
    """Load the most authoritative result.json for the dispatch.

    For sequences, that's `<artifacts_dir>/sequence-result.json`.
    For singles, `<artifacts_dir>/dispatch/result.json` (or, if absent,
    `<artifacts_dir>/result.json` from the prepare step).
    """
    if summary.is_sequence:
        seq_result = summary.artifacts_dir / "sequence-result.json"
        if seq_result.exists():
            try:
                return json.loads(seq_result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None
    for candidate in (
        summary.artifacts_dir / "dispatch" / "result.json",
        summary.artifacts_dir / "result.json",
    ):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None


# ---------------------------------------------------------------------------
# Liveness + reply analysis
# ---------------------------------------------------------------------------


def _run_lifesigns(
    *,
    repo_root: Path,
    dispatch_dir: Path,
    threshold_seconds: int,
) -> dict[str, Any]:
    """Shell out to orchestrator/lifesigns.py check-fallback. Returns the
    parsed JSON payload. Raises RuntimeError on a usage-shape failure
    (helper crashed) — those cases bubble up as a hard gate."""
    helper = repo_root / "orchestrator" / "lifesigns.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--threshold-seconds",
            str(threshold_seconds),
            "check-fallback",
            "--artifacts-dir",
            str(dispatch_dir),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            f"lifesigns helper failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lifesigns helper returned non-JSON: {exc}") from exc


_PLAN_VERDICT_RE = re.compile(r"^##\s+Plan verdict\s*\n+\s*(\w+)", re.MULTILINE)
_PLAN_PATH_RE = re.compile(r"^##\s+Plan path\s*\n+\s*(\S+)", re.MULTILINE)
_TEST_PLAN_PATH_RE = re.compile(r"^##\s+Test plan path\s*\n+\s*(\S+)", re.MULTILINE)
_USER_DECISION_RE = re.compile(r"^USER DECISION REQUIRED:", re.MULTILINE)
_STRATEGY_CHANGE_RE = re.compile(
    r"^##\s+Strategy changes requiring user approval", re.MULTILINE
)


def _read_reply_text(summary: DispatchSummary) -> str:
    """Best-effort read of the dispatched subagent's final reply."""
    if summary.is_sequence:
        result = summary.result or {}
        steps = result.get("steps") or []
        for entry in reversed(steps):
            reply_path = (entry.get("dispatch") or {}).get("reply_path")
            if reply_path and Path(reply_path).exists():
                try:
                    return Path(reply_path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
        return ""
    reply_path = (summary.result or {}).get("reply_path")
    if reply_path and Path(reply_path).exists():
        try:
            return Path(reply_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
    return ""


def _detect_planning_edit_round(tmp_root: Path) -> int:
    """Count completed planning-edit dispatches present on disk."""
    count = 0
    try:
        entries = list(tmp_root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.is_dir() and entry.name.startswith("trycycle-phase-planning-edit-"):
            count += 1
    return count


def _detect_review_round(tmp_root: Path) -> int:
    """Count completed post-implementation-review dispatches on disk."""
    count = 0
    try:
        entries = list(tmp_root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.is_dir() and entry.name.startswith(
            "trycycle-phase-post-implementation-review-"
        ):
            count += 1
    return count


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class PulseAction:
    """The decision pulse made for this tick."""

    kind: str  # "alive" | "advance" | "hard_gate" | "escalate" | "idle"
    reason: str
    next_phase: str | None = None
    next_dispatch_argv: list[str] = field(default_factory=list)
    next_dispatch_env: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


def decide_action(
    *,
    summary: DispatchSummary | None,
    lifesigns: dict[str, Any] | None,
    reply_text: str,
    planning_edit_round: int,
    review_round: int,
) -> PulseAction:
    """Pure state machine. Inputs are everything pulse already gathered.

    Returns a PulseAction describing what to do this tick. Callers handle
    the side-effect (printing, dispatching) separately.
    """
    if summary is None:
        return PulseAction(
            kind="idle",
            reason="no in-flight trycycle dispatch found under /tmp",
        )

    if summary.phase in HARD_GATE_PHASES:
        return PulseAction(
            kind="hard_gate",
            reason=(
                f"latest dispatch is {summary.phase}, which always requires "
                "user approval before the next step"
            ),
        )

    if lifesigns is None:
        return PulseAction(
            kind="idle",
            reason=(
                f"dispatch {summary.artifacts_dir} has no readable lifesigns "
                f"signal yet (subagent may be initialising)"
            ),
        )

    if not lifesigns.get("should_escalate"):
        last = lifesigns.get("last_activity_seconds_ago")
        return PulseAction(
            kind="alive",
            reason=(
                f"{summary.phase} still running; last activity "
                f"{last}s ago"
            ),
            diagnostics={"lifesigns": lifesigns},
        )

    # Lifesigns says the subagent is silent past threshold. That's only a
    # finishing signal if the dispatch wrote a result.json. If it did not,
    # the runner died unobserved — escalate.
    result = summary.result
    if not result:
        return PulseAction(
            kind="escalate",
            reason=(
                f"{summary.phase} silent past threshold and no result.json "
                f"is present — subagent likely died without recording status"
            ),
            diagnostics={"lifesigns": lifesigns},
        )

    status = result.get("status")
    if status not in {"ok"}:
        message = result.get("message") or status or "unknown failure"
        return PulseAction(
            kind="escalate",
            reason=f"{summary.phase} ended with status={status}: {message}",
            diagnostics={"result_status": status, "result_message": message},
        )

    # status == ok. Check the reply for explicit USER DECISION REQUIRED
    # markers — those override automatic advancement even on success.
    if _USER_DECISION_RE.search(reply_text or ""):
        first_line = (
            (reply_text or "").splitlines()[0]
            if reply_text
            else "USER DECISION REQUIRED"
        )
        return PulseAction(
            kind="hard_gate",
            reason=f"{summary.phase} returned USER DECISION REQUIRED: {first_line}",
        )

    # Phase-specific advancement logic.
    return _decide_next_phase(
        summary=summary,
        reply_text=reply_text or "",
        planning_edit_round=planning_edit_round,
        review_round=review_round,
    )


def _decide_next_phase(
    *,
    summary: DispatchSummary,
    reply_text: str,
    planning_edit_round: int,
    review_round: int,
) -> PulseAction:
    """The advance-or-stop branch tree, post-success."""
    phase = summary.phase

    if phase == "planning-initial":
        return PulseAction(
            kind="advance",
            reason="planning-initial complete; dispatching planning-edit round 1",
            next_phase="planning-edit",
        )

    if phase == "planning-edit":
        verdict_match = _PLAN_VERDICT_RE.search(reply_text)
        verdict = verdict_match.group(1).upper() if verdict_match else "UNKNOWN"
        if verdict == "READY":
            return PulseAction(
                kind="advance",
                reason="plan verdict READY; dispatching test-plan",
                next_phase="test-plan",
            )
        if verdict == "REVISED" or verdict == "CREATED":
            if planning_edit_round >= PLANNING_EDIT_ROUND_CAP:
                return PulseAction(
                    kind="hard_gate",
                    reason=(
                        f"plan-editor loop reached {PLANNING_EDIT_ROUND_CAP} "
                        "rounds without a READY verdict (SKILL.md cap)"
                    ),
                )
            return PulseAction(
                kind="advance",
                reason=(
                    f"plan verdict {verdict}; dispatching plan-editor round "
                    f"{planning_edit_round + 1}/{PLANNING_EDIT_ROUND_CAP}"
                ),
                next_phase="planning-edit",
            )
        return PulseAction(
            kind="hard_gate",
            reason=(
                "planning-edit completed but the reply lacked a recognisable "
                "Plan verdict (READY/REVISED/CREATED) — manual inspection needed"
            ),
        )

    if phase == "test-plan":
        if _STRATEGY_CHANGE_RE.search(reply_text):
            return PulseAction(
                kind="hard_gate",
                reason=(
                    "test-plan returned a 'Strategy changes requiring user "
                    "approval' section — manual approval needed"
                ),
            )
        return PulseAction(
            kind="advance",
            reason="test-plan complete; dispatching executing run-sequence",
            next_phase="executing",
        )

    if phase == "executing":
        return PulseAction(
            kind="advance",
            reason="executing complete; dispatching post-implementation-review",
            next_phase="post-implementation-review",
        )

    if phase == "post-implementation-review":
        # We can't reliably parse blocking_issue_count from the reviewer
        # reply alone — it's emitted by review_observations.py. Look for an
        # extracted observations file in the artifacts dir.
        observations_path = _find_review_observations_path(summary)
        blocking = _blocking_issue_count(observations_path)
        if blocking is None:
            return PulseAction(
                kind="hard_gate",
                reason=(
                    "review complete but no extracted observations artifact "
                    "found — run review_observations.py extract manually"
                ),
            )
        if blocking == 0:
            return PulseAction(
                kind="hard_gate",
                reason=(
                    "review reports 0 blocking issues — proceed to finish "
                    "(integration step always requires user approval)"
                ),
                diagnostics={"blocking_issue_count": 0},
            )
        if review_round >= REVIEW_ROUND_CAP:
            return PulseAction(
                kind="hard_gate",
                reason=(
                    f"review loop reached {REVIEW_ROUND_CAP} rounds with "
                    "blocking issues still present (SKILL.md cap)"
                ),
            )
        return PulseAction(
            kind="advance",
            reason=(
                f"review reports {blocking} blocking issue(s); dispatching "
                f"executing fix-round {review_round + 1}/{REVIEW_ROUND_CAP}"
            ),
            next_phase="executing-fix",
            diagnostics={
                "blocking_issue_count": blocking,
                "observations_path": str(observations_path),
            },
        )

    return PulseAction(
        kind="hard_gate",
        reason=f"phase '{phase}' has no automatic advance rule",
    )


def _find_review_observations_path(summary: DispatchSummary) -> Path | None:
    """Convention: orchestrators write the extracted observations next to
    the review reply, or in /tmp/trycycle-review-observations-*.json. This
    helper looks at standard locations and returns the most recent."""
    candidates: list[tuple[float, Path]] = []
    nearby = summary.artifacts_dir / "review-observations.json"
    if nearby.exists():
        try:
            candidates.append((nearby.stat().st_mtime, nearby))
        except OSError:
            pass
    for tmp_path in Path("/tmp").glob("trycycle-review-observations-*.json"):
        try:
            candidates.append((tmp_path.stat().st_mtime, tmp_path))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _blocking_issue_count(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("blocking_issue_count")
    if isinstance(value, int):
        return value
    return None


# ---------------------------------------------------------------------------
# Auto-advance dispatch
# ---------------------------------------------------------------------------


def _resolve_implementation_plan_path(
    *, tmp_root: Path, latest: DispatchSummary
) -> Path | None:
    """Walk back through trycycle dispatch dirs to find the most recent
    IMPLEMENTATION_PLAN_PATH binding or `## Plan path` reply line."""
    # 1. Reply of the latest dispatch.
    reply = _read_reply_text(latest)
    match = _PLAN_PATH_RE.search(reply)
    if match:
        candidate = Path(match.group(1))
        if candidate.exists():
            return candidate
    # 2. Walk siblings sorted newest-first; check reply text and prepare result.
    siblings = []
    try:
        for entry in tmp_root.iterdir():
            if not entry.is_dir():
                continue
            if not any(entry.name.startswith(p) for p in PHASE_DIR_PATTERNS):
                continue
            try:
                siblings.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    except OSError:
        return None
    siblings.sort(key=lambda item: item[0], reverse=True)
    for _, entry in siblings:
        for reply_path_candidate in (
            entry / "dispatch" / "reply.txt",
            entry / "reply.txt",
        ):
            if reply_path_candidate.exists():
                try:
                    text = reply_path_candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                m = _PLAN_PATH_RE.search(text)
                if m and Path(m.group(1)).exists():
                    return Path(m.group(1))
    return None


def _resolve_test_plan_path(
    *, tmp_root: Path
) -> Path | None:
    """Find the most recent test-plan reply and parse `## Test plan path`."""
    siblings: list[tuple[float, Path]] = []
    try:
        for entry in tmp_root.iterdir():
            if entry.is_dir() and entry.name.startswith("trycycle-phase-test-plan-"):
                try:
                    siblings.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
    except OSError:
        return None
    siblings.sort(key=lambda item: item[0], reverse=True)
    for _, entry in siblings:
        for candidate in (
            entry / "dispatch" / "reply.txt",
            entry / "reply.txt",
        ):
            if candidate.exists():
                try:
                    text = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                m = _TEST_PLAN_PATH_RE.search(text)
                if m and Path(m.group(1)).exists():
                    return Path(m.group(1))
    return None


def _resolve_user_request_transcript(
    *, tmp_root: Path
) -> Path | None:
    """Reuse a USER_REQUEST_TRANSCRIPT.txt that prepare wrote earlier.

    Pulse does not run canary lookup itself — that requires the live
    Claude Code session and is fragile from inside a /loop tick. Instead
    we look for the most-recent prepare's `inputs/USER_REQUEST_TRANSCRIPT.txt`
    and reuse its bytes.
    """
    candidates: list[tuple[float, Path]] = []
    try:
        for entry in tmp_root.iterdir():
            if not entry.is_dir():
                continue
            if not any(entry.name.startswith(p) for p in PHASE_DIR_PATTERNS):
                continue
            transcript = entry / "inputs" / "USER_REQUEST_TRANSCRIPT.txt"
            if transcript.exists():
                try:
                    candidates.append((transcript.stat().st_mtime, transcript))
                except OSError:
                    continue
            # run-sequence: each step has its own inputs dir
            steps_dir = entry / "steps"
            if steps_dir.exists():
                for step in steps_dir.iterdir():
                    transcript = step / "inputs" / "USER_REQUEST_TRANSCRIPT.txt"
                    if transcript.exists():
                        try:
                            candidates.append(
                                (transcript.stat().st_mtime, transcript)
                            )
                        except OSError:
                            continue
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _resolve_approved_test_strategy(
    *, tmp_root: Path
) -> Path | None:
    """The trycycle SKILL writes the approved strategy as a temp file under
    /tmp/trycycle-approved-strategy-*.txt (one of several conventions).
    Best-effort scan; pulse falls back to skipping the binding when nothing
    matches."""
    candidates: list[tuple[float, Path]] = []
    for pattern in (
        "trycycle-approved-strategy-*.txt",
        "trycycle-approved-test-strategy-*.txt",
    ):
        for path in tmp_root.glob(pattern):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _resolve_workdir(*, latest: DispatchSummary) -> Path | None:
    """Read workdir from prepare's result.json (always recorded)."""
    if latest.is_sequence:
        # sequence-result.json doesn't carry workdir directly; look at any
        # step's prepare result.
        for step_dir in (latest.artifacts_dir / "steps").glob("*"):
            result = step_dir / "result.json"
            if result.exists():
                try:
                    data = json.loads(result.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                workdir = data.get("workdir")
                if workdir:
                    return Path(workdir)
        return None
    result = latest.artifacts_dir / "result.json"
    if not result.exists():
        return None
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    workdir = data.get("workdir")
    return Path(workdir) if workdir else None


def build_advance_command(
    *,
    repo_root: Path,
    next_phase: str,
    latest: DispatchSummary,
    tmp_root: Path,
    observations_path: Path | None = None,
) -> list[str] | None:
    """Build the argv that dispatches `next_phase` in the background.

    Returns None if a required binding cannot be resolved from /tmp;
    callers degrade to a hard-gate in that case.
    """
    workdir = _resolve_workdir(latest=latest)
    if workdir is None:
        return None
    transcript = _resolve_user_request_transcript(tmp_root=tmp_root)
    impl_plan = _resolve_implementation_plan_path(tmp_root=tmp_root, latest=latest)
    run_phase_py = repo_root / "orchestrator" / "run_phase.py"
    subagents = repo_root / "subagents"

    if next_phase == "planning-edit":
        if impl_plan is None or transcript is None:
            return None
        return [
            sys.executable,
            str(run_phase_py),
            "run",
            "--phase",
            "planning-edit",
            "--template",
            str(subagents / "prompt-planning-edit.md"),
            "--workdir",
            str(workdir),
            "--set",
            f"WORKTREE_PATH={workdir}",
            "--set",
            f"IMPLEMENTATION_PLAN_PATH={impl_plan}",
            "--set-file",
            f"USER_REQUEST_TRANSCRIPT={transcript}",
            "--require-nonempty-tag",
            "task_input_json",
            "--backend",
            "claude",
        ]

    if next_phase == "test-plan":
        if impl_plan is None or transcript is None:
            return None
        approved = _resolve_approved_test_strategy(tmp_root=tmp_root)
        if approved is None:
            # Without an approved-strategy file, test-plan dispatch will
            # fail validation. Surface that to the caller so it can
            # gate-stop instead of silently mis-dispatching.
            return None
        return [
            sys.executable,
            str(run_phase_py),
            "run",
            "--phase",
            "test-plan",
            "--template",
            str(subagents / "prompt-test-plan.md"),
            "--workdir",
            str(workdir),
            "--set",
            f"WORKTREE_PATH={workdir}",
            "--set",
            f"IMPLEMENTATION_PLAN_PATH={impl_plan}",
            "--set-file",
            f"APPROVED_TEST_STRATEGY={approved}",
            "--set-file",
            f"USER_REQUEST_TRANSCRIPT={transcript}",
            "--require-nonempty-tag",
            "approved_test_strategy",
            "--backend",
            "claude",
        ]

    if next_phase == "executing":
        test_plan = _resolve_test_plan_path(tmp_root=tmp_root)
        if impl_plan is None or test_plan is None:
            return None
        return [
            sys.executable,
            str(run_phase_py),
            "run-sequence",
            "--phase",
            "executing",
            "--steps",
            "load,next-task,next-task,next-task,next-task,next-task,next-task,next-task,next-task,next-task,next-task,finalize",
            "--template-dir",
            str(subagents),
            "--workdir",
            str(workdir),
            "--set",
            f"WORKTREE_PATH={workdir}",
            "--set",
            f"IMPLEMENTATION_PLAN_PATH={impl_plan}",
            "--set",
            f"TEST_PLAN_PATH={test_plan}",
            "--short-circuit-on-sentinel",
            "next-task",
            "--backend",
            "claude",
        ]

    if next_phase == "executing-fix":
        test_plan = _resolve_test_plan_path(tmp_root=tmp_root)
        if impl_plan is None or test_plan is None or observations_path is None:
            return None
        return [
            sys.executable,
            str(run_phase_py),
            "run",
            "--phase",
            "executing",
            "--template",
            str(subagents / "prompt-executing.md"),
            "--workdir",
            str(workdir),
            "--set",
            f"WORKTREE_PATH={workdir}",
            "--set",
            f"IMPLEMENTATION_PLAN_PATH={impl_plan}",
            "--set",
            f"TEST_PLAN_PATH={test_plan}",
            "--set-file",
            f"POST_IMPLEMENTATION_REVIEW_OBSERVATIONS_JSON={observations_path}",
            "--ignore-tag-for-placeholders",
            "post_implementation_review_observations_json",
            "--backend",
            "claude",
        ]

    if next_phase == "post-implementation-review":
        test_plan = _resolve_test_plan_path(tmp_root=tmp_root)
        if impl_plan is None or test_plan is None:
            return None
        return [
            sys.executable,
            str(run_phase_py),
            "run",
            "--phase",
            "post-implementation-review",
            "--template",
            str(subagents / "prompt-post-impl-review.md"),
            "--workdir",
            str(workdir),
            "--set",
            f"WORKTREE_PATH={workdir}",
            "--set",
            f"IMPLEMENTATION_PLAN_PATH={impl_plan}",
            "--set",
            f"TEST_PLAN_PATH={test_plan}",
            "--backend",
            "claude",
        ]

    return None


def _spawn_background(argv: list[str], log_dir: Path) -> int:
    """Spawn argv in the background. Returns the child PID. The child's
    stdout/stderr are redirected to log files under log_dir so the next
    pulse tick can read them via lifesigns."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "pulse-dispatch.stdout"
    stderr_path = log_dir / "pulse-dispatch.stderr"
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=open(stdout_path, "wb"),
        stderr=open(stderr_path, "wb"),
        start_new_session=True,
    )
    return proc.pid


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_action(action: PulseAction, summary: DispatchSummary | None) -> str:
    """Structured one-screen summary for the user reading /loop history."""
    lines: list[str] = []
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines.append(f"[trycycle-pulse {timestamp}] kind={action.kind}")
    if summary is not None:
        lines.append(f"  phase: {summary.phase}")
        lines.append(f"  artifacts: {summary.artifacts_dir}")
    lines.append(f"  reason: {action.reason}")
    if action.kind in {"hard_gate", "escalate"}:
        lines.append("")
        lines.append(STOP_LOOP_BANNER)
        lines.append(
            "Send a stop message to your trycycle session (e.g. 'stop the loop') "
            "and address the gate above before resuming."
        )
    if action.next_phase:
        lines.append(f"  next-phase: {action.next_phase}")
    if action.diagnostics:
        for key, value in action.diagnostics.items():
            if isinstance(value, dict) or isinstance(value, list):
                value = json.dumps(value, sort_keys=True)
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "trycycle-pulse: one-tick liveness check + auto-advance for "
            "long-running trycycle phases. Designed to be invoked by "
            "Claude Code's /loop skill."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help="Path to the trycycle source root (defaults to this script's parent).",
    )
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=Path("/tmp"),
        help="Where to look for trycycle-{phase,seq}-* dispatch dirs (default /tmp).",
    )
    parser.add_argument(
        "--threshold-seconds",
        type=int,
        default=300,
        help="Liveness threshold passed to lifesigns.py check-fallback.",
    )
    parser.add_argument(
        "--no-advance",
        action="store_true",
        help=(
            "Decide and report only; do not actually dispatch the next phase."
            " Useful for dry-running pulse against an existing artifacts tree."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary = _newest_dispatch_dir(args.tmp_root)
    if summary is not None:
        summary.dispatch_dir = _resolve_dispatch_dir(summary)
        summary.result = _load_result_json(summary)

    lifesigns: dict[str, Any] | None = None
    if summary is not None and summary.dispatch_dir is not None:
        try:
            lifesigns = _run_lifesigns(
                repo_root=args.repo_root,
                dispatch_dir=summary.dispatch_dir,
                threshold_seconds=args.threshold_seconds,
            )
        except RuntimeError as exc:
            print(f"[trycycle-pulse] lifesigns failed: {exc}", file=sys.stderr)
            lifesigns = None

    reply_text = _read_reply_text(summary) if summary is not None else ""
    action = decide_action(
        summary=summary,
        lifesigns=lifesigns,
        reply_text=reply_text,
        planning_edit_round=_detect_planning_edit_round(args.tmp_root),
        review_round=_detect_review_round(args.tmp_root),
    )

    print(format_action(action, summary))

    if action.kind == "advance" and action.next_phase and summary is not None:
        observations_path: Path | None = None
        diag = action.diagnostics or {}
        if diag.get("observations_path"):
            observations_path = Path(diag["observations_path"])

        argv = build_advance_command(
            repo_root=args.repo_root,
            next_phase=action.next_phase,
            latest=summary,
            tmp_root=args.tmp_root,
            observations_path=observations_path,
        )
        if argv is None:
            print(
                f"  hard-gate: required binding(s) for {action.next_phase} "
                "could not be resolved from /tmp; manual dispatch needed.",
                file=sys.stderr,
            )
            print(STOP_LOOP_BANNER)
            print(
                "Send a stop message to your trycycle session and dispatch "
                f"{action.next_phase} manually."
            )
            return 0
        if args.no_advance:
            print("  dry-run argv:", " ".join(shlex.quote(a) for a in argv))
        else:
            log_dir = args.tmp_root / "trycycle-pulse-dispatch-logs"
            try:
                pid = _spawn_background(argv, log_dir)
                print(f"  dispatched: pid={pid} logs={log_dir}")
                print(
                    f"  argv: {' '.join(shlex.quote(a) for a in argv)}"
                )
            except OSError as exc:
                print(f"  dispatch failed: {exc}", file=sys.stderr)
                print(STOP_LOOP_BANNER)
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
