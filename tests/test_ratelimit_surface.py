"""P1 — surface claude usage/cost metadata and rate-limit-style failures.

The claude CLI does not expose anthropic-ratelimit-* HTTP response headers
today. The closest available signal is the per-call usage object emitted
under `--output-format=json` (input/output tokens, service_tier, cost,
duration). subagent_runner now switches the claude dispatch to JSON,
parses that block, plumbs it into result.json's `ratelimit_at_exit` field,
and emits a `rate_limit_state` event into events.jsonl.

Account-level rate-limit failures (CLI exits 1 with a `result` text like
"You've hit your limit · resets …") are detected and elevated into the
dispatch's `message` so the coordinator can disambiguate them from
stream-idle / wall-clock timeouts.
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
SUBAGENT_RUNNER = REPO_ROOT / "orchestrator" / "subagent_runner.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_json_emitting_claude(bin_dir: Path, *, payload: dict, exit_code: int = 0) -> None:
    """Fake claude that, on a non-help invocation, writes the given JSON
    payload to stdout (mimicking --output-format=json) and exits with the
    given code. Always advertises --allowedTools in --help."""
    encoded = json.dumps(payload).replace('"', '\\"').replace("\n", "\\n")
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

            sys.stdout.write("{encoded}")
            raise SystemExit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    (bin_dir / "claude").chmod(0o755)


class ParseClaudeJsonReplyUnitTests(unittest.TestCase):
    """Direct unit tests against the parser. No subprocess."""

    def test_parses_success_payload(self) -> None:
        from orchestrator.subagent_runner import _parse_claude_json_reply

        payload = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "the reply text",
                "duration_ms": 1234,
                "duration_api_ms": 1000,
                "num_turns": 2,
                "stop_reason": "end_turn",
                "service_tier": "standard",
                "total_cost_usd": 0.005,
                "usage": {"input_tokens": 5, "output_tokens": 10},
                "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 5}},
                "permission_denials": [],
                "terminal_reason": "completed",
            }
        )
        reply, meta = _parse_claude_json_reply(payload)
        self.assertEqual(reply, "the reply text")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["total_cost_usd"], 0.005)
        self.assertEqual(meta["service_tier"], "standard")
        self.assertEqual(meta["usage"]["input_tokens"], 5)
        self.assertIn("modelUsage", meta)

    def test_falls_back_to_text_on_non_json(self) -> None:
        from orchestrator.subagent_runner import _parse_claude_json_reply

        reply, meta = _parse_claude_json_reply("plain text reply\n")
        self.assertEqual(reply, "plain text reply\n")
        self.assertIsNone(meta)

    def test_falls_back_when_result_field_missing(self) -> None:
        from orchestrator.subagent_runner import _parse_claude_json_reply

        reply, meta = _parse_claude_json_reply(json.dumps({"foo": "bar"}))
        self.assertEqual(reply, json.dumps({"foo": "bar"}))
        self.assertIsNone(meta)

    def test_empty_stdout_is_empty_reply_no_metadata(self) -> None:
        from orchestrator.subagent_runner import _parse_claude_json_reply

        reply, meta = _parse_claude_json_reply("")
        self.assertEqual(reply, "")
        self.assertIsNone(meta)


class ClassifyClaudeFailureUnitTests(unittest.TestCase):
    def test_recognises_account_limit_message(self) -> None:
        from orchestrator.subagent_runner import _classify_claude_failure

        msg = _classify_claude_failure("You've hit your limit · resets 11am (UTC)")
        self.assertIsNotNone(msg)
        self.assertIn("rate-limit", msg)
        self.assertIn("resets 11am", msg)

    def test_returns_none_on_unrelated_text(self) -> None:
        from orchestrator.subagent_runner import _classify_claude_failure

        self.assertIsNone(_classify_claude_failure("everything went fine"))
        self.assertIsNone(_classify_claude_failure(""))


class RunWithFakeJsonClaudeIntegration(unittest.TestCase):
    """Drive the runner end-to-end with a fake claude that emits the JSON
    shape we expect, then assert ratelimit_at_exit and the events.jsonl
    `rate_limit_state` row are populated."""

    def run_runner(
        self,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(SUBAGENT_RUNNER), *args],
            text=True,
            capture_output=True,
            check=False,
            env=merged,
        )

    def test_success_dispatch_populates_ratelimit_at_exit_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            artifacts_dir = tmp_path / "artifacts"
            home_dir = tmp_path / "home"
            home_dir.mkdir()
            prompt_path = tmp_path / "prompt.txt"
            prompt_path.write_text("ping\n", encoding="utf-8")

            _write_json_emitting_claude(
                bin_dir,
                payload={
                    "type": "result",
                    "subtype": "success",
                    "result": "fake reply",
                    "is_error": False,
                    "duration_ms": 42,
                    "duration_api_ms": 40,
                    "num_turns": 1,
                    "stop_reason": "end_turn",
                    "service_tier": "standard",
                    "total_cost_usd": 0.001,
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 7,
                        "service_tier": "standard",
                    },
                    "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 3}},
                    "permission_denials": [],
                    "terminal_reason": "completed",
                },
                exit_code=0,
            )

            result = self.run_runner(
                "run",
                "--phase",
                "smoke",
                "--prompt-file",
                str(prompt_path),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")
            rl = payload["ratelimit_at_exit"]
            self.assertIsNotNone(rl)
            self.assertEqual(rl["service_tier"], "standard")
            self.assertEqual(rl["total_cost_usd"], 0.001)
            self.assertEqual(rl["usage"]["input_tokens"], 3)

            events = (artifacts_dir / "events.jsonl").read_text(encoding="utf-8")
            rate_limit_lines = [
                json.loads(line)
                for line in events.splitlines()
                if line.strip() and "rate_limit_state" in line
            ]
            self.assertEqual(len(rate_limit_lines), 1)
            self.assertEqual(rate_limit_lines[0]["service_tier"], "standard")

            reply_text = (artifacts_dir / "reply.txt").read_text(encoding="utf-8")
            self.assertEqual(reply_text, "fake reply")

    def test_account_limit_failure_elevates_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            artifacts_dir = tmp_path / "artifacts"
            home_dir = tmp_path / "home"
            home_dir.mkdir()
            prompt_path = tmp_path / "prompt.txt"
            prompt_path.write_text("ping\n", encoding="utf-8")

            # CLI returns a non-zero exit with a `result` field carrying
            # the rate-limit message — modelled on the actual reply observed
            # in the downstream evidence tarball.
            _write_json_emitting_claude(
                bin_dir,
                payload={
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "You've hit your limit · resets 11am (UTC)",
                    "duration_ms": 12,
                    "duration_api_ms": 10,
                    "num_turns": 0,
                    "stop_reason": "rate_limit",
                    "service_tier": "standard",
                    "total_cost_usd": 0.0,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "modelUsage": {},
                    "permission_denials": [],
                    "terminal_reason": "error",
                },
                exit_code=1,
            )

            result = self.run_runner(
                "run",
                "--phase",
                "smoke",
                "--prompt-file",
                str(prompt_path),
                "--workdir",
                str(workdir),
                "--artifacts-dir",
                str(artifacts_dir),
                "--backend",
                "claude",
                env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home_dir)},
            )

            # subagent_runner returns 1 on escalate_to_user.
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "escalate_to_user")
            self.assertIn("rate-limit", payload["message"])
            self.assertIn("resets 11am", payload["message"])
            # And the usage block survived the failure path.
            self.assertIsNotNone(payload["ratelimit_at_exit"])


if __name__ == "__main__":
    unittest.main()
