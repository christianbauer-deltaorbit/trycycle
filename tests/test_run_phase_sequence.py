from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PHASE = REPO_ROOT / "orchestrator" / "run_phase.py"


def _write_fake_claude_binary(bin_dir: Path) -> Path:
    """Fake `claude` that advertises --allowedTools and executes test
    directives embedded in the rendered prompt it receives on stdin.

    Directives (each on its own line in the rendered prompt):
      FAKE_APPEND_TO: <path> CONTENT: <one-line string>
      FAKE_REPLY: <one-line string>
      FAKE_EXIT: <int>
    """
    claude_path = bin_dir / "claude"
    claude_path.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import re, sys

            if "--help" in sys.argv:
                sys.stdout.write(
                    "-p, --print\\n"
                    "--output-format\\n"
                    "--resume\\n"
                    "--session-id\\n"
                    "--allowedTools\\n"
                )
                raise SystemExit(0)

            prompt = ""
            try:
                prompt = sys.stdin.read()
            except Exception:
                pass

            for m in re.finditer(
                r'^FAKE_APPEND_TO:\\s*(\\S+)\\s+CONTENT:\\s*(.*)$',
                prompt,
                re.MULTILINE,
            ):
                path, content = m.group(1), m.group(2)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(content + "\\n")

            reply_m = re.search(r'^FAKE_REPLY:\\s*(.*)$', prompt, re.MULTILINE)
            exit_m = re.search(r'^FAKE_EXIT:\\s*(\\d+)$', prompt, re.MULTILINE)

            if reply_m:
                sys.stdout.write(reply_m.group(1) + "\\n")

            raise SystemExit(int(exit_m.group(1)) if exit_m else 0)
            """
        ),
        encoding="utf-8",
    )
    claude_path.chmod(0o755)
    return claude_path


def _write_step_template(template_path: Path, *, directives: str = "") -> None:
    """Minimal rendered template that includes PHASE_STATE_PATH binding and
    any fake-claude directives verbatim."""
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(
        "You are a test micro-step subagent.\n"
        "\n"
        f"PHASE_STATE_PATH={{PHASE_STATE_PATH}}\n"
        "\n"
        f"{directives}\n",
        encoding="utf-8",
    )


class RunSequenceTests(unittest.TestCase):
    def run_phase(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            [sys.executable, str(RUN_PHASE), *args],
            text=True,
            capture_output=True,
            check=False,
            env=merged_env,
            cwd=cwd,
        )

    # --- happy path ---

    def test_three_step_sequence_all_succeed_and_scratch_accumulates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"
            _write_fake_claude_binary(bin_dir)

            _write_step_template(
                template_dir / "prompt-testphase-alpha.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: alpha-wrote\n"
                    "FAKE_REPLY: alpha-done\n"
                ),
            )
            _write_step_template(
                template_dir / "prompt-testphase-beta.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: beta-wrote\n"
                    "FAKE_REPLY: beta-done\n"
                ),
            )
            _write_step_template(
                template_dir / "prompt-testphase-gamma.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: gamma-wrote\n"
                    "FAKE_REPLY: gamma-done\n"
                ),
            )

            result = self.run_phase(
                "run-sequence",
                "--phase",
                "testphase",
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
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual([s["step"] for s in payload["steps"]], ["alpha", "beta", "gamma"])
            for step in payload["steps"]:
                self.assertEqual(step["status"], "ok", step)

            scratch_body = Path(payload["phase_state_path"]).read_text(encoding="utf-8")
            self.assertIn("alpha-wrote", scratch_body)
            self.assertIn("beta-wrote", scratch_body)
            self.assertIn("gamma-wrote", scratch_body)

    # --- mid-sequence failure ---

    def test_mid_sequence_failure_stops_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"
            _write_fake_claude_binary(bin_dir)

            _write_step_template(
                template_dir / "prompt-testphase-alpha.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: alpha-ok\n"
                    "FAKE_REPLY: alpha-done\n"
                ),
            )
            _write_step_template(
                template_dir / "prompt-testphase-beta.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: beta-ran-then-failed\n"
                    "FAKE_EXIT: 1\n"
                ),
            )
            _write_step_template(
                template_dir / "prompt-testphase-gamma.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: gamma-should-not-run\n"
                    "FAKE_REPLY: gamma-done\n"
                ),
            )

            result = self.run_phase(
                "run-sequence",
                "--phase",
                "testphase",
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
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "escalate_to_user")
            self.assertEqual([s["step"] for s in payload["steps"]], ["alpha", "beta"])
            self.assertEqual(payload["steps"][0]["status"], "ok")
            self.assertEqual(payload["steps"][1]["status"], "escalate_to_user")

            scratch_body = Path(payload["phase_state_path"]).read_text(encoding="utf-8")
            self.assertIn("alpha-ok", scratch_body)
            self.assertIn("beta-ran-then-failed", scratch_body)
            self.assertNotIn("gamma-should-not-run", scratch_body)

    # --- single-shot ---

    def test_single_shot_runs_exactly_one_step_and_seeds_phase_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            artifacts_dir = tmp_path / "artifacts"
            _write_fake_claude_binary(bin_dir)

            single_template = tmp_path / "one-shot.md"
            # Single-shot dispatch uses the phase name as-is (planning-initial
            # here), so the rendered prompt must satisfy the heavy-phase
            # heartbeat check. Include the Streaming discipline section.
            single_template.write_text(
                "# single-shot template\n"
                "\n"
                "## Streaming discipline\n"
                "\n"
                "Heartbeat every ~90s.\n"
                "\n"
                "PHASE_STATE_PATH={PHASE_STATE_PATH}\n"
                "\n"
                "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: single-shot-wrote\n"
                "FAKE_REPLY: single-shot-done\n",
                encoding="utf-8",
            )

            result = self.run_phase(
                "run-sequence",
                "--phase",
                "planning-initial",
                "--single-shot",
                str(single_template),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(len(payload["steps"]), 1)
            self.assertEqual(payload["steps"][0]["step"], "single-shot")
            self.assertEqual(payload["steps"][0]["status"], "ok")

            scratch_path = Path(payload["phase_state_path"])
            self.assertTrue(scratch_path.exists())
            self.assertIn("single-shot-wrote", scratch_path.read_text(encoding="utf-8"))

    # --- sentinel short-circuit ---

    def test_sentinel_in_phase_state_short_circuits_matching_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"
            _write_fake_claude_binary(bin_dir)

            # Loop step writes the sentinel on its first invocation.
            _write_step_template(
                template_dir / "prompt-looptest-load.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: load-ran\n"
                    "FAKE_REPLY: load-done\n"
                ),
            )
            _write_step_template(
                template_dir / "prompt-looptest-next.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: next-ran\n"
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: [[TRYCYCLE_SEQUENCE_DONE]]\n"
                    "FAKE_REPLY: next-done\n"
                ),
            )
            _write_step_template(
                template_dir / "prompt-looptest-finalize.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: finalize-ran\n"
                    "FAKE_REPLY: finalize-done\n"
                ),
            )

            result = self.run_phase(
                "run-sequence",
                "--phase",
                "looptest",
                "--steps",
                "load,next,next,next,finalize",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                "--short-circuit-on-sentinel",
                "next",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertTrue(payload["sentinel_seen"])
            statuses = [(s["step"], s["status"]) for s in payload["steps"]]
            # load runs, first next runs (and writes sentinel), subsequent
            # `next` steps are skipped, finalize still runs.
            self.assertEqual(statuses[0], ("load", "ok"))
            self.assertEqual(statuses[1], ("next", "ok"))
            self.assertEqual(statuses[2], ("next", "skipped"))
            self.assertEqual(statuses[3], ("next", "skipped"))
            self.assertEqual(statuses[4], ("finalize", "ok"))

            scratch_body = Path(payload["phase_state_path"]).read_text(encoding="utf-8")
            self.assertIn("load-ran", scratch_body)
            self.assertIn("next-ran", scratch_body)
            self.assertIn("[[TRYCYCLE_SEQUENCE_DONE]]", scratch_body)
            # finalize ran after the sentinel
            self.assertIn("finalize-ran", scratch_body)
            # Only one "next-ran" entry — subsequent `next` steps were skipped
            self.assertEqual(scratch_body.count("next-ran"), 1)

    # --- scratch file lifecycle ---

    def test_scratch_file_created_under_artifacts_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"
            _write_fake_claude_binary(bin_dir)

            _write_step_template(
                template_dir / "prompt-testphase-only.md",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: only-ran\n"
                    "FAKE_REPLY: only-done\n"
                ),
            )

            result = self.run_phase(
                "run-sequence",
                "--phase",
                "testphase",
                "--steps",
                "only",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            expected_scratch = artifacts_dir / "scratch" / "phase-state.md"
            self.assertEqual(Path(payload["phase_state_path"]), expected_scratch)
            self.assertTrue(expected_scratch.exists())
            self.assertIn("only-ran", expected_scratch.read_text(encoding="utf-8"))

    # --- missing template path ---

    def test_missing_step_template_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            template_dir = tmp_path / "templates"
            template_dir.mkdir()
            # don't create any templates

            result = self.run_phase(
                "run-sequence",
                "--phase",
                "testphase",
                "--steps",
                "missing",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--backend",
                "claude",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("prompt-testphase-missing.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
