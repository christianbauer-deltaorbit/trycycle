"""Integration test for run-sequence against the real installed `claude` CLI.

This test exercises everything from the `run_phase.py run-sequence` entry
point through `subagent_runner.py run` and the real `claude --help` probe.
It stops short of actually dispatching a model call (uses --dry-run) so
that the test does not depend on network connectivity or API credentials.

Set TRYCYCLE_INTEGRATION_LIVE=1 to additionally attempt a live dispatch
against the real model. The live variant is skipped by default because
it requires valid credentials and consumes quota.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PHASE = REPO_ROOT / "orchestrator" / "run_phase.py"


@unittest.skipUnless(
    shutil.which("claude") is not None,
    "real claude CLI not on PATH; integration test requires it",
)
class DecompositionIntegrationTests(unittest.TestCase):
    """Exercise the real claude CLI through run-sequence end-to-end."""

    def _write_step(self, template_dir: Path, phase: str, step: str) -> None:
        path = template_dir / f"prompt-{phase}-{step}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# integration-{step}\n\n"
            "Write one line to {PHASE_STATE_PATH} via the Write tool:\n"
            f"'integration-{step}-ran'.\n",
            encoding="utf-8",
        )

    def test_dry_run_three_step_sequence_invokes_real_claude_probe(self) -> None:
        """Three-step dry-run against the real claude CLI. Verifies the probe
        reports --allowedTools support and every step dry-runs green."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template_dir = tmp_path / "templates"
            for step in ("alpha", "beta", "gamma"):
                self._write_step(template_dir, "integphase", step)
            artifacts_dir = tmp_path / "artifacts"

            result = subprocess.run(
                [
                    sys.executable,
                    str(RUN_PHASE),
                    "run-sequence",
                    "--phase",
                    "integphase",
                    "--steps",
                    "alpha,beta,gamma",
                    "--template-dir",
                    str(template_dir),
                    "--workdir",
                    str(workdir),
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--backend",
                    "claude",
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(
                [s["step"] for s in payload["steps"]], ["alpha", "beta", "gamma"]
            )
            for step in payload["steps"]:
                self.assertEqual(step["status"], "ok", step)
                probe = step["dispatch"]["probe"]["backends"]["claude"]
                self.assertTrue(probe["available"], probe)
                self.assertTrue(
                    probe["supports_allowed_tools"],
                    "real claude --help must advertise --allowedTools",
                )
                cmd = step["dispatch"]["process"]["command"]
                # Under root (hosted sandbox), root-safety swaps the flag.
                # Under non-root, backward-compat keeps the legacy flag.
                if os.geteuid() == 0:
                    self.assertIn("--allowedTools", cmd)
                    self.assertNotIn("--dangerously-skip-permissions", cmd)
                else:
                    self.assertIn("--dangerously-skip-permissions", cmd)

            # Scratch file exists under the shared artifacts dir.
            scratch = artifacts_dir / "scratch" / "phase-state.md"
            self.assertTrue(scratch.exists())

    @unittest.skipUnless(
        os.environ.get("TRYCYCLE_INTEGRATION_LIVE") == "1",
        "live integration test; set TRYCYCLE_INTEGRATION_LIVE=1 to run",
    )
    def test_live_three_step_sequence_writes_marker_to_scratch(self) -> None:
        """Live version that actually dispatches the model. Each step asks
        the model to append a distinctive marker to phase-state. After the
        three steps the scratch file must contain all three markers."""
        markers = ("alpha-MARKER-42", "beta-MARKER-42", "gamma-MARKER-42")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template_dir = tmp_path / "templates"
            for name, marker in zip(("alpha", "beta", "gamma"), markers):
                path = template_dir / f"prompt-integphase-{name}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "# live integration step\n\n"
                    "## Streaming discipline\n\n"
                    "Write the marker immediately via the Write tool; do not narrate.\n\n"
                    f"Use the Write tool to append this exact line to {{PHASE_STATE_PATH}}:\n"
                    f"{marker}\n\n"
                    "Then reply with a single word: done.\n",
                    encoding="utf-8",
                )

            artifacts_dir = tmp_path / "artifacts"
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUN_PHASE),
                    "run-sequence",
                    "--phase",
                    "integphase",
                    "--steps",
                    "alpha,beta,gamma",
                    "--template-dir",
                    str(template_dir),
                    "--workdir",
                    str(workdir),
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--backend",
                    "claude",
                    "--timeout-seconds",
                    "120",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            scratch = (artifacts_dir / "scratch" / "phase-state.md").read_text(
                encoding="utf-8"
            )
            for marker in markers:
                self.assertIn(marker, scratch, f"missing {marker} in scratch")


if __name__ == "__main__":
    unittest.main()
