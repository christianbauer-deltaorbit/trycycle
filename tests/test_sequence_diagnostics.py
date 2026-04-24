"""P5 — sentinel diagnostic at run-sequence end.

run-sequence emits a one-line diagnostic to stderr summarising sentinel
state and repeatable-step counts so a coordinator can tell at a glance
whether the sentinel mechanism worked. The same counts are written to
result.json#diagnostics for postmortem inspection. The diagnostic also
adds a load-bearing warning when every repeatable slot was consumed
without the sentinel ever firing — that's the failure mode the
downstream session hit three times.

Also asserts the prompt-executing-next-task.md template carries an
explicit, prominent sentinel instruction.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_PHASE = REPO_ROOT / "orchestrator" / "run_phase.py"


def _write_fake_claude(bin_dir: Path) -> None:
    (bin_dir / "claude").write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import re, sys

            if "--help" in sys.argv:
                sys.stdout.write(
                    "-p, --print\\n--output-format\\n--resume\\n"
                    "--session-id\\n--allowedTools\\n"
                )
                raise SystemExit(0)

            prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
            for m in re.finditer(
                r'^FAKE_APPEND_TO:\\s*(\\S+)\\s+CONTENT:\\s*(.*)$',
                prompt, re.MULTILINE,
            ):
                with open(m.group(1), "a", encoding="utf-8") as f:
                    f.write(m.group(2) + "\\n")
            reply_m = re.search(r'^FAKE_REPLY:\\s*(.*)$', prompt, re.MULTILINE)
            exit_m = re.search(r'^FAKE_EXIT:\\s*(\\d+)$', prompt, re.MULTILINE)
            if reply_m: sys.stdout.write(reply_m.group(1) + "\\n")
            raise SystemExit(int(exit_m.group(1)) if exit_m else 0)
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "claude").chmod(0o755)


def _write_step(template_dir: Path, *, phase: str, step: str, directives: str) -> None:
    path = template_dir / f"prompt-{phase}-{step}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"PHASE_STATE_PATH={{PHASE_STATE_PATH}}\n\n{directives}\n",
        encoding="utf-8",
    )


def _run_sequence(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(RUN_PHASE), "run-sequence", *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged,
    )


class SentinelDiagnosticTests(unittest.TestCase):
    def test_diagnostic_when_sentinel_fires_partway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "art"

            _write_step(
                template_dir,
                phase="executing",
                step="load",
                directives="FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: load-ran\nFAKE_REPLY: ok",
            )
            _write_step(
                template_dir,
                phase="executing",
                step="next-task",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: next-ran\n"
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: [[TRYCYCLE_SEQUENCE_DONE]]\n"
                    "FAKE_REPLY: ok"
                ),
            )
            _write_step(
                template_dir,
                phase="executing",
                step="finalize",
                directives="FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: finalize-ran\nFAKE_REPLY: ok",
            )

            result = _run_sequence(
                "--phase", "executing",
                "--steps", "load,next-task,next-task,next-task,finalize",
                "--template-dir", str(template_dir),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts_dir),
                "--backend", "claude",
                "--short-circuit-on-sentinel", "next-task",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            # One diagnostic line on stderr.
            diag_lines = [
                line
                for line in result.stderr.splitlines()
                if line.startswith("sequence-diagnostic:")
            ]
            self.assertEqual(len(diag_lines), 1, result.stderr)
            self.assertIn("sentinel_seen=true", diag_lines[0])
            self.assertIn("repeatable_completed=1/3", diag_lines[0])
            self.assertIn("repeatable_skipped=2", diag_lines[0])
            self.assertNotIn("warning:", diag_lines[0])

            # Same counts in the JSON payload.
            payload = json.loads(result.stdout)
            diag = payload["diagnostics"]
            self.assertTrue(diag["sentinel_seen"])
            self.assertEqual(diag["repeatable_total_slots"], 3)
            self.assertEqual(diag["repeatable_completed"], 1)
            self.assertEqual(diag["repeatable_skipped"], 2)
            self.assertEqual(diag["repeatable_failed"], 0)

    def test_diagnostic_warns_when_sentinel_never_fires(self) -> None:
        """Mirrors the downstream-session failure mode: every next-task
        slot is consumed but the sentinel never appears."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "art"

            _write_step(
                template_dir,
                phase="executing",
                step="load",
                directives="FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: load\nFAKE_REPLY: ok",
            )
            # No directive that writes the sentinel anywhere.
            _write_step(
                template_dir,
                phase="executing",
                step="next-task",
                directives=(
                    "FAKE_APPEND_TO: {PHASE_STATE_PATH} CONTENT: next-ran\n"
                    "FAKE_REPLY: ok"
                ),
            )
            _write_step(
                template_dir,
                phase="executing",
                step="finalize",
                directives="FAKE_REPLY: ok",
            )

            result = _run_sequence(
                "--phase", "executing",
                "--steps", "load,next-task,next-task,next-task,finalize",
                "--template-dir", str(template_dir),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts_dir),
                "--backend", "claude",
                "--short-circuit-on-sentinel", "next-task",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)

            diag_lines = [
                line
                for line in result.stderr.splitlines()
                if line.startswith("sequence-diagnostic:")
            ]
            self.assertEqual(len(diag_lines), 1)
            self.assertIn("sentinel_seen=false", diag_lines[0])
            self.assertIn("repeatable_completed=3/3", diag_lines[0])
            self.assertIn("warning:", diag_lines[0])
            self.assertIn("sentinel", diag_lines[0])

            payload = json.loads(result.stdout)
            self.assertFalse(payload["diagnostics"]["sentinel_seen"])
            self.assertEqual(payload["diagnostics"]["repeatable_completed"], 3)
            self.assertEqual(payload["diagnostics"]["repeatable_skipped"], 0)

    def test_diagnostic_for_sequence_with_no_repeatable_steps(self) -> None:
        """A linear sequence (planning-initial style) reports zero
        repeatable totals and no warning."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "art"

            for step in ("survey", "scaffold", "commit"):
                _write_step(
                    template_dir,
                    phase="planning-initial",
                    step=step,
                    directives=f"FAKE_REPLY: {step}-done",
                )

            result = _run_sequence(
                "--phase", "planning-initial",
                "--steps", "survey,scaffold,commit",
                "--template-dir", str(template_dir),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts_dir),
                "--backend", "claude",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            diag_lines = [
                line
                for line in result.stderr.splitlines()
                if line.startswith("sequence-diagnostic:")
            ]
            self.assertEqual(len(diag_lines), 1)
            self.assertIn("repeatable_completed=0/0", diag_lines[0])
            self.assertNotIn("warning:", diag_lines[0])

    def test_diagnostic_payload_in_result_json(self) -> None:
        """Diagnostic block is persisted to sequence-result.json."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "art"

            _write_step(
                template_dir,
                phase="t",
                step="only",
                directives="FAKE_REPLY: ok",
            )

            result = _run_sequence(
                "--phase", "t",
                "--steps", "only",
                "--template-dir", str(template_dir),
                "--workdir", str(workdir),
                "--artifacts-dir", str(artifacts_dir),
                "--backend", "claude",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            on_disk = json.loads(
                (artifacts_dir / "sequence-result.json").read_text(encoding="utf-8")
            )
            self.assertIn("diagnostics", on_disk)
            self.assertIn("log_line", on_disk["diagnostics"])
            self.assertIn("repeatable_total_slots", on_disk["diagnostics"])


class NextTaskTemplateAuditTests(unittest.TestCase):
    """The shipped prompt-executing-next-task.md template must give the
    sentinel rule prominent placement (top-of-file, bold, with an exact
    example) — the failure mode the downstream session hit was the model
    skipping the rule under load."""

    TEMPLATE = REPO_ROOT / "subagents" / "prompt-executing-next-task.md"

    def test_sentinel_section_appears_before_step_process(self) -> None:
        text = self.TEMPLATE.read_text(encoding="utf-8")
        sentinel_idx = text.find("[[TRYCYCLE_SEQUENCE_DONE]]")
        process_idx = text.find("## Step process")
        self.assertGreater(sentinel_idx, 0, "sentinel string must appear in template")
        self.assertGreater(process_idx, 0, "## Step process must appear in template")
        self.assertLess(
            sentinel_idx,
            process_idx,
            "sentinel rule must be documented BEFORE Step process",
        )

    def test_sentinel_appears_in_a_fenced_code_block(self) -> None:
        """Bold prose alone is too easy to skim past — the exact byte
        sequence must be in a fenced block so the model sees it as a
        literal."""
        text = self.TEMPLATE.read_text(encoding="utf-8")
        # Look for any fenced code block (```\n…[[TRYCYCLE_SEQUENCE_DONE]]…\n```)
        match = re.search(
            r"```[a-z]*\n[^`]*\[\[TRYCYCLE_SEQUENCE_DONE\]\][^`]*\n```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match, "template must contain the sentinel inside a fenced code block"
        )

    def test_critical_callout_present(self) -> None:
        text = self.TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("CRITICAL", text)


if __name__ == "__main__":
    unittest.main()
