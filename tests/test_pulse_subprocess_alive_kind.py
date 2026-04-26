"""pulse: kind=alive_subprocess_active branch.

Layer 1 of the buffered-output false-positive fix. lifesigns now
populates `subprocess_probe.alive=True` when the spawned claude PID is
still running despite stale mtimes. Pulse surfaces that as a separate
kind so users reading /loop history can tell:

  kind=alive                       — mtimes fresh, runner is healthy.
  kind=alive_subprocess_active     — mtimes stale, but the spawned
                                     subprocess is still working
                                     (claude --output-format json
                                     buffers output until completion).
  kind=escalate                    — mtimes stale and subprocess dead.

These tests use the pure-function decide_action and a CLI subprocess
test against a forged events.jsonl + live worker.
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
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PULSE = REPO_ROOT / "orchestrator" / "pulse.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class DecideActionAliveSubprocessActiveTests(unittest.TestCase):
    """Pure decide_action input/output coverage for the new kind."""

    def setUp(self) -> None:
        from orchestrator.pulse import DispatchSummary

        self.DispatchSummary = DispatchSummary

    def _summary(self, phase: str = "executing"):
        return self.DispatchSummary(
            artifacts_dir=Path(f"/tmp/fake-{phase}"),
            phase=phase,
            is_sequence=True,
            last_mtime=time.time(),
            result={"status": "ok"},
        )

    def test_lifesigns_subprocess_alive_yields_alive_subprocess_active(self) -> None:
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("executing"),
            lifesigns={
                "should_escalate": False,
                "last_activity_seconds_ago": 2700.0,
                "reason": (
                    "subprocess 8997 still running, last artifact write "
                    "2700s ago (claude --output-format=json buffers output "
                    "until completion)"
                ),
                "subprocess_probe": {"pid": 8997, "alive": True, "detail": "..."},
            },
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "alive_subprocess_active")
        self.assertIn("8997", action.reason)
        self.assertIn("2700.0s ago", action.reason)
        self.assertIn("buffers until completion", action.reason)

    def test_no_subprocess_probe_field_keeps_plain_alive(self) -> None:
        """Older lifesigns (Layer 1 not yet deployed) lacks the
        subprocess_probe field. Pulse must not regress to alive_subprocess_active
        guesswork — emit plain alive."""
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("executing"),
            lifesigns={
                "should_escalate": False,
                "last_activity_seconds_ago": 12.0,
                # no subprocess_probe key
            },
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "alive")

    def test_subprocess_probe_alive_false_keeps_plain_alive(self) -> None:
        """Lifesigns reports subprocess_probe with alive=False ONLY when
        it's escalating. If should_escalate is False (mtimes fresh),
        subprocess_probe is absent or alive=False is moot — emit plain
        alive."""
        from orchestrator.pulse import decide_action

        action = decide_action(
            summary=self._summary("executing"),
            lifesigns={
                "should_escalate": False,
                "last_activity_seconds_ago": 5.0,
                "subprocess_probe": {"pid": 1, "alive": False, "detail": "..."},
            },
            reply_text="",
            planning_edit_round=0,
            review_round=0,
        )
        self.assertEqual(action.kind, "alive")

    def test_format_action_renders_subprocess_active_kind_clearly(self) -> None:
        from orchestrator.pulse import format_action, PulseAction

        action = PulseAction(
            kind="alive_subprocess_active",
            reason=(
                "executing subprocess pid 8997 still running; "
                "last artifact write 2723.183s ago "
                "(claude --output-format json buffers until completion)"
            ),
            diagnostics={
                "lifesigns": {
                    "subprocess_probe": {"pid": 8997, "alive": True}
                }
            },
        )
        rendered = format_action(action, summary=None)
        self.assertIn("alive_subprocess_active", rendered)
        self.assertIn("pid 8997", rendered)
        self.assertNotIn("STOP THE LOOP", rendered)


class PulseCliWithLiveSubprocessTests(unittest.TestCase):
    """End-to-end: forge an events.jsonl with a real live PID,
    invoke pulse, observe the new kind in stdout."""

    def test_pulse_reports_alive_subprocess_active_against_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tmp_root = tmp_path / "tmproot"
            tmp_root.mkdir()
            dispatch_root = tmp_root / "trycycle-seq-executing-livesubpa"
            steps_dir = dispatch_root / "steps" / "next-task"
            artifacts = steps_dir / "dispatch"
            artifacts.mkdir(parents=True)

            # Spawn a real subprocess we can probe. Using sleep with an
            # absolute path so the cmdline check matches.
            proc = subprocess.Popen(
                ["/usr/bin/sleep", "120"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                # Forge events.jsonl referencing the real PID + matching cmdline.
                events_path = artifacts / "events.jsonl"
                events_path.write_text(
                    json.dumps(
                        {
                            "event": "process_spawned",
                            "backend": "claude",
                            "command": ["/usr/bin/sleep", "120"],
                            "pid": proc.pid,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (artifacts / "stdout.txt").write_text("", encoding="utf-8")
                (artifacts / "stderr.txt").write_text("", encoding="utf-8")
                # sequence-result.json shows status ok so pulse doesn't
                # treat the dispatch as failed.
                (dispatch_root / "sequence-result.json").write_text(
                    json.dumps({"status": "ok", "steps": []}), encoding="utf-8"
                )

                # Backdate every signal to 2700s ago — well past threshold.
                past = time.time() - 2700
                for name in ("events.jsonl", "stdout.txt", "stderr.txt"):
                    os.utime(artifacts / name, (past, past))

                result = subprocess.run(
                    [
                        sys.executable,
                        str(PULSE),
                        "--tmp-root",
                        str(tmp_root),
                        "--threshold-seconds",
                        "300",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                proc.kill()
                proc.wait()

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("kind=alive_subprocess_active", result.stdout)
            self.assertIn(str(proc.pid), result.stdout)
            self.assertIn("buffers until completion", result.stdout)
            self.assertNotIn("STOP THE LOOP", result.stdout)

    def test_pulse_escalates_when_recorded_pid_is_dead(self) -> None:
        """Counter-test: same shape but the recorded PID is dead.
        Pulse falls through to its existing escalate path."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tmp_root = tmp_path / "tmproot"
            tmp_root.mkdir()
            dispatch_root = tmp_root / "trycycle-seq-executing-deadsubpa"
            steps_dir = dispatch_root / "steps" / "next-task"
            artifacts = steps_dir / "dispatch"
            artifacts.mkdir(parents=True)

            # Spawn briefly, capture pid, then kill so probe fails.
            proc = subprocess.Popen(
                ["/usr/bin/sleep", "1"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            dead_pid = proc.pid
            proc.terminate()
            proc.wait()
            # Wait for kernel to fully reap so pid is unambiguously gone.
            for _ in range(20):
                try:
                    os.kill(dead_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)

            events_path = artifacts / "events.jsonl"
            events_path.write_text(
                json.dumps(
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": ["/usr/bin/sleep", "1"],
                        "pid": dead_pid,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (artifacts / "stdout.txt").write_text("", encoding="utf-8")
            (artifacts / "stderr.txt").write_text("", encoding="utf-8")
            # No result.json — pulse will see "silent past threshold and no result"
            # for the sequence root, but the dispatch dir's mtime is what's
            # checked. Lifesigns will say escalate due to dead PID.
            past = time.time() - 2700
            for name in ("events.jsonl", "stdout.txt", "stderr.txt"):
                os.utime(artifacts / name, (past, past))

            result = subprocess.run(
                [
                    sys.executable,
                    str(PULSE),
                    "--tmp-root",
                    str(tmp_root),
                    "--threshold-seconds",
                    "300",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("kind=escalate", result.stdout)
            self.assertIn("STOP THE LOOP", result.stdout)


if __name__ == "__main__":
    unittest.main()
