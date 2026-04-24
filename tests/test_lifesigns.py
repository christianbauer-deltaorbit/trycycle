"""P2 — life-signs gate.

orchestrator/lifesigns.py reads file mtimes (no polling, no busy-loop) and
reports whether a subagent appears alive based on whether anything has
changed within a configurable threshold. Tests use os.utime to set mtimes
deterministically, so they don't depend on real wall-clock latency.
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
LIFESIGNS = REPO_ROOT / "orchestrator" / "lifesigns.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _seed_fallback_artifacts(artifacts_dir: Path, *, mtime_seconds_ago: float) -> None:
    """Create events.jsonl/stdout.txt/stderr.txt under artifacts_dir and set
    their mtimes to (now - mtime_seconds_ago)."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target = time.time() - mtime_seconds_ago
    for name in ("events.jsonl", "stdout.txt", "stderr.txt"):
        path = artifacts_dir / name
        path.write_text("", encoding="utf-8")
        os.utime(path, (target, target))


class LifesignsUnitTests(unittest.TestCase):
    """Direct calls to check_fallback / check_native, no subprocess."""

    def test_healthy_when_recent_activity_under_threshold(self) -> None:
        from orchestrator.lifesigns import check_fallback

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_fallback_artifacts(art, mtime_seconds_ago=10)

            report = check_fallback(art, threshold_seconds=300)

            self.assertFalse(report["should_escalate"])
            self.assertLess(report["last_activity_seconds_ago"], 30)
            self.assertEqual(report["reasonable_threshold_seconds"], 300)

    def test_warn_when_activity_old_but_files_exist(self) -> None:
        """Caller can adjust threshold to model a stricter gate; the helper
        just compares the recorded mtime against the supplied threshold."""
        from orchestrator.lifesigns import check_fallback

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_fallback_artifacts(art, mtime_seconds_ago=120)

            relaxed = check_fallback(art, threshold_seconds=300)
            strict = check_fallback(art, threshold_seconds=60)

            self.assertFalse(relaxed["should_escalate"])
            self.assertTrue(strict["should_escalate"])

    def test_dead_when_threshold_exceeded(self) -> None:
        from orchestrator.lifesigns import check_fallback

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_fallback_artifacts(art, mtime_seconds_ago=4_800)  # 80 min

            report = check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertGreater(report["last_activity_seconds_ago"], 4_500)

    def test_no_files_means_should_escalate(self) -> None:
        """Subagent has not produced any output yet — treat as dead until
        the first signal lands."""
        from orchestrator.lifesigns import check_fallback

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            art.mkdir()

            report = check_fallback(art, threshold_seconds=300)

            self.assertTrue(report["should_escalate"])
            self.assertIsNone(report["last_activity_seconds_ago"])
            self.assertEqual(report["reason"], "no_signal_paths_exist")

    def test_native_mode_reads_single_transcript_mtime(self) -> None:
        from orchestrator.lifesigns import check_native

        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "agent.jsonl"
            transcript.write_text("", encoding="utf-8")
            target = time.time() - 30
            os.utime(transcript, (target, target))

            report = check_native(transcript, threshold_seconds=60)

            self.assertFalse(report["should_escalate"])
            self.assertLess(report["last_activity_seconds_ago"], 60)


class LifesignsCliTests(unittest.TestCase):
    """End-to-end CLI invocations to verify JSON output shape."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LIFESIGNS), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_check_fallback_emits_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_fallback_artifacts(art, mtime_seconds_ago=5)

            result = self.run_cli(
                "--threshold-seconds", "60",
                "check-fallback",
                "--artifacts-dir", str(art),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("last_activity_seconds_ago", payload)
            self.assertIn("checked_paths", payload)
            self.assertEqual(payload["reasonable_threshold_seconds"], 60)
            self.assertFalse(payload["should_escalate"])

    def test_cli_check_native_with_old_mtime_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "agent.jsonl"
            transcript.write_text("", encoding="utf-8")
            target = time.time() - 4_800
            os.utime(transcript, (target, target))

            result = self.run_cli(
                "--threshold-seconds", "300",
                "check-native",
                "--transcript-file", str(transcript),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["should_escalate"])
            self.assertGreater(payload["last_activity_seconds_ago"], 4_500)

    def test_cli_uses_default_threshold_when_unspecified(self) -> None:
        from orchestrator.lifesigns import DEFAULT_THRESHOLD_SECONDS

        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "art"
            _seed_fallback_artifacts(art, mtime_seconds_ago=5)

            result = self.run_cli(
                "check-fallback", "--artifacts-dir", str(art),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(
                payload["reasonable_threshold_seconds"], DEFAULT_THRESHOLD_SECONDS
            )


if __name__ == "__main__":
    unittest.main()
