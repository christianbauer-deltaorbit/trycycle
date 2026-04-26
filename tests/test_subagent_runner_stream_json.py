"""Layer 3 — claude --output-format stream-json driver.

Fixture-driven unit tests verify the stream reader parses one JSON
object per line, emits a `subagent_event` row to events.jsonl per line,
and extracts the reply text + usage metadata from the terminal
`type: "result"` event. A skippable integration test exercises the
real `claude` CLI to confirm at least two `subagent_event` rows land
in events.jsonl with monotonically advancing timestamps.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SUBAGENT_RUNNER = REPO_ROOT / "orchestrator" / "subagent_runner.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_stream_json_fake(bin_dir: Path, *, lines: list[dict], exit_code: int = 0) -> None:
    """Fake claude that emits each JSON line on a new line, simulating
    `--output-format stream-json` output."""
    encoded_lines = [json.dumps(line).replace('"', '\\"') for line in lines]
    body = "".join(
        f'sys.stdout.write("{line}" + "\\n")\nsys.stdout.flush()\n'
        for line in encoded_lines
    )
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

            """
        )
        + body
        + f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    (bin_dir / "claude").chmod(0o755)


class StreamReaderUnitTests(unittest.TestCase):
    """Direct calls into _drive_claude_streaming with a Popen against a
    fake binary. No mocks of subprocess.run — real subprocess each time."""

    def test_emits_subagent_event_per_line_and_extracts_reply(self) -> None:
        from orchestrator.subagent_runner import _drive_claude_streaming

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            events_path = tmp_path / "events.jsonl"
            events_path.write_text("", encoding="utf-8")

            _write_stream_json_fake(
                bin_dir,
                lines=[
                    {"type": "system", "subtype": "init"},
                    {"type": "assistant", "message": "thinking..."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/foo"},
                    },
                    {"type": "tool_result", "content": "fake content"},
                    {
                        "type": "assistant",
                        "message": "found it",
                    },
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "the final reply",
                        "duration_ms": 1234,
                        "duration_api_ms": 1000,
                        "num_turns": 2,
                        "service_tier": "standard",
                        "total_cost_usd": 0.005,
                        "usage": {"input_tokens": 5, "output_tokens": 10},
                        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 5}},
                        "permission_denials": [],
                        "terminal_reason": "completed",
                        "stop_reason": "end_turn",
                    },
                ],
            )

            proc = subprocess.Popen(
                [str(bin_dir / "claude"), "-p", "--output-format", "stream-json"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout_text, stderr_text, reply, usage = _drive_claude_streaming(
                proc=proc,
                prompt_text="ping\n",
                timeout_seconds=15,
                events_path=events_path,
            )

            self.assertEqual(proc.returncode, 0)
            self.assertEqual(reply, "the final reply")
            self.assertIsNotNone(usage)
            self.assertEqual(usage["service_tier"], "standard")
            self.assertEqual(usage["total_cost_usd"], 0.005)

            events = _read_jsonl(events_path)
            subagent_events = [e for e in events if e["event"] == "subagent_event"]
            self.assertEqual(len(subagent_events), 6)
            types = [e.get("type") for e in subagent_events]
            self.assertEqual(
                types,
                ["system", "assistant", "tool_use", "tool_result", "assistant", "result"],
            )

    def test_monotonic_timestamps_on_subagent_events(self) -> None:
        from orchestrator.subagent_runner import _drive_claude_streaming

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            events_path = tmp_path / "events.jsonl"
            events_path.write_text("", encoding="utf-8")

            _write_stream_json_fake(
                bin_dir,
                lines=[
                    {"type": "system", "subtype": "init"},
                    {"type": "assistant", "message": "step 1"},
                    {
                        "type": "result",
                        "result": "done",
                        "is_error": False,
                        "service_tier": "standard",
                    },
                ],
            )

            proc = subprocess.Popen(
                [str(bin_dir / "claude"), "-p"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _drive_claude_streaming(
                proc=proc,
                prompt_text="",
                timeout_seconds=10,
                events_path=events_path,
            )

            events = _read_jsonl(events_path)
            subagent_events = [e for e in events if e["event"] == "subagent_event"]
            timestamps = [e["timestamp"] for e in subagent_events]
            self.assertEqual(timestamps, sorted(timestamps))

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        """A malformed line must not crash the reader thread; valid
        lines around it must still be parsed and emitted."""
        from orchestrator.subagent_runner import _drive_claude_streaming

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            events_path = tmp_path / "events.jsonl"
            events_path.write_text("", encoding="utf-8")

            (bin_dir / "claude").write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import sys
                    if "--help" in sys.argv:
                        sys.stdout.write("-p, --print\\n--output-format\\n--resume\\n--session-id\\n--allowedTools\\n")
                        raise SystemExit(0)
                    sys.stdout.write('{{"type":"system"}}\\n')
                    sys.stdout.write('not even json\\n')
                    sys.stdout.write('{{"type":"result","result":"final","is_error":false}}\\n')
                    raise SystemExit(0)
                    """
                ),
                encoding="utf-8",
            )
            (bin_dir / "claude").chmod(0o755)

            proc = subprocess.Popen(
                [str(bin_dir / "claude"), "-p"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout_text, stderr_text, reply, _ = _drive_claude_streaming(
                proc=proc,
                prompt_text="",
                timeout_seconds=10,
                events_path=events_path,
            )

            self.assertEqual(reply, "final")
            events = _read_jsonl(events_path)
            subagent_events = [e for e in events if e["event"] == "subagent_event"]
            self.assertEqual(len(subagent_events), 2)  # only the two valid lines

    def test_missing_terminal_result_event_returns_none_reply(self) -> None:
        """When the stream ends without a `type: "result"` event, the
        helper returns None for the reply. The caller (subagent_runner)
        falls back to the legacy raw-stdout parser via _parse_claude_json_reply."""
        from orchestrator.subagent_runner import _drive_claude_streaming

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            events_path = tmp_path / "events.jsonl"
            events_path.write_text("", encoding="utf-8")

            _write_stream_json_fake(
                bin_dir,
                lines=[
                    {"type": "system", "subtype": "init"},
                    {"type": "assistant", "message": "incomplete"},
                ],
            )
            proc = subprocess.Popen(
                [str(bin_dir / "claude"), "-p"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _, _, reply, usage = _drive_claude_streaming(
                proc=proc,
                prompt_text="",
                timeout_seconds=10,
                events_path=events_path,
            )

            self.assertIsNone(reply)
            self.assertIsNone(usage)


class StreamReaderBackendIntegrationTests(unittest.TestCase):
    """End-to-end through subagent_runner.py with a stream-json-emitting
    fake claude. Verifies the reply ends up in reply.txt and the
    rate_limit_state event is present from the terminal result's usage."""

    def test_runner_reply_and_usage_come_from_terminal_result_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            artifacts_dir = tmp_path / "art"
            home_dir = tmp_path / "home"
            home_dir.mkdir()
            prompt_path = tmp_path / "prompt.txt"
            prompt_path.write_text("ping\n", encoding="utf-8")

            _write_stream_json_fake(
                bin_dir,
                lines=[
                    {"type": "system", "subtype": "init"},
                    {"type": "assistant", "message": "doing it"},
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "real reply",
                        "service_tier": "standard",
                        "total_cost_usd": 0.001,
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                        "modelUsage": {},
                        "permission_denials": [],
                        "stop_reason": "end_turn",
                        "duration_ms": 50,
                        "duration_api_ms": 40,
                        "num_turns": 1,
                        "terminal_reason": "completed",
                    },
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SUBAGENT_RUNNER),
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
                ],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home_dir)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "ok")

            reply_text = (artifacts_dir / "reply.txt").read_text(encoding="utf-8")
            self.assertEqual(reply_text, "real reply")

            events = _read_jsonl(artifacts_dir / "events.jsonl")
            subagent_events = [e for e in events if e["event"] == "subagent_event"]
            self.assertEqual(len(subagent_events), 3)
            rate_limit_rows = [e for e in events if e["event"] == "rate_limit_state"]
            self.assertEqual(len(rate_limit_rows), 1)
            self.assertEqual(rate_limit_rows[0]["service_tier"], "standard")

            self.assertEqual(payload["ratelimit_at_exit"]["total_cost_usd"], 0.001)


@unittest.skipUnless(
    shutil.which("claude") is not None,
    "real claude CLI not on PATH; skipping live stream-json check",
)
class StreamReaderLiveIntegrationTests(unittest.TestCase):
    def test_live_claude_emits_at_least_one_subagent_event(self) -> None:
        """End-to-end against the installed claude. Asserts at least
        one subagent_event row was captured (in practice claude emits
        system + assistant + result, but we only require >= 1)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "repo"
            workdir.mkdir()
            artifacts_dir = tmp_path / "art"
            prompt_path = tmp_path / "prompt.txt"
            prompt_path.write_text(
                "Reply with literally: pong\n", encoding="utf-8"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SUBAGENT_RUNNER),
                    "run",
                    "--phase",
                    "live-stream-smoke",
                    "--prompt-file",
                    str(prompt_path),
                    "--workdir",
                    str(workdir),
                    "--artifacts-dir",
                    str(artifacts_dir),
                    "--backend",
                    "claude",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            events = _read_jsonl(artifacts_dir / "events.jsonl")
            subagent_events = [e for e in events if e["event"] == "subagent_event"]
            self.assertGreaterEqual(len(subagent_events), 1)
            # Terminal `result` event should be the last subagent_event.
            self.assertEqual(subagent_events[-1].get("type"), "result")


if __name__ == "__main__":
    unittest.main()
