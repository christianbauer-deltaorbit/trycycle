"""lifesigns subprocess-probe tests.

Background: in the downstream meshing-module session, lifesigns
false-positively reported `should_escalate=true` against a long-running
fallback-runner step where the spawned `claude` subprocess was healthy
but had not yet flushed output. Cause: `claude -p --output-format=json`
buffers its result until completion, so events.jsonl/stdout.txt/stderr.txt
all stop being touched after `process_spawned` until the subprocess
exits. Fix: read the recorded PID and probe `os.kill(pid, 0)`.

Tests use unittest.mock to control `_pid_is_alive` and `_read_proc_cmdline`
so cases don't depend on real PIDs or the host's /proc layout.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _seed_artifacts(
    artifacts_dir: Path,
    *,
    mtime_seconds_ago: float,
    events: list[dict] | None = None,
) -> None:
    """Build an artifacts dir whose signal files have stale mtimes and
    whose events.jsonl carries the supplied event lines."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target = time.time() - mtime_seconds_ago
    for name in ("events.jsonl", "stdout.txt", "stderr.txt"):
        path = artifacts_dir / name
        path.write_text("", encoding="utf-8")
    if events:
        events_path = artifacts_dir / "events.jsonl"
        events_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
    # Set mtimes AFTER writes so they reflect the simulated activity gap.
    for name in ("events.jsonl", "stdout.txt", "stderr.txt"):
        os.utime(artifacts_dir / name, (target, target))


class LifesignsSubprocessProbeTests(unittest.TestCase):
    """Six scenarios per the spec, plus PID-reuse defense."""

    def test_stale_mtimes_with_live_pid_overrides_to_alive(self) -> None:
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            command = ["/opt/node22/bin/claude", "-p", "--session-id", "abc"]
            _seed_artifacts(
                art,
                mtime_seconds_ago=2700,  # 45 min — well past the 5 min default
                events=[
                    {
                        "event": "phase_start",
                        "phase": "executing-next-task",
                        "backend": "claude",
                    },
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": command,
                        "pid": 12345,
                    },
                ],
            )

            with mock.patch.object(
                lifesigns, "_pid_is_alive", return_value=True
            ), mock.patch.object(
                lifesigns, "_read_proc_cmdline", return_value=command
            ):
                report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertFalse(report["should_escalate"])
            self.assertIn("12345", report["reason"])
            self.assertIn("buffers output until completion", report["reason"])
            probe = report["subprocess_probe"]
            self.assertEqual(probe["pid"], 12345)
            self.assertTrue(probe["alive"])

    def test_stale_mtimes_with_dead_pid_keeps_escalation(self) -> None:
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_artifacts(
                art,
                mtime_seconds_ago=2700,
                events=[
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": ["/opt/node22/bin/claude", "-p"],
                        "pid": 99999,
                    },
                ],
            )

            with mock.patch.object(lifesigns, "_pid_is_alive", return_value=False):
                report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertIn("no longer exists", report["reason"])
            self.assertFalse(report["subprocess_probe"]["alive"])

    def test_stale_mtimes_no_process_spawned_event_falls_back_to_mtime(self) -> None:
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_artifacts(
                art,
                mtime_seconds_ago=2700,
                events=[
                    {"event": "phase_start", "phase": "smoke", "backend": "claude"}
                ],
            )

            report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertIn("no process_spawned event recorded", report["reason"])
            self.assertNotIn("subprocess_probe", report)

    def test_stale_mtimes_process_spawned_lacks_pid_falls_back_to_mtime(
        self,
    ) -> None:
        """Older runner versions emitted process_spawned without `pid`."""
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_artifacts(
                art,
                mtime_seconds_ago=2700,
                events=[
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": ["/opt/node22/bin/claude", "-p"],
                        # no pid field
                    },
                ],
            )

            report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertIn("lacks pid", report["reason"])
            self.assertIn("older runner version", report["reason"])

    def test_no_events_jsonl_yields_no_signal_paths_exist(self) -> None:
        """When events.jsonl is missing entirely, the existing
        no_signal_paths_exist reason wins — the subagent never started."""
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            art.mkdir()
            # don't seed any files

            report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertEqual(report["reason"], "no_signal_paths_exist")
            self.assertNotIn("subprocess_probe", report)

    def test_pid_alive_but_cmdline_mismatch_treated_as_pid_reuse(self) -> None:
        """Defense against PID reuse: a stale recorded command vs. a
        live process with a different argv means the subagent has
        already exited and the PID has been recycled."""
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            recorded = ["/opt/node22/bin/claude", "-p", "--session-id", "old"]
            actual = ["/usr/bin/python3", "-c", "import time; time.sleep(60)"]
            _seed_artifacts(
                art,
                mtime_seconds_ago=2700,
                events=[
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": recorded,
                        "pid": 4242,
                    },
                ],
            )

            with mock.patch.object(
                lifesigns, "_pid_is_alive", return_value=True
            ), mock.patch.object(
                lifesigns, "_read_proc_cmdline", return_value=actual
            ):
                report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertIn("PID likely reused", report["reason"])

    def test_pid_alive_proc_unavailable_trusts_kill_probe(self) -> None:
        """On platforms where /proc isn't readable (Mac, Windows),
        _read_proc_cmdline returns None. We must trust os.kill alone
        rather than failing closed."""
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_artifacts(
                art,
                mtime_seconds_ago=2700,
                events=[
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": ["/opt/node22/bin/claude", "-p"],
                        "pid": 31337,
                    },
                ],
            )

            with mock.patch.object(
                lifesigns, "_pid_is_alive", return_value=True
            ), mock.patch.object(
                lifesigns, "_read_proc_cmdline", return_value=None
            ):
                report = lifesigns.check_fallback(art, threshold_seconds=300)

            self.assertFalse(report["should_escalate"])
            self.assertIn("31337", report["reason"])
            self.assertIn(
                "buffers output until completion", report["reason"]
            )

    def test_recent_mtimes_skip_subprocess_probe_entirely(self) -> None:
        """When mtimes are fresh, lifesigns must not bother with the
        probe — preserves the existing fast path for healthy runs."""
        from orchestrator import lifesigns

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_artifacts(
                art,
                mtime_seconds_ago=10,
                events=[
                    {
                        "event": "process_spawned",
                        "backend": "claude",
                        "command": ["/opt/node22/bin/claude", "-p"],
                        "pid": 5555,
                    },
                ],
            )

            with mock.patch.object(
                lifesigns, "_pid_is_alive"
            ) as patched_alive:
                report = lifesigns.check_fallback(art, threshold_seconds=300)
                patched_alive.assert_not_called()

            self.assertFalse(report["should_escalate"])
            self.assertNotIn("subprocess_probe", report)


