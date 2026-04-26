"""P0 — per-step timeout declared by step template.

A `<!-- trycycle-step: ... -->` HTML-comment header at the top of a micro-step
template can declare `timeout-seconds: N`. run-sequence picks that up per step
and overrides the CLI-level --timeout-seconds default. Templates without a
header fall back to the CLI default (or to subagent_runner.py's phase
defaults if --timeout-seconds is omitted).
"""

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

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_fake_claude(bin_dir: Path) -> None:
    """Fake claude that advertises --allowedTools and prints argv to a known
    path so tests can verify which --timeout-seconds value the runner used."""
    log_path = bin_dir / "argv.log"
    (bin_dir / "claude").write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys

            if "--help" in sys.argv:
                sys.stdout.write(
                    "-p, --print\\n--output-format\\n--resume\\n"
                    "--session-id\\n--allowedTools\\n"
                )
                raise SystemExit(0)

            with open(r"{log_path}", "a", encoding="utf-8") as f:
                f.write(" ".join(sys.argv[1:]) + "\\n")
            sys.stdout.write("ok\\n")
            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "claude").chmod(0o755)


def _write_template(
    template_dir: Path,
    *,
    phase: str,
    step: str,
    header: str | None,
) -> None:
    parts: list[str] = []
    if header is not None:
        parts.append(header)
    parts.append(
        "Test step body. PHASE_STATE_PATH={PHASE_STATE_PATH}\n"
        "FAKE_REPLY: ok\n"
    )
    template_path = template_dir / f"prompt-{phase}-{step}.md"
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text("\n".join(parts), encoding="utf-8")


