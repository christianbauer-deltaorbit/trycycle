"""trycycle-pulse: state machine + discovery tests.

The interesting unit is `decide_action`. It is a pure function over
(latest dispatch, lifesigns payload, reply text, planning-edit round
count, review round count) and it returns a PulseAction. Tests construct
those inputs deterministically — no real subagent runs, no real lifesigns
helper invocations.

End-to-end CLI tests build small fake `/tmp/trycycle-*` trees and assert
the script reads them, classifies them, and (in --no-advance mode) prints
the right argv for the next phase.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PULSE = REPO_ROOT / "orchestrator" / "pulse.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _build_phase_dir(
    tmp_root: Path,
    *,
    phase: str,
    is_sequence: bool = False,
    result: dict | None = None,
    reply_text: str | None = None,
    inputs_transcript: str | None = None,
    workdir: str | None = None,
    activity_seconds_ago: float = 5.0,
) -> Path:
    """Create a fake trycycle dispatch directory under tmp_root."""
    prefix = "trycycle-seq-" if is_sequence else "trycycle-phase-"
    suffix = "abcd1234"
    artifacts = tmp_root / f"{prefix}{phase}-{suffix}"
    if is_sequence:
        step_dir = artifacts / "steps" / "step1"
        step_dir.mkdir(parents=True, exist_ok=True)
        dispatch = step_dir / "dispatch"
        dispatch.mkdir(parents=True, exist_ok=True)
        if reply_text is not None:
            reply_path = dispatch / "reply.txt"
            reply_path.write_text(reply_text, encoding="utf-8")
            (dispatch / "events.jsonl").write_text("", encoding="utf-8")
            (dispatch / "stdout.txt").write_text("", encoding="utf-8")
            (dispatch / "stderr.txt").write_text("", encoding="utf-8")
            now = time.time() - activity_seconds_ago
            for name in ("events.jsonl", "stdout.txt", "stderr.txt", "reply.txt"):
                os.utime(dispatch / name, (now, now))
        if result is not None:
            seq_result = artifacts / "sequence-result.json"
            if "steps" in result:
                pass  # caller-supplied
            else:
                # add a steps[] entry pointing at the synthesized reply
                if reply_text is not None:
                    result = {
                        **result,
                        "steps": [
                            {
                                "step": "step1",
                                "status": result.get("status", "ok"),
                                "dispatch": {"reply_path": str(dispatch / "reply.txt")},
                            }
                        ],
                    }
            seq_result.write_text(
                json.dumps(result), encoding="utf-8"
            )
        if workdir:
            (step_dir / "result.json").write_text(
                json.dumps({"workdir": workdir}), encoding="utf-8"
            )
        if inputs_transcript is not None:
            inputs_dir = step_dir / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)
            (inputs_dir / "USER_REQUEST_TRANSCRIPT.txt").write_text(
                inputs_transcript, encoding="utf-8"
            )
    else:
        dispatch = artifacts / "dispatch"
        dispatch.mkdir(parents=True, exist_ok=True)
        if reply_text is not None:
            (dispatch / "reply.txt").write_text(reply_text, encoding="utf-8")
            (dispatch / "events.jsonl").write_text("", encoding="utf-8")
            (dispatch / "stdout.txt").write_text("", encoding="utf-8")
            (dispatch / "stderr.txt").write_text("", encoding="utf-8")
            now = time.time() - activity_seconds_ago
            for name in ("events.jsonl", "stdout.txt", "stderr.txt", "reply.txt"):
                os.utime(dispatch / name, (now, now))
        if result is not None:
            payload = dict(result)
            if reply_text is not None:
                payload.setdefault("reply_path", str(dispatch / "reply.txt"))
            (dispatch / "result.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
        if workdir:
            (artifacts / "result.json").write_text(
                json.dumps({"workdir": workdir}), encoding="utf-8"
            )
        if inputs_transcript is not None:
            inputs_dir = artifacts / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)
            (inputs_dir / "USER_REQUEST_TRANSCRIPT.txt").write_text(
                inputs_transcript, encoding="utf-8"
            )
    return artifacts


# ---------------------------------------------------------------------------
# Pure state-machine tests
# ---------------------------------------------------------------------------


class DecideActionTests(unittest.TestCase):
    def setUp(self) -> None:
        from orchestrator.pulse import DispatchSummary

        self.DispatchSummary = DispatchSummary

    def _summary(
        self,
        phase: str,
        *,
        is_sequence: bool = False,
        result: dict | None = None,
    ):
        return self.DispatchSummary(
            artifacts_dir=Path(f"/tmp/fake-{phase}"),
            phase=phase,
            is_sequence=is_sequence,
            last_mtime=time.time(),
            result=result,
        )

    def test_no_dispatch_yields_idle(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=None,
            lifesigns=None,
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "idle")
        self.assertIn("no in-flight", action.reason)

    def test_alive_subagent_yields_alive(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("planning-initial"),
            lifesigns={"should_escalate": False, "last_activity_seconds_ago": 12.3},
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "alive")
        self.assertIn("12.3s ago", action.reason)

    def test_test_strategy_phase_is_hard_gate(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary(
                "test-strategy",
                result={"status": "ok"},
            ),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text="strategy proposal",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "hard_gate")
        self.assertIn("user approval", action.reason)

    def test_silent_subagent_with_no_result_escalates(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("planning-initial", result=None),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "escalate")
        self.assertIn("died without recording status", action.reason)

    def test_failure_status_escalates(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary(
                "planning-initial",
                result={"status": "escalate_to_user", "message": "rate-limited"},
            ),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "escalate")
        self.assertIn("rate-limited", action.reason)

    def test_user_decision_required_in_reply_is_hard_gate(self) -> None:
        from orchestrator.pulse import decide_action

        reply = "USER DECISION REQUIRED: pick framework A or B\n\nrationale follows"
        action = decide_action(
            summary=self._summary("planning-initial", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text=reply,
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "hard_gate")
        self.assertIn("USER DECISION REQUIRED", action.reason)

    def test_planning_initial_success_advances_to_planning_edit(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("planning-initial", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text="## Plan verdict\nCREATED\n## Plan path\n/tmp/plan.md",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "advance")
        self.assertEqual(action.next_phase, "planning-edit")

    def test_planning_edit_revised_under_cap_advances_again(self) -> None:
        from orchestrator.pulse import decide_action

        reply = "## Plan verdict\nREVISED\n## Plan path\n/tmp/plan.md"
        action = decide_action(
            summary=self._summary("planning-edit", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text=reply,
            planning_edit_round=2,
            review_round=0,
        )
        self.assertEqual(action.kind, "advance")
        self.assertEqual(action.next_phase, "planning-edit")
        self.assertIn("3/5", action.reason)

    def test_planning_edit_revised_at_cap_is_hard_gate(self) -> None:
        from orchestrator.pulse import decide_action

        reply = "## Plan verdict\nREVISED"
        action = decide_action(
            summary=self._summary("planning-edit", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text=reply,
            planning_edit_round=5,
            review_round=0,
        )
        self.assertEqual(action.kind, "hard_gate")
        self.assertIn("5 rounds", action.reason)

    def test_planning_edit_ready_advances_to_test_plan(self) -> None:
        from orchestrator.pulse import decide_action

        reply = "## Plan verdict\nREADY\n## Plan path\n/tmp/plan.md"
        action = decide_action(
            summary=self._summary("planning-edit", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text=reply,
            planning_edit_round=2,
            review_round=0,
        )
        self.assertEqual(action.kind, "advance")
        self.assertEqual(action.next_phase, "test-plan")

    def test_planning_edit_unrecognised_verdict_is_hard_gate(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("planning-edit", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text="reply with no Plan verdict heading",
            planning_edit_round=2,
            review_round=0,
        )
        self.assertEqual(action.kind, "hard_gate")
        self.assertIn("Plan verdict", action.reason)

    def test_test_plan_with_strategy_changes_is_hard_gate(self) -> None:
        from orchestrator.pulse import decide_action

        reply = (
            "## Strategy changes requiring user approval\n\n"
            "We need a paid API key.\n\n"
            "## Test plan path\n/tmp/test-plan.md"
        )
        action = decide_action(
            summary=self._summary("test-plan", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text=reply,
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "hard_gate")
        self.assertIn("Strategy changes", action.reason)

    def test_test_plan_clean_advances_to_executing(self) -> None:
        from orchestrator.pulse import decide_action

        reply = "## Test plan path\n/tmp/test-plan.md\n## Commit\nabc123"
        action = decide_action(
            summary=self._summary("test-plan", result={"status": "ok"}),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text=reply,
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "advance")
        self.assertEqual(action.next_phase, "executing")

    def test_executing_success_advances_to_review(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary(
                "executing", is_sequence=True, result={"status": "ok"}
            ),
            lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
            reply_text="## Implementation summary\nlanded\n",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "advance")
        self.assertEqual(action.next_phase, "post-implementation-review")


class ReviewPathTests(unittest.TestCase):
    """post-implementation-review depends on the extracted observations
    artifact, which is on disk under known paths. Build a fake one and
    assert pulse classifies the round correctly."""

    def setUp(self) -> None:
        from orchestrator.pulse import DispatchSummary

        self.DispatchSummary = DispatchSummary

    def _make_summary_with_observations(
        self, tmp_path: Path, *, blocking_count: int
    ):
        artifacts = tmp_path / "trycycle-phase-post-implementation-review-aaaa"
        artifacts.mkdir(parents=True, exist_ok=True)
        observations = artifacts / "review-observations.json"
        observations.write_text(
            json.dumps(
                {
                    "status": "issues_found"
                    if blocking_count
                    else "no_issues",
                    "observations": [],
                    "issue_count": blocking_count,
                    "blocking_issue_count": blocking_count,
                }
            ),
            encoding="utf-8",
        )
        return self.DispatchSummary(
            artifacts_dir=artifacts,
            phase="post-implementation-review",
            is_sequence=False,
            last_mtime=time.time(),
            result={"status": "ok"},
        )

    def test_review_zero_blocking_is_hard_gate_at_finish(self) -> None:
        from orchestrator.pulse import decide_action

        with tempfile.TemporaryDirectory() as tmp:
            summary = self._make_summary_with_observations(
                Path(tmp), blocking_count=0
            )
            action = decide_action(
                summary=summary,
                lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
                reply_text="<review_observations_json>...</review_observations_json>",
                planning_edit_round=0,
                review_round=1,
            )
            self.assertEqual(action.kind, "hard_gate")
            self.assertIn("0 blocking issues", action.reason)
            self.assertIn("finish", action.reason)

    def test_review_with_blocking_under_cap_advances_to_executing_fix(self) -> None:
        from orchestrator.pulse import decide_action

        with tempfile.TemporaryDirectory() as tmp:
            summary = self._make_summary_with_observations(
                Path(tmp), blocking_count=3
            )
            action = decide_action(
                summary=summary,
                lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
                reply_text="reviewer reply",
                planning_edit_round=0,
                review_round=1,
            )
            self.assertEqual(action.kind, "advance")
            self.assertEqual(action.next_phase, "executing-fix")
            self.assertIn("3 blocking", action.reason)

    def test_review_with_blocking_at_cap_is_hard_gate(self) -> None:
        from orchestrator.pulse import decide_action

        with tempfile.TemporaryDirectory() as tmp:
            summary = self._make_summary_with_observations(
                Path(tmp), blocking_count=2
            )
            action = decide_action(
                summary=summary,
                lifesigns={"should_escalate": True, "last_activity_seconds_ago": 600},
                reply_text="reviewer reply",
                planning_edit_round=0,
                review_round=8,
            )
            self.assertEqual(action.kind, "hard_gate")
            self.assertIn("8 rounds", action.reason)


# ---------------------------------------------------------------------------
# CLI / discovery integration
# ---------------------------------------------------------------------------


def _run_pulse(*args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(PULSE), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged,
    )


class PulseCliTests(unittest.TestCase):
    def test_empty_tmp_root_yields_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_pulse("--tmp-root", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("kind=idle", result.stdout)
            self.assertIn("no in-flight", result.stdout)

    def test_alive_dispatch_reports_status_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _build_phase_dir(
                tmp_path,
                phase="planning-initial",
                reply_text="",
                activity_seconds_ago=5.0,
            )
            result = _run_pulse(
                "--tmp-root", tmp,
                "--threshold-seconds", "300",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("kind=alive", result.stdout)
            self.assertNotIn("STOP THE LOOP", result.stdout)

    def test_silent_dispatch_with_failure_result_emits_stop_banner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _build_phase_dir(
                tmp_path,
                phase="planning-initial",
                reply_text="failure body",
                result={"status": "escalate_to_user", "message": "rate-limited"},
                activity_seconds_ago=600.0,
            )
            result = _run_pulse(
                "--tmp-root", tmp,
                "--threshold-seconds", "60",
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("kind=escalate", result.stdout)
            self.assertIn("STOP THE LOOP", result.stdout)
            self.assertIn("rate-limited", result.stdout)

    def test_silent_dispatch_with_strategy_changes_emits_hard_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = (
                "## Strategy changes requiring user approval\n\n"
                "Need a paid API.\n\n## Test plan path\n/tmp/tp.md"
            )
            _build_phase_dir(
                tmp_path,
                phase="test-plan",
                reply_text=reply,
                result={"status": "ok"},
                activity_seconds_ago=600.0,
            )
            result = _run_pulse(
                "--tmp-root", tmp,
                "--threshold-seconds", "60",
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("kind=hard_gate", result.stdout)
            self.assertIn("STOP THE LOOP", result.stdout)
            self.assertIn("Strategy changes", result.stdout)

    def test_advance_dry_run_prints_argv_without_dispatching(self) -> None:
        """No-advance mode: pulse decides to advance, but only prints the
        argv it WOULD have run. No background process spawned."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            plan_path = tmp_path / "plan.md"
            plan_path.write_text("# plan", encoding="utf-8")

            _build_phase_dir(
                tmp_path,
                phase="planning-initial",
                reply_text=(
                    f"## Plan verdict\nCREATED\n## Plan path\n{plan_path}"
                ),
                result={"status": "ok"},
                activity_seconds_ago=600.0,
                inputs_transcript='[{"role": "user", "text": "task"}]',
                workdir=str(workdir),
            )

            result = _run_pulse(
                "--tmp-root", tmp,
                "--threshold-seconds", "60",
                "--no-advance",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("kind=advance", result.stdout)
            self.assertIn("next-phase: planning-edit", result.stdout)
            self.assertIn("dry-run argv:", result.stdout)
            self.assertIn("--phase planning-edit", result.stdout)


# ---------------------------------------------------------------------------
# Discovery-helper unit tests
# ---------------------------------------------------------------------------


class DiscoveryHelperTests(unittest.TestCase):
    def test_phase_from_dirname_strips_random_suffix(self) -> None:
        from orchestrator.pulse import _phase_from_dirname

        self.assertEqual(
            _phase_from_dirname("trycycle-phase-planning-initial-abcd1234"),
            ("planning-initial", False),
        )
        self.assertEqual(
            _phase_from_dirname("trycycle-seq-executing-XYZpqr987"),
            ("executing", True),
        )

    def test_phase_from_dirname_returns_none_for_non_matching(self) -> None:
        from orchestrator.pulse import _phase_from_dirname

        self.assertIsNone(_phase_from_dirname("trycycle-other-foo"))

    def test_newest_dispatch_dir_picks_most_recent(self) -> None:
        from orchestrator.pulse import _newest_dispatch_dir

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old = _build_phase_dir(tmp_path, phase="planning-initial")
            time.sleep(0.05)
            new = _build_phase_dir(tmp_path, phase="planning-edit")
            os.utime(old, (time.time() - 1000, time.time() - 1000))

            best = _newest_dispatch_dir(tmp_path)
            self.assertIsNotNone(best)
            self.assertEqual(best.phase, "planning-edit")
            self.assertEqual(best.artifacts_dir, new)


if __name__ == "__main__":
    unittest.main()
