#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_BUILDER = SCRIPT_DIR / "prompt_builder" / "build.py"
TRANSCRIPT_BUILDER = SCRIPT_DIR / "user-request-transcript" / "build.py"
SUBAGENT_RUNNER = SCRIPT_DIR / "subagent_runner.py"

# Phases that received a "## Streaming discipline" block in 9bb41ce.
# Rendered prompts for these phases must carry that section to keep the
# per-turn streaming-heartbeat rule enforceable.
HEAVY_PHASES_REQUIRING_HEARTBEAT = frozenset({
    "test-strategy",
    "test-plan",
    "executing",
    "post-implementation-review",
    "planning-initial",
    "planning-edit",
})
HEARTBEAT_SECTION = "Streaming discipline"

# Sentinel a decomposed-phase subagent can write into the shared scratch file
# to tell run-sequence that all remaining repeatable steps can be skipped.
SEQUENCE_DONE_SENTINEL = "[[TRYCYCLE_SEQUENCE_DONE]]"

# Prompt-size warning threshold. Prompts above this routinely correlate
# with stream-idle timeouts because the subagent burns initial tool-call
# budget reading them. The transcript binding is the most common cause.
PROMPT_SIZE_WARNING_BYTES = 100_000

# Optional per-step header in a micro-step template:
#
#   <!-- trycycle-step:
#     timeout-seconds: 7200
#   -->
#
# Supported keys today: timeout-seconds (int). Unknown keys are ignored so
# templates can declare new fields without breaking older runners.
_STEP_HEADER_RE = re.compile(
    r"<!--\s*trycycle-step:\s*(?P<body>.*?)-->",
    re.DOTALL,
)


def _parse_step_header(template_text: str) -> dict[str, Any]:
    """Extract the optional `<!-- trycycle-step: ... -->` block.

    Returns a dict of the declared fields, or {} when the block is missing.
    Format: simple `key: value` lines, one per line. Values are str-stripped;
    integer-shaped values are returned as int. Unknown keys are preserved
    as strings so future runners can pick them up without code changes here.
    """
    match = _STEP_HEADER_RE.search(template_text)
    if not match:
        return {}
    fields: dict[str, Any] = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            fields[key] = int(value)
        else:
            fields[key] = value
    return fields


def _resolve_step_timeout_seconds(
    template_path: Path,
    cli_default: int | None,
) -> int | None:
    """Per-step timeout: prefer the template header value when present,
    otherwise fall back to the CLI-level --timeout-seconds default."""
    try:
        text = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return cli_default
    declared = _parse_step_header(text).get("timeout-seconds")
    if isinstance(declared, int) and declared > 0:
        return declared
    return cli_default


