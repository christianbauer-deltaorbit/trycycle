from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

subagent_runner = importlib.import_module("orchestrator.subagent_runner")


class ClaudeCommandPermissionTests(unittest.TestCase):
    """P0: claude command vector must swap permission flag based on uid."""

    def _run(self, command_fn, *, geteuid: int, supports_allowed_tools: bool, env: dict | None = None):
        env = env or {}
        with mock.patch.object(subagent_runner.os, "geteuid", return_value=geteuid, create=True), \
             mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop(subagent_runner.CLAUDE_ALLOWED_TOOLS_ENV, None)
            for key, value in env.items():
                os.environ[key] = value
            return command_fn(supports_allowed_tools=supports_allowed_tools)

    # _claude_command path

    def test_non_root_keeps_dangerously_skip_permissions(self) -> None:
        def build(*, supports_allowed_tools: bool):
            return subagent_runner._claude_command(
                binary="claude",
                effort=None,
                model=None,
                supports_allowed_tools=supports_allowed_tools,
            )

        argv, _ = self._run(build, geteuid=1000, supports_allowed_tools=True)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allowedTools", argv)

    def test_root_swaps_in_default_allowed_tools(self) -> None:
        def build(*, supports_allowed_tools: bool):
            return subagent_runner._claude_command(
                binary="claude",
                effort=None,
                model=None,
                supports_allowed_tools=supports_allowed_tools,
            )

        argv, _ = self._run(build, geteuid=0, supports_allowed_tools=True)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--allowedTools", argv)
        idx = argv.index("--allowedTools")
        self.assertEqual(argv[idx + 1], subagent_runner.CLAUDE_DEFAULT_ALLOWED_TOOLS)

    def test_root_honours_env_override(self) -> None:
        def build(*, supports_allowed_tools: bool):
            return subagent_runner._claude_command(
                binary="claude",
                effort=None,
                model=None,
                supports_allowed_tools=supports_allowed_tools,
            )

        argv, _ = self._run(
            build,
            geteuid=0,
            supports_allowed_tools=True,
            env={subagent_runner.CLAUDE_ALLOWED_TOOLS_ENV: "Read,Write"},
        )
        idx = argv.index("--allowedTools")
        self.assertEqual(argv[idx + 1], "Read,Write")

    def test_root_without_cli_support_raises(self) -> None:
        with mock.patch.object(subagent_runner.os, "geteuid", return_value=0, create=True):
            with self.assertRaises(subagent_runner._RootPermissionError) as ctx:
                subagent_runner._claude_command(
                    binary="claude",
                    effort=None,
                    model=None,
                    supports_allowed_tools=False,
                )
        self.assertIn("TRYCYCLE_CLAUDE_ALLOWED_TOOLS", str(ctx.exception))

    # _claude_resume_command path

    def test_resume_non_root_keeps_dangerously_skip_permissions(self) -> None:
        with mock.patch.object(subagent_runner.os, "geteuid", return_value=1000, create=True):
            argv = subagent_runner._claude_resume_command(
                binary="claude",
                session_id="abc",
                effort=None,
                model=None,
                supports_allowed_tools=True,
            )
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--allowedTools", argv)

    def test_resume_root_swaps_in_default_allowed_tools(self) -> None:
        with mock.patch.object(subagent_runner.os, "geteuid", return_value=0, create=True):
            os.environ.pop(subagent_runner.CLAUDE_ALLOWED_TOOLS_ENV, None)
            argv = subagent_runner._claude_resume_command(
                binary="claude",
                session_id="abc",
                effort=None,
                model=None,
                supports_allowed_tools=True,
            )
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--allowedTools", argv)
        idx = argv.index("--allowedTools")
        self.assertEqual(argv[idx + 1], subagent_runner.CLAUDE_DEFAULT_ALLOWED_TOOLS)

    def test_resume_root_honours_env_override(self) -> None:
        with mock.patch.object(subagent_runner.os, "geteuid", return_value=0, create=True), \
             mock.patch.dict(os.environ, {subagent_runner.CLAUDE_ALLOWED_TOOLS_ENV: "Read,Write"}, clear=False):
            argv = subagent_runner._claude_resume_command(
                binary="claude",
                session_id="abc",
                effort=None,
                model=None,
                supports_allowed_tools=True,
            )
        idx = argv.index("--allowedTools")
        self.assertEqual(argv[idx + 1], "Read,Write")

    def test_resume_root_without_cli_support_raises(self) -> None:
        with mock.patch.object(subagent_runner.os, "geteuid", return_value=0, create=True):
            with self.assertRaises(subagent_runner._RootPermissionError):
                subagent_runner._claude_resume_command(
                    binary="claude",
                    session_id="abc",
                    effort=None,
                    model=None,
                    supports_allowed_tools=False,
                )


class ClaudeProbeTests(unittest.TestCase):
    """Integration: real claude --help probe should return supports_allowed_tools."""

    def test_probe_reports_allowed_tools_support_on_installed_cli(self) -> None:
        if not subagent_runner._resolve_binary("claude"):
            self.skipTest("claude CLI not installed in this environment")
        probe = subagent_runner._probe_claude("claude")
        self.assertTrue(probe["available"])
        self.assertIn("supports_allowed_tools", probe)
        self.assertTrue(probe["supports_allowed_tools"])


if __name__ == "__main__":
    unittest.main()
