"""P3 — review_observations.py extract: scratch-file fallback.

When the reviewer's reply is missing the `<review_observations_json>`
envelope, the extractor reads structured fenced ```observation``` blocks
from a scratch file glob and synthesises the envelope mechanically. The
scratch path is documented in subagents/prompt-post-impl-review.md so the
reviewer writes it deterministically. No model is involved in the
synthesis.
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
EXTRACTOR = REPO_ROOT / "orchestrator" / "review_observations.py"


def _make_envelope_reply(observations: list[dict]) -> str:
    payload = {
        "status": "issues_found" if observations else "no_issues",
        "summary": "test reply",
        "observations": observations,
    }
    return f"<review_observations_json>{json.dumps(payload)}</review_observations_json>\n"


def _make_scratch_with_blocks(observations: list[dict]) -> str:
    lines: list[str] = ["# Review scratch — test", ""]
    for obs in observations:
        lines.append("```observation")
        lines.append(json.dumps(obs))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _run_extractor(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXTRACTOR), "extract", *args],
        text=True,
        capture_output=True,
        check=False,
    )


class EnvelopePathPreserved(unittest.TestCase):
    """Existing behaviour: when the envelope is present and well-formed,
    the extractor uses it and ignores the scratch file entirely."""

    def test_envelope_present_uses_envelope_not_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            scratch = tmp_path / "scratch.md"
            output = tmp_path / "obs.json"

            envelope_observations = [
                {
                    "id": "R1",
                    "severity": "critical",
                    "category": "correctness",
                    "expected": "from envelope",
                    "observed": "envelope wins",
                }
            ]
            reply.write_text(_make_envelope_reply(envelope_observations), encoding="utf-8")
            # Scratch contains a different observation. Must be ignored.
            scratch.write_text(
                _make_scratch_with_blocks(
                    [
                        {
                            "id": "S1",
                            "severity": "major",
                            "category": "correctness",
                            "expected": "from scratch",
                            "observed": "should be ignored",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-file", str(scratch),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            self.assertEqual(stdout["source"], "envelope")
            obs = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(obs["observations"][0]["id"], "R1")
            self.assertEqual(obs["observations"][0]["expected"], "from envelope")


class ScratchFallbackTriggers(unittest.TestCase):
    """When the envelope is missing, the extractor synthesises from the
    scratch file's ```observation``` blocks."""

    def test_missing_envelope_with_scratch_blocks_synthesises_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            scratch = tmp_path / "scratch.md"
            output = tmp_path / "obs.json"

            reply.write_text(
                "my review is already finalized — the JSON block is above\n",
                encoding="utf-8",
            )
            observations = [
                {
                    "id": "R1",
                    "severity": "critical",
                    "category": "correctness",
                    "expected": "X",
                    "observed": "Y",
                    "where": {"file": "src/foo.py", "line": 42},
                },
                {
                    "id": "R2",
                    "severity": "major",
                    "category": "missing_test",
                    "expected": "test for edge case Z",
                    "observed": "no test exists",
                },
            ]
            scratch.write_text(_make_scratch_with_blocks(observations), encoding="utf-8")

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-file", str(scratch),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            self.assertEqual(stdout["source"], "scratch_fallback")
            self.assertEqual(stdout["issue_count"], 2)
            self.assertEqual(stdout["blocking_issue_count"], 2)
            self.assertEqual(stdout["scratch_files_used"], [str(scratch)])

            normalized = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(normalized["status"], "issues_found")
            ids = [o["id"] for o in normalized["observations"]]
            self.assertEqual(ids, ["R1", "R2"])
            self.assertEqual(normalized["observations"][0]["where"]["line"], 42)

    def test_glob_aggregates_multiple_scratch_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            output = tmp_path / "obs.json"
            scratch1 = tmp_path / "review-scratch-aaa.md"
            scratch2 = tmp_path / "review-scratch-bbb.md"

            reply.write_text("dispatch died before envelope\n", encoding="utf-8")
            scratch1.write_text(
                _make_scratch_with_blocks(
                    [
                        {
                            "id": "A1",
                            "severity": "major",
                            "category": "correctness",
                            "expected": "a",
                            "observed": "b",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            scratch2.write_text(
                _make_scratch_with_blocks(
                    [
                        {
                            "id": "B1",
                            "severity": "minor",
                            "category": "edge_case",
                            "expected": "x",
                            "observed": "y",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-glob", str(tmp_path / "review-scratch-*.md"),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            stdout = json.loads(result.stdout)
            self.assertEqual(stdout["source"], "scratch_fallback")
            normalized = json.loads(output.read_text(encoding="utf-8"))
            ids = sorted(o["id"] for o in normalized["observations"])
            self.assertEqual(ids, ["A1", "B1"])

    def test_synthesised_envelope_matches_direct_envelope_for_same_observations(
        self,
    ) -> None:
        """A reviewer that 'forgot' to emit the envelope but wrote the same
        observations to scratch produces an extracted artifact that is
        equivalent (modulo `summary`) to one a healthy reviewer would have
        emitted."""
        observations = [
            {
                "id": "R1",
                "severity": "critical",
                "category": "correctness",
                "expected": "X",
                "observed": "Y",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Envelope path
            reply_env = tmp_path / "reply_env.txt"
            output_env = tmp_path / "obs_env.json"
            reply_env.write_text(_make_envelope_reply(observations), encoding="utf-8")

            # Scratch fallback path
            reply_scratch = tmp_path / "reply_scratch.txt"
            scratch = tmp_path / "scratch.md"
            output_scratch = tmp_path / "obs_scratch.json"
            reply_scratch.write_text("dispatch died\n", encoding="utf-8")
            scratch.write_text(_make_scratch_with_blocks(observations), encoding="utf-8")

            r1 = _run_extractor(
                "--reply", str(reply_env),
                "--output", str(output_env),
            )
            self.assertEqual(r1.returncode, 0, r1.stderr)

            r2 = _run_extractor(
                "--reply", str(reply_scratch),
                "--output", str(output_scratch),
                "--scratch-file", str(scratch),
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)

            env_norm = json.loads(output_env.read_text(encoding="utf-8"))
            scratch_norm = json.loads(output_scratch.read_text(encoding="utf-8"))

            # `summary` differs by design (the synthesised one is auto-labelled).
            env_norm.pop("summary", None)
            scratch_norm.pop("summary", None)
            self.assertEqual(env_norm, scratch_norm)


class ScratchFallbackEdgeCases(unittest.TestCase):
    def test_missing_envelope_no_scratch_passed_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            output = tmp_path / "obs.json"
            reply.write_text("dispatch died\n", encoding="utf-8")

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required", result.stderr)

    def test_missing_envelope_scratch_glob_matches_nothing_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            output = tmp_path / "obs.json"
            reply.write_text("dispatch died\n", encoding="utf-8")

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-glob", str(tmp_path / "no-match-*.md"),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required", result.stderr)

    def test_scratch_with_no_observation_blocks_synthesises_no_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            scratch = tmp_path / "scratch.md"
            output = tmp_path / "obs.json"

            reply.write_text("dispatch died\n", encoding="utf-8")
            scratch.write_text(
                "# Review scratch\n\nReviewer wrote some notes but no fenced blocks.\n",
                encoding="utf-8",
            )

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-file", str(scratch),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(normalized["status"], "no_issues")
            self.assertEqual(normalized["observations"], [])
            self.assertEqual(normalized["blocking_issue_count"], 0)

    def test_unrelated_fenced_blocks_in_scratch_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            scratch = tmp_path / "scratch.md"
            output = tmp_path / "obs.json"

            reply.write_text("dispatch died\n", encoding="utf-8")
            scratch.write_text(
                textwrap.dedent(
                    """\
                    # scratch

                    ```python
                    print("not an observation")
                    ```

                    ```observation
                    {"id": "R1", "severity": "major", "category": "correctness",
                     "expected": "x", "observed": "y"}
                    ```

                    ```bash
                    echo "neither am i"
                    ```
                    """
                ),
                encoding="utf-8",
            )

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-file", str(scratch),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized = json.loads(output.read_text(encoding="utf-8"))
            ids = [o["id"] for o in normalized["observations"]]
            self.assertEqual(ids, ["R1"])

    def test_malformed_observation_block_skipped_others_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            scratch = tmp_path / "scratch.md"
            output = tmp_path / "obs.json"

            reply.write_text("dispatch died\n", encoding="utf-8")
            scratch.write_text(
                textwrap.dedent(
                    """\
                    ```observation
                    not even json
                    ```

                    ```observation
                    {"id": "R1", "severity": "critical", "category": "correctness",
                     "expected": "x", "observed": "y"}
                    ```
                    """
                ),
                encoding="utf-8",
            )

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-file", str(scratch),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            normalized = json.loads(output.read_text(encoding="utf-8"))
            ids = [o["id"] for o in normalized["observations"]]
            self.assertEqual(ids, ["R1"])

    def test_unreadable_scratch_file_errors(self) -> None:
        """When every passed scratch file is unreadable, the extractor
        does NOT silently succeed — it surfaces the failure."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reply = tmp_path / "reply.txt"
            output = tmp_path / "obs.json"
            scratch = tmp_path / "missing.md"  # never created

            reply.write_text("dispatch died\n", encoding="utf-8")

            result = _run_extractor(
                "--reply", str(reply),
                "--output", str(output),
                "--scratch-file", str(scratch),
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("scratch", result.stderr)


if __name__ == "__main__":
    unittest.main()
