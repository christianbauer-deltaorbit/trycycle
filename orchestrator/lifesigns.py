#!/usr/bin/env python3
"""P2 — life-signs gate for native-Agent and fallback-runner subagents.

Background. SKILL.md's Critical Rules forbid busy-polling subagents; that
rule is correct for healthy runs because polling drives context bloat and
encourages premature kills. But when a subagent dies without producing any
event flow (transcript file frozen, no error, no completion notification),
the coordinator has no signal at all and the only options observed in the
wild were (a) wait the full 60-180 minute hard timeout, (b) ad-hoc check
the transcript file mtime by hand. (b) is what one downstream session did
when its review subagent went silent for 80 minutes.

This helper is the principled, deterministic version of (b): the
coordinator runs ONE check at the cadence SKILL.md prescribes (every 5
minutes), reads file mtimes that the subagent SHOULD be touching, and
reports a structured `should_escalate` boolean plus diagnostics.

Two modes:

- ``check-fallback --artifacts-dir DIR`` reads ``events.jsonl``,
  ``stdout.txt``, and ``stderr.txt`` mtimes inside the fallback runner's
  artifacts directory. The most recent of those is the subagent's last
  observed activity timestamp.

- ``check-native --transcript-file FILE`` reads a single file's mtime,
  intended for the native Agent transcript file the host CLI writes to.

In both modes the threshold defaults to 5 minutes (the SKILL polling
cadence), so calling this helper at the prescribed interval and acting
on ``should_escalate`` will only escalate when there has been no progress
for at least one full polling window.

Output is JSON on stdout::

    {
      "last_activity_seconds_ago": 73.4,
      "reasonable_threshold_seconds": 300.0,
      "should_escalate": false,
      "checked_paths": [{"path": "...", "exists": true, "mtime": 1234567890.0}],
      "now": 1234567963.4
    }

Exit code is 0 in every well-formed case (including should_escalate=true).
A non-zero exit is reserved for usage errors and missing required paths
that the caller passed explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_THRESHOLD_SECONDS = 5 * 60.0


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _check_paths(paths: list[Path], *, threshold_seconds: float) -> dict[str, Any]:
    now = time.time()
    checked: list[dict[str, Any]] = []
    most_recent: float | None = None
    for path in paths:
        mtime = _mtime_or_none(path)
        checked.append(
            {
                "path": str(path),
                "exists": mtime is not None,
                "mtime": mtime,
            }
        )
        if mtime is not None:
            if most_recent is None or mtime > most_recent:
                most_recent = mtime

    if most_recent is None:
        return {
            "last_activity_seconds_ago": None,
            "reasonable_threshold_seconds": threshold_seconds,
            "should_escalate": True,
            "checked_paths": checked,
            "now": now,
            "reason": "no_signal_paths_exist",
        }

    seconds_ago = max(0.0, now - most_recent)
    return {
        "last_activity_seconds_ago": round(seconds_ago, 3),
        "reasonable_threshold_seconds": threshold_seconds,
        "should_escalate": seconds_ago > threshold_seconds,
        "checked_paths": checked,
        "now": now,
    }


def check_fallback(artifacts_dir: Path, *, threshold_seconds: float) -> dict[str, Any]:
    """Inspect a subagent_runner artifacts dir for last-activity signals.

    Reads mtimes of events.jsonl, stdout.txt, and stderr.txt. The most
    recent of those is the most recent observed activity. A directory
    where none of these exist is treated as `should_escalate` because the
    subagent has not even started writing output.
    """
    paths = [
        artifacts_dir / "events.jsonl",
        artifacts_dir / "stdout.txt",
        artifacts_dir / "stderr.txt",
    ]
    return _check_paths(paths, threshold_seconds=threshold_seconds)


def check_native(transcript_file: Path, *, threshold_seconds: float) -> dict[str, Any]:
    """Inspect a single native-Agent transcript file mtime."""
    return _check_paths([transcript_file], threshold_seconds=threshold_seconds)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Life-signs gate for trycycle subagents. Reads file mtimes only;"
            " no polling, no busy-loop. Run once at the SKILL-prescribed"
            " interval and act on `should_escalate`."
        )
    )
    parser.add_argument(
        "--threshold-seconds",
        type=float,
        default=DEFAULT_THRESHOLD_SECONDS,
        help=(
            f"Treat the subagent as silent when its most recent observed"
            f" activity is older than this. Default {DEFAULT_THRESHOLD_SECONDS}"
            f" (matches the SKILL polling cadence)."
        ),
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    fb = sub.add_parser(
        "check-fallback",
        help="Inspect a subagent_runner artifacts directory.",
    )
    fb.add_argument(
        "--artifacts-dir",
        required=True,
        type=Path,
        help="Path to the dispatch's artifacts dir (the one with events.jsonl).",
    )

    nv = sub.add_parser(
        "check-native",
        help="Inspect a native-Agent transcript file.",
    )
    nv.add_argument(
        "--transcript-file",
        required=True,
        type=Path,
        help="Path to the native Agent's transcript file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.mode == "check-fallback":
        report = check_fallback(
            args.artifacts_dir, threshold_seconds=args.threshold_seconds
        )
    elif args.mode == "check-native":
        report = check_native(
            args.transcript_file, threshold_seconds=args.threshold_seconds
        )
    else:  # pragma: no cover - argparse rejects this case
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
