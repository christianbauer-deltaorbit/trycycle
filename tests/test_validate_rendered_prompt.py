from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "orchestrator" / "prompt_builder" / "validate_rendered.py"


class ValidateRenderedPromptTests(unittest.TestCase):
    def run_validator(self, prompt_text: str, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_path = Path(tmpdir) / "prompt.txt"
            prompt_path.write_text(prompt_text, encoding="utf-8")
            return subprocess.run(
                ["python3", str(VALIDATOR), "--prompt-file", str(prompt_path), *args],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_accepts_prompt_without_placeholders(self) -> None:
        result = self.run_validator(
            "<task_input_json>{\"goal\": \"ship preview\"}</task_input_json>\n"
            "Work in /tmp/example\n",
            "--require-nonempty-tag",
            "task_input_json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_unsubstituted_placeholders(self) -> None:
        result = self.run_validator("Work in {WORKTREE_PATH}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsubstituted placeholders: WORKTREE_PATH", result.stderr)

    def test_rejects_missing_required_tag(self) -> None:
        result = self.run_validator("hello\n", "--require-nonempty-tag", "task_input_json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing required <task_input_json> block", result.stderr)

    def test_rejects_empty_required_tag(self) -> None:
        result = self.run_validator(
            "<task_input_json>   \n\t </task_input_json>\n",
            "--require-nonempty-tag",
            "task_input_json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty <task_input_json> block", result.stderr)

    def test_ignores_placeholder_like_text_inside_allowed_tag(self) -> None:
        result = self.run_validator(
            "<task_input_json>{\"text\": \"historical {WORKTREE_PATH}\"}</task_input_json>\n"
            "Work in /tmp/example\n",
            "--require-nonempty-tag",
            "task_input_json",
            "--ignore-tag-for-placeholders",
            "task_input_json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class IgnoredTagGreedyBodyTests(unittest.TestCase):
    """Greedy body match: <tag>...</tag> must absorb any literal inner </tag>
    strings that appear inside a substituted transcript, so their bodies are
    stripped in one pass before placeholder detection runs."""

    def run_validator(self, prompt_text: str, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_path = Path(tmpdir) / "prompt.txt"
            prompt_path.write_text(prompt_text, encoding="utf-8")
            return subprocess.run(
                ["python3", str(VALIDATOR), "--prompt-file", str(prompt_path), *args],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_inner_close_tag_then_placeholder_inside_context_is_accepted(self) -> None:
        # Transcript body contains a literal </context> (e.g. pasted docs)
        # followed by a placeholder-like token still INSIDE the outer span.
        # Greedy body match absorbs to the final </context>, so {FAKE_TOKEN}
        # must not be flagged as an unsubstituted placeholder.
        result = self.run_validator(
            "<context>\n"
            "pasted docs mention </context> and reference {FAKE_TOKEN}\n"
            "</context>\n"
            "Work in /tmp/example\n",
            "--ignore-tag-for-placeholders",
            "context",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_placeholder_outside_context_is_still_rejected(self) -> None:
        # Same shape of transcript, but the placeholder now lives OUTSIDE
        # the <context> span. Ignoring the tag must not silence that.
        result = self.run_validator(
            "Leading {FAKE_TOKEN} at top.\n"
            "<context>\n"
            "pasted docs mention </context> inside\n"
            "</context>\n",
            "--ignore-tag-for-placeholders",
            "context",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAKE_TOKEN", result.stderr)

    def test_two_unrelated_tags_with_nested_closes_both_stripped(self) -> None:
        # Two different ignored tags, each with a literal nested close of
        # its own name in its body, and placeholder-like tokens after the
        # inner closes. Both must be stripped.
        result = self.run_validator(
            "<context>\n"
            "first transcript body with </context> inside and {TOKEN_A}\n"
            "</context>\n"
            "middle prose with no placeholders\n"
            "<reply>\n"
            "assistant reply containing </reply> inside and {TOKEN_B}\n"
            "</reply>\n",
            "--ignore-tag-for-placeholders",
            "context",
            "--ignore-tag-for-placeholders",
            "reply",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ValidateHeartbeatSectionTests(unittest.TestCase):
    """P1: rendered prompts must carry a non-empty '## Streaming discipline' section."""

    def run_validator(self, prompt_text: str, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_path = Path(tmpdir) / "prompt.txt"
            prompt_path.write_text(prompt_text, encoding="utf-8")
            return subprocess.run(
                ["python3", str(VALIDATOR), "--prompt-file", str(prompt_path), *args],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_accepts_prompt_with_heartbeat_body(self) -> None:
        result = self.run_validator(
            "# title\n\n## Streaming discipline\n\n"
            "Commit after each task. Each commit resets the streaming window.\n\n"
            "## Output discipline\n\nTerse.\n",
            "--require-heartbeat-section",
            "Streaming discipline",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_prompt_missing_section(self) -> None:
        result = self.run_validator(
            "# title\n\nno heartbeat block here\n",
            "--require-heartbeat-section",
            "Streaming discipline",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Streaming discipline", result.stderr)
        self.assertIn("missing", result.stderr)

    def test_rejects_prompt_with_empty_section(self) -> None:
        result = self.run_validator(
            "## Streaming discipline\n\n   \n\n## Output discipline\n\nbody\n",
            "--require-heartbeat-section",
            "Streaming discipline",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty", result.stderr)
        self.assertIn("Streaming discipline", result.stderr)

    def test_accepts_section_at_end_of_file(self) -> None:
        result = self.run_validator(
            "# title\n\nintro\n\n## Streaming discipline\n\n"
            "Write progressively. Heartbeat every ~90s.\n",
            "--require-heartbeat-section",
            "Streaming discipline",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ValidateShippedPromptsHaveHeartbeatTests(unittest.TestCase):
    """Regression guard: the six subagent prompts shipped in 9bb41ce all carry heartbeat."""

    SHIPPED_PROMPTS = (
        "subagents/prompt-test-strategy.md",
        "subagents/prompt-test-plan.md",
        "subagents/prompt-executing.md",
        "subagents/prompt-post-impl-review.md",
        "subagents/prompt-planning-initial.md",
        "subagents/prompt-planning-edit.md",
    )

    def test_every_shipped_prompt_has_heartbeat_section(self) -> None:
        # Imported directly (bypassing the CLI's placeholder check) because
        # these are templates with {PLACEHOLDER} tokens, not rendered prompts.
        import sys
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from orchestrator.prompt_builder.validate_rendered import (
            validate_heartbeat_section,
        )
        for relative in self.SHIPPED_PROMPTS:
            with self.subTest(prompt=relative):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                validate_heartbeat_section(text, "Streaming discipline")


if __name__ == "__main__":
    unittest.main()