class StepTimeoutHeaderTests(unittest.TestCase):
    def run_sequence(
        self,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(RUN_PHASE), *args],
            text=True,
            capture_output=True,
            check=False,
            env=merged,
        )

    def test_header_value_overrides_cli_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"

            _write_template(
                template_dir,
                phase="testphase",
                step="long",
                header="<!-- trycycle-step:\n  timeout-seconds: 7200\n-->",
            )

            result = self.run_sequence(
                "run-sequence",
                "--phase",
                "testphase",
                "--steps",
                "long",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                "--timeout-seconds",
                "60",
                "--dry-run",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["steps"][0]["timeout_seconds"], 7200)
            # And the dispatched argv reflects that value.
            cmd = payload["steps"][0]["dispatch"]["process"]["command"]
            self.assertNotIn("--timeout-seconds", cmd)  # only on the runner's CLI, not on claude argv

    def test_header_absent_uses_cli_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"

            _write_template(
                template_dir, phase="testphase", step="quick", header=None
            )

            result = self.run_sequence(
                "run-sequence",
                "--phase",
                "testphase",
                "--steps",
                "quick",
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
                "--dry-run",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["steps"][0]["timeout_seconds"], 120)

    def test_header_absent_and_no_cli_default_yields_none(self) -> None:
        """Without a header and without --timeout-seconds, the per-step
        value is None — subagent_runner.py applies its phase default."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"

            _write_template(
                template_dir, phase="testphase", step="bare", header=None
            )

            result = self.run_sequence(
                "run-sequence",
                "--phase",
                "testphase",
                "--steps",
                "bare",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                "--dry-run",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIsNone(payload["steps"][0]["timeout_seconds"])

    def test_two_steps_with_different_headers_both_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"
            artifacts_dir = tmp_path / "artifacts"

            _write_template(
                template_dir,
                phase="multi",
                step="quick",
                header="<!-- trycycle-step:\n  timeout-seconds: 60\n-->",
            )
            _write_template(
                template_dir,
                phase="multi",
                step="long",
                header="<!-- trycycle-step:\n  timeout-seconds: 7200\n-->",
            )

            result = self.run_sequence(
                "run-sequence",
                "--phase",
                "multi",
                "--steps",
                "quick,long",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                "--timeout-seconds",
                "300",
                "--dry-run",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            seen = {step["step"]: step["timeout_seconds"] for step in payload["steps"]}
            self.assertEqual(seen["quick"], 60)
            self.assertEqual(seen["long"], 7200)

    def test_shipped_templates_declare_expected_per_step_timeouts(self) -> None:
        """Each shipped subagent template under subagents/ that the
        per-template-timeout convention applies to declares the
        empirically-justified value documented in SKILL.md.

        Updating this map deliberately requires touching the test so a
        change in expected duration is reviewed alongside the template
        change.
        """
        from orchestrator.run_phase import _parse_step_header

        expected: dict[str, int] = {
            # Long deterministic work — bounded by repo regression suite.
            "subagents/prompt-executing-finalize.md": 14400,
            # Multi-task implementation; per-invocation override pattern
            # documented for unusual loads (e.g. --timeout-seconds 14400).
            "subagents/prompt-executing-next-task.md": 10800,
            # Inventory step. Anything longer = over-exploration; fail fast.
            "subagents/prompt-planning-initial-survey.md": 600,
            # Bounded creative work on a known scaffold.
            "subagents/prompt-planning-initial-scaffold.md": 1800,
            "subagents/prompt-planning-initial-detail.md": 1800,
            "subagents/prompt-planning-initial-commit.md": 1800,
            # Stateless review pass; should not need an hour.
            "subagents/prompt-planning-edit.md": 1800,
            # Single-shot reasoning over codebase. Beyond an hour usually
            # means prompt-too-big or rambling.
            "subagents/prompt-test-strategy.md": 3600,
            "subagents/prompt-test-plan.md": 3600,
            # Full diff review with structured observations output.
            "subagents/prompt-post-impl-review.md": 3600,
        }

        for relative, value in expected.items():
            with self.subTest(template=relative):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                fields = _parse_step_header(text)
                self.assertEqual(
                    fields.get("timeout-seconds"),
                    value,
                    f"{relative} should declare timeout-seconds: {value}",
                )

    def test_header_wins_over_explicit_cli_timeout_seconds(self) -> None:
        """Precedence regression. When BOTH a template header AND --timeout-seconds
        are passed, the header value wins. (This is the inherited behaviour from
        commit 1987c89; the run-sequence resolver uses the header as the override
        and the CLI flag as the default.) The same template dispatched without
        --timeout-seconds also uses the header value.

        Codifies the precedence so a future runner change cannot silently flip
        it without this test failing first.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            _write_fake_claude(bin_dir)
            template_dir = tmp_path / "templates"

            _write_template(
                template_dir,
                phase="precedence",
                step="long",
                header="<!-- trycycle-step:\n  timeout-seconds: 1800\n-->",
            )

            base_args = [
                "run-sequence",
                "--phase",
                "precedence",
                "--steps",
                "long",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--backend",
                "claude",
                "--dry-run",
            ]
            env = {
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "HOME": str(tmp_path),
            }

            # 1. Header alone, no --timeout-seconds: header wins (1800).
            artifacts_a = tmp_path / "art_a"
            result_a = self.run_sequence(
                *base_args,
                "--artifacts-dir",
                str(artifacts_a),
                env=env,
            )
            self.assertEqual(result_a.returncode, 0, result_a.stderr)
            payload_a = json.loads(result_a.stdout)
            self.assertEqual(payload_a["steps"][0]["timeout_seconds"], 1800)

            # 2. Header + --timeout-seconds 600: under the current resolver
            # (commit 1987c89), the HEADER wins, not the CLI flag. If you
            # want CLI to win, _resolve_step_timeout_seconds must be flipped
            # — and this test will start failing as the canary.
            artifacts_b = tmp_path / "art_b"
            result_b = self.run_sequence(
                *base_args,
                "--artifacts-dir",
                str(artifacts_b),
                "--timeout-seconds",
                "600",
                env=env,
            )
            self.assertEqual(result_b.returncode, 0, result_b.stderr)
            payload_b = json.loads(result_b.stdout)
            self.assertEqual(
                payload_b["steps"][0]["timeout_seconds"],
                1800,
                "Header value 1800 should override CLI --timeout-seconds 600 "
                "under commit 1987c89's resolver. If this fails, the precedence "
                "has been inverted intentionally — update the documentation in "
                "SKILL.md §5 (Sequenced phases) accordingly.",
            )

            # 3. Template without a header + --timeout-seconds 600: CLI wins.
            _write_template(
                template_dir,
                phase="precedence",
                step="bare",
                header=None,
            )
            artifacts_c = tmp_path / "art_c"
            result_c = self.run_sequence(
                "run-sequence",
                "--phase",
                "precedence",
                "--steps",
                "bare",
                "--template-dir",
                str(template_dir),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_c),
                "--backend",
                "claude",
                "--dry-run",
                "--timeout-seconds",
                "600",
                env=env,
            )
            self.assertEqual(result_c.returncode, 0, result_c.stderr)
            payload_c = json.loads(result_c.stdout)
            self.assertEqual(payload_c["steps"][0]["timeout_seconds"], 600)

    def test_unknown_header_keys_are_ignored_not_errored(self) -> None:
        """Forward-compatibility: unknown keys must not break parsing."""
        from orchestrator.run_phase import _parse_step_header

        text = (
            "<!-- trycycle-step:\n"
            "  timeout-seconds: 60\n"
            "  experimental-knob: foo\n"
            "  another: 12.5\n"
            "-->\n"
        )
        fields = _parse_step_header(text)
        self.assertEqual(fields["timeout-seconds"], 60)
        self.assertEqual(fields["experimental-knob"], "foo")
        # 12.5 isn't an int → kept as string. Future code can parse if needed.
        self.assertEqual(fields["another"], "12.5")


if __name__ == "__main__":
    unittest.main()