class ReadRecentProcessSpawnedTests(unittest.TestCase):
    def test_picks_last_process_spawned_when_multiple_dispatches(self) -> None:
        from orchestrator.lifesigns import _read_recent_process_spawned

        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"event": "process_spawned", "pid": 100, "command": ["a"]}
                        ),
                        json.dumps({"event": "process_exit", "exit_code": 0}),
                        json.dumps(
                            {"event": "process_spawned", "pid": 200, "command": ["b"]}
                        ),
                        json.dumps({"event": "phase_resume", "phase": "executing"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            recent = _read_recent_process_spawned(events)
            self.assertIsNotNone(recent)
            self.assertEqual(recent["pid"], 200)

    def test_returns_none_for_missing_file(self) -> None:
        from orchestrator.lifesigns import _read_recent_process_spawned

        self.assertIsNone(_read_recent_process_spawned(Path("/tmp/no-such-file")))

    def test_skips_malformed_lines(self) -> None:
        from orchestrator.lifesigns import _read_recent_process_spawned

        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            events.write_text(
                "not json\n"
                + json.dumps(
                    {"event": "process_spawned", "pid": 7, "command": []}
                )
                + "\n[also malformed\n",
                encoding="utf-8",
            )
            recent = _read_recent_process_spawned(events)
            self.assertIsNotNone(recent)
            self.assertEqual(recent["pid"], 7)


class PidLivenessHelperTests(unittest.TestCase):
    def test_pid_is_alive_returns_true_for_self(self) -> None:
        from orchestrator.lifesigns import _pid_is_alive

        self.assertTrue(_pid_is_alive(os.getpid()))

    def test_pid_is_alive_returns_false_for_max_pid(self) -> None:
        """4194303 is the upper bound on Linux pids; effectively never
        in use on a fresh process tree. The test tolerates either
        ProcessLookupError or PermissionError gracefully — what we
        guarantee is that a non-existent pid yields False, not an
        unhandled exception."""
        from orchestrator.lifesigns import _pid_is_alive

        # Try a sequence of unlikely-to-exist pids; at least one should
        # be free and yield False.
        candidates = [4_194_303, 4_194_302, 999_999_999]
        results = [_pid_is_alive(p) for p in candidates]
        self.assertIn(False, results)


if __name__ == "__main__":
    unittest.main()
