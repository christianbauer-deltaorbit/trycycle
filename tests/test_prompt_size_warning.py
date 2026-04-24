"""P4 — run_phase.py prepare prompt-size warning.

run_phase.py prepare logs a warning to stderr when the rendered prompt
exceeds 100KB and errors out when --max-prompt-bytes is set and the
prompt exceeds it. The threshold matches the empirical observation that
oversized prompts predictably correlate with stream-idle failures.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PHASE = REPO_ROOT / "orchestrator" / "run_phase.py"


def _make_template(template_path: Path, *, body_bytes: int) -> None:
    """Create a template that renders to approximately `body_bytes` bytes
    (give or take the surrounding scaffold)."""
    template_path.parent.mkdir(parents=True, exist_ok=True)
    filler = "x" * body_bytes
    template_path.write_text(
        f"# test template\n\nWork in {{WORKTREE_PATH}}\n\n{filler}\n",
        encoding="utf-8",
    )


def _run_prepare(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUN_PHASE), "prepare", *args],
        text=True,
        capture_output=True,
        check=False,
    )


class PromptSizeUnderThreshold(unittest.TestCase):
    def test_small_prompt_no_warning_no_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template = tmp_path / "tiny.md"
            _make_template(template, body_bytes=100)
            artifacts = tmp_path / "art"

            result = _run_prepare(
                "--phase", "smoke",
                "--template", str(template),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts),
                "--set", f"WORKTREE_PATH={workdir}",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("warning:", result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["prompt_size"]["verdict"], "ok")
            self.assertLess(payload["prompt_size"]["bytes"], 1_000)


class PromptSizeOverWarningThreshold(unittest.TestCase):
    def test_large_prompt_warns_to_stderr_but_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template = tmp_path / "big.md"
            # Cross the 100KB warning threshold.
            _make_template(template, body_bytes=150_000)
            artifacts = tmp_path / "art"

            result = _run_prepare(
                "--phase", "smoke",
                "--template", str(template),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts),
                "--set", f"WORKTREE_PATH={workdir}",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning:", result.stderr)
            self.assertIn("transcript", result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["prompt_size"]["verdict"], "warn")
            self.assertGreater(payload["prompt_size"]["bytes"], 100_000)


class PromptSizeOverHardLimit(unittest.TestCase):
    def test_max_prompt_bytes_errors_when_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template = tmp_path / "huge.md"
            _make_template(template, body_bytes=50_000)
            artifacts = tmp_path / "art"

            result = _run_prepare(
                "--phase", "smoke",
                "--template", str(template),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts),
                "--set", f"WORKTREE_PATH={workdir}",
                "--max-prompt-bytes", "10000",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exceeding --max-prompt-bytes", result.stderr)

    def test_max_prompt_bytes_under_limit_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template = tmp_path / "small.md"
            _make_template(template, body_bytes=1_000)
            artifacts = tmp_path / "art"

            result = _run_prepare(
                "--phase", "smoke",
                "--template", str(template),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts),
                "--set", f"WORKTREE_PATH={workdir}",
                "--max-prompt-bytes", "10000",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["prompt_size"]["verdict"], "ok")
            self.assertEqual(payload["prompt_size"]["max_bytes"], 10_000)


class PromptSizePayloadShape(unittest.TestCase):
    """The prompt_size payload is part of result.json so coordinators can
    inspect it post-hoc when triaging a slow dispatch."""

    def test_result_json_carries_prompt_size_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template = tmp_path / "t.md"
            _make_template(template, body_bytes=5_000)
            artifacts = tmp_path / "art"

            result = _run_prepare(
                "--phase", "smoke",
                "--template", str(template),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts),
                "--set", f"WORKTREE_PATH={workdir}",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout_payload = json.loads(result.stdout)
            on_disk = json.loads(
                (artifacts / "result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stdout_payload["prompt_size"], on_disk["prompt_size"])
            self.assertEqual(on_disk["prompt_size"]["warning_threshold_bytes"], 100_000)
            self.assertIsNone(on_disk["prompt_size"]["max_bytes"])


if __name__ == "__main__":
    unittest.main()