class PhaseError(RuntimeError):
    pass


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _emit_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _parse_placeholder_name(raw: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", raw):
        raise PhaseError(f"Invalid placeholder name: {raw!r}")
    return raw


def _parse_binding(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise PhaseError(f"Binding must be NAME=VALUE, got: {raw!r}")
    name, value = raw.split("=", 1)
    return _parse_placeholder_name(name), value


def _detect_transcript_cli(selected: str) -> str:
    if selected != "auto":
        return selected
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_HOME"):
        return "codex-cli"
    if os.environ.get("CLAUDECODE"):
        return "claude-code"
    if os.environ.get("OPENCODE"):
        return "opencode"
    raise PhaseError("Could not detect transcript CLI. Pass --transcript-cli explicitly.")


def _run_command(
    argv: list[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise PhaseError(result.stderr.strip() or f"Command failed: {' '.join(argv)}")
    return result


def _prepare_transcripts(
    args: argparse.Namespace,
    artifacts_dir: Path,
    *,
    workdir: Path,
) -> tuple[str | None, dict[str, str]]:
    placeholders = [_parse_placeholder_name(raw) for raw in args.transcript_placeholder]
    if not placeholders:
        return None, {}

    cli_name = _detect_transcript_cli(args.transcript_cli)
    if cli_name == "claude-code" and not args.canary:
        raise PhaseError(
            "Claude transcript lookup requires --canary from a prior top-level canary command."
        )

    inputs_dir = artifacts_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    rendered_paths: dict[str, str] = {}

    first_placeholder = placeholders[0]
    first_output_path = inputs_dir / f"{first_placeholder}.txt"
    transcript_search_root = None
    if args.transcript_search_root:
        transcript_search_root = args.transcript_search_root
        if not transcript_search_root.is_absolute():
            transcript_search_root = (Path.cwd() / transcript_search_root).resolve()

    command = [
        sys.executable,
        str(TRANSCRIPT_BUILDER),
        "--cli",
        cli_name,
        "--output",
        str(first_output_path),
    ]
    if args.canary:
        command.extend(["--canary", args.canary])
    if transcript_search_root:
        command.extend(["--search-root", str(transcript_search_root)])
    _run_command(command, cwd=workdir)
    rendered_paths[first_placeholder] = str(first_output_path)

    for placeholder in placeholders[1:]:
        path = inputs_dir / f"{placeholder}.txt"
        shutil.copyfile(first_output_path, path)
        rendered_paths[placeholder] = str(path)

    return cli_name, rendered_paths


def _build_prompt(
    args: argparse.Namespace,
    artifacts_dir: Path,
    transcript_paths: dict[str, str],
) -> Path:
    prompt_path = artifacts_dir / "prompt.txt"
    command = [
        sys.executable,
        str(PROMPT_BUILDER),
        "--template",
        str(Path(args.template).resolve()),
        "--output",
        str(prompt_path),
    ]
    for raw in args.set:
        command.extend(["--set", raw])
    for raw in args.set_file:
        command.extend(["--set-file", raw])
    for name, path in transcript_paths.items():
        command.extend(["--set-file", f"{name}={path}"])
    for tag in args.require_nonempty_tag:
        command.extend(["--require-nonempty-tag", tag])
    for tag in args.ignore_tag_for_placeholders:
        command.extend(["--ignore-tag-for-placeholders", tag])
    if args.phase in HEAVY_PHASES_REQUIRING_HEARTBEAT:
        command.extend(["--require-heartbeat-section", HEARTBEAT_SECTION])
    _run_command(command)
    return prompt_path


def _reject_skill_md_as_template(template: Path) -> None:
    """Refuse to render trycycle's orchestrator documentation as a phase prompt.

    SKILL.md is orchestrator prose whose '{PLACEHOLDER}' tokens are
    documentation references, not renderable bindings. Rendering it produces
    a generic "unsubstituted placeholders" dump that hides the real mistake.
    """
    if template.name == "SKILL.md":
        raise PhaseError(
            f"--template points at {template} (orchestrator documentation, "
            "not a phase prompt). Use <skill-directory>/subagents/"
            "prompt-<phase>.md instead."
        )


def _check_prompt_size(prompt_path: Path, max_bytes: int | None) -> dict[str, Any]:
    """Inspect rendered-prompt size. Warn over PROMPT_SIZE_WARNING_BYTES,
    error when --max-prompt-bytes is set and exceeded.

    Returns a `prompt_size` payload describing the verdict so callers can
    surface it in their result.json. The warning is emitted to stderr so
    automated callers see it.
    """
    try:
        size_bytes = prompt_path.stat().st_size
    except OSError:
        return {"bytes": None, "verdict": "unknown"}

    payload: dict[str, Any] = {
        "bytes": size_bytes,
        "warning_threshold_bytes": PROMPT_SIZE_WARNING_BYTES,
        "max_bytes": max_bytes,
        "verdict": "ok",
    }
    if max_bytes is not None and size_bytes > max_bytes:
        payload["verdict"] = "exceeds_max"
        raise PhaseError(
            f"rendered prompt is {size_bytes} bytes, exceeding "
            f"--max-prompt-bytes {max_bytes}. The transcript binding is the "
            f"most common cause; consider trimming it via "
            f"--ignore-tag-for-placeholders or use a narrower placeholder."
        )
    if size_bytes > PROMPT_SIZE_WARNING_BYTES:
        payload["verdict"] = "warn"
        print(
            f"warning: rendered prompt is {size_bytes} bytes "
            f"(>{PROMPT_SIZE_WARNING_BYTES} bytes). Large prompts correlate "
            f"with stream-idle timeouts; the transcript binding is the most "
            f"common cause.",
            file=sys.stderr,
        )
    return payload


def _prepare_phase(args: argparse.Namespace) -> dict[str, Any]:
    template = Path(args.template).resolve()
    _reject_skill_md_as_template(template)
    workdir = Path(args.workdir).resolve()
    artifacts_dir = (
        Path(args.artifacts_dir).resolve()
        if args.artifacts_dir
        else Path(tempfile.mkdtemp(prefix=f"trycycle-phase-{args.phase}-")).resolve()
    )
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    transcript_cli, transcript_paths = _prepare_transcripts(
        args,
        artifacts_dir,
        workdir=workdir,
    )
    prompt_path = _build_prompt(args, artifacts_dir, transcript_paths)
    max_prompt_bytes = getattr(args, "max_prompt_bytes", None)
    prompt_size = _check_prompt_size(prompt_path, max_prompt_bytes)

    payload = {
        "status": "prepared",
        "phase": args.phase,
        "artifacts_dir": str(artifacts_dir),
        "result_path": str(artifacts_dir / "result.json"),
        "template_path": str(Path(args.template).resolve()),
        "workdir": str(workdir),
        "prompt_path": str(prompt_path),
        "transcript_cli": transcript_cli,
        "transcript_paths": transcript_paths,
        "prompt_size": prompt_size,
    }
    if args.canary:
        payload["canary"] = args.canary
    _write_json(Path(payload["result_path"]), payload)
    return payload


def _command_prepare(args: argparse.Namespace) -> int:
    payload = _prepare_phase(args)
    _emit_json(payload)
    return 0


def _dispatch_via_runner(
    *,
    phase: str,
    prompt_path: str,
    workdir: Path,
    dispatch_dir: Path,
    backend: str,
    effort: str | None = None,
    profile: str | None = None,
    model: str | None = None,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """Shell out to subagent_runner.py run and return (dispatch_payload, returncode)."""
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(SUBAGENT_RUNNER),
        "run",
        "--phase",
        phase,
        "--prompt-file",
        prompt_path,
        "--workdir",
        str(workdir),
        "--artifacts-dir",
        str(dispatch_dir),
        "--backend",
        backend,
    ]
    if effort:
        command.extend(["--effort", effort])
    if profile:
        command.extend(["--profile", profile])
    if model:
        command.extend(["--model", model])
    if timeout_seconds is not None:
        command.extend(["--timeout-seconds", str(timeout_seconds)])
    if dry_run:
        command.append("--dry-run")

    dispatch_result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    dispatch_payload: dict[str, Any] | None = None
    if dispatch_result.stdout.strip():
        try:
            dispatch_payload = json.loads(dispatch_result.stdout)
        except json.JSONDecodeError as exc:
            raise PhaseError(
                f"subagent runner returned non-JSON stdout: {dispatch_result.stdout!r}"
            ) from exc
    if dispatch_payload is None:
        result_path = dispatch_dir / "result.json"
        if result_path.exists():
            dispatch_payload = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            raise PhaseError(
                dispatch_result.stderr.strip() or "subagent runner returned no result"
            )
    return dispatch_payload, dispatch_result.returncode


def _command_run(args: argparse.Namespace) -> int:
    payload = _prepare_phase(args)
    dispatch_dir = Path(payload["artifacts_dir"]) / "dispatch"

    dispatch_payload, returncode = _dispatch_via_runner(
        phase=args.phase,
        prompt_path=payload["prompt_path"],
        workdir=Path(args.workdir).resolve(),
        dispatch_dir=dispatch_dir,
        backend=args.backend,
        effort=args.effort,
        profile=args.profile,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )

    final_payload = {
        **payload,
        "status": dispatch_payload["status"],
        "dispatch": dispatch_payload,
        "result_path": str(Path(payload["artifacts_dir"]) / "result.json"),
    }
    _write_json(Path(final_payload["result_path"]), final_payload)
    _emit_json(final_payload)
    return 0 if returncode == 0 else returncode


def _command_run_sequence(args: argparse.Namespace) -> int:
    """Dispatch a phase as a sequence of bounded micro-step subagent calls.

    Each step renders its own template but shares one transcript (fetched once)
    and one scratch file ({PHASE_STATE_PATH}) for inter-step state handoff.
    A subagent can write SEQUENCE_DONE_SENTINEL into the scratch file to
    short-circuit any remaining steps whose names are in
    --short-circuit-on-sentinel.
    """
    shared_artifacts_dir = (
        Path(args.artifacts_dir).resolve()
        if args.artifacts_dir
        else Path(tempfile.mkdtemp(prefix=f"trycycle-seq-{args.phase}-")).resolve()
    )
    shared_artifacts_dir.mkdir(parents=True, exist_ok=True)

    scratch_dir = shared_artifacts_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    phase_state_path = scratch_dir / "phase-state.md"
    if not phase_state_path.exists():
        phase_state_path.touch()

    if args.single_shot:
        steps: list[tuple[str, Path]] = [
            ("single-shot", Path(args.single_shot).resolve())
        ]
    else:
        if not args.steps:
            raise PhaseError("run-sequence requires --steps or --single-shot.")
        template_dir = Path(args.template_dir).resolve()
        step_names = [name.strip() for name in args.steps.split(",") if name.strip()]
        steps = []
        for name in step_names:
            template_path = template_dir / f"prompt-{args.phase}-{name}.md"
            if not template_path.exists():
                raise PhaseError(
                    f"Micro-step template not found: {template_path}. "
                    f"Expected prompt-<phase>-<step>.md under --template-dir."
                )
            steps.append((name, template_path))

    short_circuit_set = set(args.short_circuit_on_sentinel or [])
    max_deadline = args.max_sequence_seconds

    # Resolve transcripts once; subsequent steps use --set-file to avoid re-lookup.
    transcript_bindings: dict[str, str] = {}
    if args.transcript_placeholder:
        _, transcript_bindings = _prepare_transcripts(
            args, shared_artifacts_dir, workdir=Path(args.workdir).resolve()
        )

    sentinel_seen = False
    step_results: list[dict[str, Any]] = []
    overall_status = "ok"
    final_reply_path: str | None = None
    sequence_started_at = time.monotonic()

    for step_name, template_path in steps:
        if sentinel_seen and step_name in short_circuit_set:
            step_results.append(
                {
                    "step": step_name,
                    "status": "skipped",
                    "reason": "sentinel_seen",
                }
            )
            continue

        step_artifacts_dir = shared_artifacts_dir / "steps" / step_name
        step_artifacts_dir.mkdir(parents=True, exist_ok=True)

        step_args = argparse.Namespace(
            phase=(
                args.phase
                if step_name == "single-shot"
                else f"{args.phase}-{step_name}"
            ),
            template=str(template_path),
            workdir=args.workdir,
            artifacts_dir=str(step_artifacts_dir),
            set=list(args.set) + [f"PHASE_STATE_PATH={phase_state_path}"],
            set_file=list(args.set_file)
            + [f"{name}={path}" for name, path in transcript_bindings.items()],
            transcript_placeholder=[],
            transcript_cli=args.transcript_cli,
            transcript_search_root=args.transcript_search_root,
            canary=args.canary,
            require_nonempty_tag=list(args.require_nonempty_tag),
            ignore_tag_for_placeholders=list(args.ignore_tag_for_placeholders),
            max_prompt_bytes=args.max_prompt_bytes,
        )

        prepare_payload = _prepare_phase(step_args)
        dispatch_dir = Path(prepare_payload["artifacts_dir"]) / "dispatch"

        step_timeout_seconds = _resolve_step_timeout_seconds(
            template_path, args.timeout_seconds
        )

        dispatch_payload, returncode = _dispatch_via_runner(
            phase=step_args.phase,
            prompt_path=prepare_payload["prompt_path"],
            workdir=Path(args.workdir).resolve(),
            dispatch_dir=dispatch_dir,
            backend=args.backend,
            effort=args.effort,
            profile=args.profile,
            model=args.model,
            timeout_seconds=step_timeout_seconds,
            dry_run=args.dry_run,
        )

        final_reply_path = dispatch_payload.get("reply_path", final_reply_path)
        step_status = dispatch_payload["status"]
        step_results.append(
            {
                "step": step_name,
                "status": step_status,
                "dispatch": dispatch_payload,
                "prompt_path": prepare_payload["prompt_path"],
                "timeout_seconds": step_timeout_seconds,
            }
        )

        if step_status != "ok":
            overall_status = step_status
            break

        # Check sentinel after each successful step.
        try:
            if SEQUENCE_DONE_SENTINEL in phase_state_path.read_text(encoding="utf-8"):
                sentinel_seen = True
        except OSError:
            pass

        if (
            max_deadline > 0
            and (time.monotonic() - sequence_started_at) > max_deadline
        ):
            overall_status = "escalate_to_user"
            step_results.append(
                {
                    "step": "_deadline",
                    "status": "escalate_to_user",
                    "reason": f"sequence exceeded {max_deadline}s",
                }
            )
            break

    final_payload = {
        "status": overall_status,
        "phase": args.phase,
        "artifacts_dir": str(shared_artifacts_dir),
        "phase_state_path": str(phase_state_path),
        "steps": step_results,
        "final_reply_path": final_reply_path,
        "sentinel_seen": sentinel_seen,
        "duration_seconds": round(time.monotonic() - sequence_started_at, 3),
    }
    result_path = shared_artifacts_dir / "sequence-result.json"
    _write_json(result_path, final_payload)
    final_payload["result_path"] = str(result_path)
    _emit_json(final_payload)
    return 0 if overall_status == "ok" else 1


def _add_prepare_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--phase", required=True, help="Logical trycycle phase name.")
    parser.add_argument("--template", required=True, help="Prompt template path.")
    parser.add_argument("--workdir", required=True, help="Worktree or repo path.")
    parser.add_argument(
        "--artifacts-dir",
        help="Directory for prompt, transcript, and result artifacts.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Bind a literal placeholder value for prompt rendering.",
    )
    parser.add_argument(
        "--set-file",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Bind a placeholder value from an existing UTF-8 file.",
    )
    parser.add_argument(
        "--transcript-placeholder",
        action="append",
        default=[],
        metavar="NAME",
        help="Bind the current session transcript to this placeholder name.",
    )
    parser.add_argument(
        "--transcript-cli",
        choices=["auto", "codex-cli", "claude-code", "kimi-cli", "opencode"],
        default="auto",
        help="Transcript provider to use for transcript placeholders.",
    )
    parser.add_argument(
        "--transcript-search-root",
        type=Path,
        help="Override transcript search root for testing or debugging.",
    )
    parser.add_argument(
        "--canary",
        help="Existing transcript canary to use when direct lookup is unavailable.",
    )
    parser.add_argument(
        "--require-nonempty-tag",
        action="append",
        default=[],
        metavar="TAG",
        help="Require a rendered prompt tag to contain non-empty content.",
    )
    parser.add_argument(
        "--ignore-tag-for-placeholders",
        action="append",
        default=[],
        metavar="TAG",
        help="Ignore placeholder-like text inside this rendered tag.",
    )
    parser.add_argument(
        "--max-prompt-bytes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Error out if the rendered prompt exceeds N bytes. When omitted,"
            f" prompts over {PROMPT_SIZE_WARNING_BYTES} bytes log a warning"
            " to stderr but proceed."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and optionally dispatch a trycycle phase prompt.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare",
        help="Build transcript and prompt artifacts for a phase.",
    )
    _add_prepare_arguments(prepare_parser)
    prepare_parser.set_defaults(func=_command_prepare)

    run_parser = subparsers.add_parser(
        "run",
        help="Prepare a phase prompt, then dispatch it through the fallback subagent runner.",
    )
    _add_prepare_arguments(run_parser)
    run_parser.add_argument(
        "--backend",
        choices=["auto", "host", "codex", "claude", "kimi", "opencode"],
        default="auto",
        help="Subagent backend selection policy.",
    )
    run_parser.add_argument(
        "--effort",
        choices=["low", "medium", "high", "max"],
        help="Reasoning effort hint for the subagent backend.",
    )
    run_parser.add_argument(
        "--profile",
        help="Codex only. Advanced profile override forwarded to subagent_runner.py.",
    )
    run_parser.add_argument(
        "--model",
        help="Advanced exact model override forwarded to subagent_runner.py. Use only with a valid exact backend model name.",
    )
    run_parser.add_argument(
        "--timeout-seconds",
        type=int,
        help="Override the runner timeout in seconds. If omitted, subagent_runner.py phase defaults apply.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare normally, then dry-run the fallback subagent dispatch.",
    )
    run_parser.set_defaults(func=_command_run)

    run_sequence_parser = subparsers.add_parser(
        "run-sequence",
        help=(
            "Dispatch a phase as a sequence of bounded micro-step subagent "
            "calls with a shared scratch file for state handoff."
        ),
    )
    run_sequence_parser.add_argument(
        "--phase",
        required=True,
        help="Logical trycycle phase name (e.g. planning-initial).",
    )
    run_sequence_parser.add_argument(
        "--steps",
        default="",
        help=(
            "Comma-separated micro-step names. Templates are resolved as "
            "<template-dir>/prompt-<phase>-<step>.md. Ignored when "
            "--single-shot is passed."
        ),
    )
    run_sequence_parser.add_argument(
        "--template-dir",
        default=str(Path(__file__).resolve().parent.parent / "subagents"),
        help="Directory holding prompt-<phase>-<step>.md templates.",
    )
    run_sequence_parser.add_argument(
        "--single-shot",
        metavar="TEMPLATE_PATH",
        help=(
            "Escape hatch: dispatch a single prompt rendered from this "
            "template path instead of stepping through --steps. Useful for "
            "short tasks where decomposition overhead isn't warranted."
        ),
    )
    run_sequence_parser.add_argument(
        "--short-circuit-on-sentinel",
        action="append",
        default=[],
        metavar="STEP_NAME",
        help=(
            "When a subagent writes the sequence-done sentinel into the "
            "scratch file, skip any remaining steps with this name. "
            "Repeatable. Intended for variable-length phases like executing."
        ),
    )
    run_sequence_parser.add_argument(
        "--max-sequence-seconds",
        type=int,
        default=600,
        help=(
            "Overall wall-clock deadline for the whole sequence. "
            "0 disables the deadline. Default 600 (10 min)."
        ),
    )
    run_sequence_parser.add_argument(
        "--workdir", required=True, help="Worktree or repo path."
    )
    run_sequence_parser.add_argument(
        "--artifacts-dir",
        help=(
            "Shared directory for every step's artifacts plus the scratch "
            "file. Created under /tmp if omitted."
        ),
    )
    run_sequence_parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Bind a literal placeholder value; applied to every step.",
    )
    run_sequence_parser.add_argument(
        "--set-file",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Bind a placeholder from a UTF-8 file; applied to every step.",
    )
    run_sequence_parser.add_argument(
        "--transcript-placeholder",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Bind the current session transcript under this placeholder. "
            "Resolved once up-front and reused across all steps."
        ),
    )
    run_sequence_parser.add_argument(
        "--transcript-cli",
        choices=["auto", "codex-cli", "claude-code", "kimi-cli", "opencode"],
        default="auto",
    )
    run_sequence_parser.add_argument(
        "--transcript-search-root",
        type=Path,
    )
    run_sequence_parser.add_argument("--canary")
    run_sequence_parser.add_argument(
        "--require-nonempty-tag",
        action="append",
        default=[],
        metavar="TAG",
    )
    run_sequence_parser.add_argument(
        "--ignore-tag-for-placeholders",
        action="append",
        default=[],
        metavar="TAG",
    )
    run_sequence_parser.add_argument(
        "--max-prompt-bytes",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Per-step prompt-size cap. Error if any step's rendered prompt"
            " exceeds N bytes. When omitted, large prompts log a warning."
        ),
    )
    run_sequence_parser.add_argument(
        "--backend",
        choices=["auto", "host", "codex", "claude", "kimi", "opencode"],
        default="auto",
    )
    run_sequence_parser.add_argument(
        "--effort", choices=["low", "medium", "high", "max"]
    )
    run_sequence_parser.add_argument("--profile")
    run_sequence_parser.add_argument("--model")
    run_sequence_parser.add_argument("--timeout-seconds", type=int)
    run_sequence_parser.add_argument("--dry-run", action="store_true")
    run_sequence_parser.set_defaults(func=_command_run_sequence)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except PhaseError as exc:
        print(f"run_phase error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
