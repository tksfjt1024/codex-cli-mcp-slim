"""codex_cli_mcp_slim: Thin, auditable MCP server wrapping the Codex CLI (`codex exec`).

Design goals:
  * Auditable: a single file with only one third-party dependency (`mcp`),
    readable end-to-end in one sitting.
  * Same tool names: the two tools are `codex` and `codex-reply`, the names the
    deprecated `codex mcp-server` exposed, so an MCP client configured for that
    server keeps its server and tool names after swapping the launch command.
  * Forward-compatible: any `codex exec` flag is reachable via `extra_args`
    without server changes; the binary itself can be swapped via $CODEX_CMD.
  * Safe: subprocess uses argv-list form (no shell), the prompt travels over
    stdin rather than argv, hard timeout, explicit env merge. Each invocation
    logs the exact argv to stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import sys
from importlib.metadata import version as _pkg_version
from typing import Any, NamedTuple

from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

logger = logging.getLogger("codex_cli_mcp_slim")

CODEX_CMD = os.environ.get("CODEX_CMD", "codex")
DEFAULT_TIMEOUT = int(os.environ.get("CODEX_CLI_MCP_SLIM_TIMEOUT", "1800"))

_PKG_NAME = "codex-cli-mcp-slim"

# Flags placed right after `codex exec` on every invocation. main() fills this from the
# server's own command line, so one MCP-client entry can pin a reasoning effort, a working
# directory or a model for every call it makes:
#
#     uvx codex-cli-mcp-slim -c model_reasoning_effort=high -C /srv/scratch
#
# `-c` and `--add-dir` may repeat (for `-c` the last one wins), so a per-call `config` can
# override a server-level `-c`. The flags in _SINGLE_USE_FLAGS may not: see there.
SERVER_ARGS: list[str] = []

# Per-call parameters whose flag codex accepts only once, with every spelling of that flag.
# A second occurrence makes `codex exec` exit 2 with "cannot be used multiple times", so when
# the server's own command line already carries one of these flags, the per-call parameter
# is left out of argv and the server-level value is used: whoever started the server fixed
# that configuration on purpose.
_SINGLE_USE_FLAGS: dict[str, tuple[str, ...]] = {
    "cd": ("-C", "--cd"),
    "model": ("-m", "--model"),
    "sandbox": ("-s", "--sandbox"),
    "profile": ("-p", "--profile"),
    "ephemeral": ("--ephemeral",),
    "skip_git_repo_check": ("--skip-git-repo-check",),
}


# The `codex exec` options (codex-cli 0.159.2) that take the next token as their value, so
# _params_set_by reads that token as a value and never as a flag of its own.
_VALUE_FLAGS = frozenset(
    {
        *("-c", "--config", "--enable", "--disable", "-i", "--image"),
        *("-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox"),
        *("-C", "--cd", "--add-dir", "--thread-source", "--output-schema", "--color"),
        *("-o", "--output-last-message"),
    }
)


class _ServerSetting(NamedTuple):
    """How the server's own command line sets one of the _SINGLE_USE_FLAGS parameters."""

    value: str | bool  # the flag's value, or True for a flag without one
    written: str  # the flag as that command line spells it, for example `-C /srv`


def _params_set_by(server_args: list[str]) -> dict[str, _ServerSetting]:
    """The _SINGLE_USE_FLAGS parameters whose flag already appears in `server_args`.

    A flag counts in every form codex parses as that flag: `--cd DIR`, `--cd=DIR`, `-C DIR`
    and the attached short forms `-CDIR` and `-C=DIR`. Short flags are case-sensitive, so the
    repeatable `-c` is not mistaken for `-C`. The token after a _VALUE_FLAGS option is its
    value (`-m` in `-c -m` is not the model flag), and nothing after `--` is a flag.
    """
    held: dict[str, _ServerSetting] = {}
    tokens = iter(server_args)
    for token in tokens:
        if token == "--":
            break
        if token in _VALUE_FLAGS:
            value = next(tokens, None)
            if value is None:
                break
            param = next((p for p, flags in _SINGLE_USE_FLAGS.items() if token in flags), None)
            if param is not None:
                held.setdefault(param, _ServerSetting(value, shlex.join([token, value])))
            continue
        for param, spellings in _SINGLE_USE_FLAGS.items():
            for flag in spellings:
                if token == flag:
                    held.setdefault(param, _ServerSetting(True, token))
                elif len(flag) == 2 and token.startswith(flag):
                    value = token[2:].removeprefix("=")
                    held.setdefault(param, _ServerSetting(value, shlex.quote(token)))
                elif token.startswith(flag + "="):
                    value = token[len(flag) + 1 :]
                    held.setdefault(param, _ServerSetting(value, shlex.quote(token)))
    return held


# What SERVER_ARGS sets among the _SINGLE_USE_FLAGS parameters. _set_server_args parses it
# once, together with SERVER_ARGS, so _build_argv, _invoke and on_list_tools read one answer.
_SERVER_SET: dict[str, _ServerSetting] = {}


def _set_server_args(server_args: list[str]) -> None:
    global SERVER_ARGS, _SERVER_SET
    SERVER_ARGS = list(server_args)
    _SERVER_SET = _params_set_by(SERVER_ARGS)


# The Server is built at the bottom of this file: v2 takes the handlers as constructor
# arguments, so they must already be defined by the time it is constructed.


def _build_argv(
    *,
    thread_id: str | None = None,
    cd: str | None = None,
    model: str | None = None,
    config: list[str] | None = None,
    sandbox: str | None = None,
    add_dir: list[str] | None = None,
    profile: str | None = None,
    ephemeral: bool = False,
    skip_git_repo_check: bool = False,
    extra_args: list[str] | None = None,
    server_args: list[str] | None = None,
) -> list[str]:
    if server_args is None:
        server_args, server_set = SERVER_ARGS, _SERVER_SET
    else:
        server_set = _params_set_by(server_args)
    argv: list[str] = [CODEX_CMD, "exec", *server_args]
    if thread_id:
        # `codex exec resume <id>` is a subcommand: it has to come before the per-call flags,
        # and it accepts only a subset of them (the `codex-reply` schema advertises just that
        # subset). Server-level flags stay in front of it on purpose: codex parses them as
        # `exec` options there, so a server pinned to a working directory keeps that
        # directory for replies as well.
        argv += ["resume", thread_id]
    # Per-call flags after `resume` are the subcommand's own, and codex accepts them there
    # even when the same flag sits among the server-level ones, so only a plain `exec` call
    # leaves out what the server already sets.
    held = {} if thread_id else server_set
    if cd and "cd" not in held:
        argv += ["-C", cd]
    if model and "model" not in held:
        argv += ["-m", model]
    for item in config or []:
        argv += ["-c", item]  # repeatable flag: one -c per key=value
    if sandbox and "sandbox" not in held:
        argv += ["--sandbox", sandbox]
    for d in add_dir or []:
        argv += ["--add-dir", d]  # repeatable flag: one --add-dir per directory
    if profile and "profile" not in held:
        argv += ["-p", profile]
    if ephemeral and "ephemeral" not in held:
        argv.append("--ephemeral")
    if skip_git_repo_check and "skip_git_repo_check" not in held:
        argv.append("--skip-git-repo-check")
    if extra_args:
        argv += list(extra_args)
    # --json is not optional for this server: the thread id, the failure of a turn and the
    # token usage exist only in the event stream. `-` makes codex read the prompt from
    # stdin, so the prompt never appears in the process list and is not bounded by the
    # argv size limit. Both stay last so that nothing in extra_args can capture `-` as its
    # value.
    argv += ["--json", "-"]
    return argv


async def _run_codex(
    *,
    argv: list[str],
    prompt: str,
    timeout: int,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    if env_overrides:
        env.update({k: str(v) for k, v in env_overrides.items()})

    logger.info("exec %s (timeout=%ss, prompt=%d chars)", argv, timeout, len(prompt))

    # stdin is a pipe of our own: the prompt goes down it and it is closed. The child must
    # never inherit the parent's stdin, which under the stdio MCP transport is the JSON-RPC
    # channel from the client; a child reading from that shared file description would
    # corrupt the channel and silently kill the server.
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # own session, so timeout cleanup can kill the group (POSIX only, no-op elsewhere)
            start_new_session=True,
        )
    except OSError as exc:
        return {
            "ok": False,
            "error": f"failed to launch codex binary: {exc} (check $CODEX_CMD)",
            "argv": argv,
        }

    # shield the task so a first-wait timeout doesn't cancel the second wait below
    communicate_task = asyncio.ensure_future(proc.communicate(prompt.encode()))
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=timeout
            )
        except asyncio.TimeoutError:
            if communicate_task.done():
                # narrow race: the process finished at the exact moment wait_for's timeout fired
                stdout, stderr = await communicate_task
                return {
                    "ok": proc.returncode == 0,
                    "returncode": proc.returncode,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "argv": argv,
                }
            _kill_process_group(proc)
            stdout, stderr = b"", b""
            with contextlib.suppress(asyncio.TimeoutError):
                # kill above closes the pipes, so communicate() reaches EOF with the buffered output
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communicate_task), timeout=10
                )
            # bounded backstop: a D-state descendant can't be force-killed, so don't block forever
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=10)
            return {
                "ok": False,
                "error": f"timeout after {timeout}s",
                "argv": argv,
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }
    except asyncio.CancelledError:
        _kill_process_group(proc)
        raise
    finally:
        if not communicate_task.done():
            communicate_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await communicate_task

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "argv": argv,
    }


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill codex's process group; a descendant that setsid/setpgid away from it is out of reach."""
    if sys.platform != "win32":
        # start_new_session=True makes codex pid==pgid, so killpg(proc.pid) works after it exits too
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    # any killpg failure (not just the two anticipated OSError subclasses) must still fall back here
    with contextlib.suppress(OSError):
        proc.kill()


def _parse_events(stdout: str) -> dict[str, Any]:
    """Fold the `codex exec --json` event stream into the few facts this server reports.

    The stream is one JSON object per line. Lines that are not JSON objects are skipped
    rather than rejected: the events this server needs are self-describing by `type`, and
    a future codex release adding lines it does not know must not turn every call into an
    error.
    """
    thread_id: str | None = None
    messages: list[str] = []
    errors: list[str] = []
    usage: dict[str, Any] | None = None
    turn_status: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                messages.append(str(item.get("text") or ""))
        elif kind == "turn.completed":
            turn_status = "completed"
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
        elif kind == "turn.failed":
            turn_status = "failed"
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            errors.append(str(message or error or event))
        elif kind == "error":
            errors.append(str(event.get("message") or event))
    # codex reports one failure twice, as an `error` event and again inside `turn.failed`.
    unique_errors = list(dict.fromkeys(errors))
    return {
        "thread_id": thread_id,
        "messages": messages,
        "errors": unique_errors,
        "usage": usage,
        "turn_status": turn_status,
        "saw_events": bool(thread_id or messages or unique_errors or turn_status),
    }


def _codex_trailer(parsed: dict[str, Any]) -> str:
    """One line of run metadata, appended under codex's own text.

    `thread_id` is here because `codex-reply` needs it and the event stream is the only
    place codex reports it. `status` is the turn's own verdict, which the exit code does
    not carry on its own: a turn that failed inside the model API still exits 0 when the
    process shut down cleanly (observed with an unsupported reasoning effort).
    """
    bits = [f"thread_id={parsed.get('thread_id') or 'UNKNOWN'}"]
    bits.append(f"status={parsed.get('turn_status') or 'UNKNOWN'}")
    usage = parsed.get("usage") or {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        if usage.get(key) is not None:
            bits.append(f"{key}={usage[key]}")
    return "[codex] " + " ".join(bits)


def _run_failed(result: dict[str, Any]) -> bool:
    """Whether the client should see this as a failed tool call.

    A non-zero exit is always a failure. The event stream is consulted on top of that
    because it is codex's own verdict on the turn rather than the shell's: a `turn.failed`
    event can arrive with exit code 0, and an `error` event without any agent message
    means the model never answered.
    """
    if not result["ok"]:
        return True
    parsed = _parse_events(result.get("stdout", ""))
    if parsed["turn_status"] == "failed":
        return True
    # The same emptiness test _format_result applies, so a run whose text opens with
    # [ERROR] is never handed back with is_error unset.
    return bool(parsed["errors"]) and not _response_text(parsed).strip()


def _response_text(parsed: dict[str, Any]) -> str:
    # codex may emit several agent messages in one turn; the last one is the answer.
    return parsed["messages"][-1] if parsed["messages"] else ""


def _format_result(result: dict[str, Any]) -> str:
    parsed = _parse_events(result.get("stdout", ""))
    if result["ok"] and parsed["turn_status"] != "failed":
        response = _response_text(parsed)
        if response.strip():
            parts = [f"{response.rstrip()}\n\n{_codex_trailer(parsed)}"]
            if parsed["errors"]:
                # An error the turn recovered from: keep it visible without costing the answer.
                parts.append("errors:\n" + "\n".join(parsed["errors"]))
            return "\n\n".join(parts)
        marker = "[ERROR]" if parsed["errors"] else "[WARNING]"
        parts = [f"{marker} codex exited successfully but produced no agent message."]
        if parsed["errors"]:
            parts.append("errors:\n" + "\n".join(parsed["errors"]))
        if parsed["saw_events"]:
            parts.append(_codex_trailer(parsed))
        elif result.get("stdout", "").strip():
            parts.append(f"stdout:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"stderr:\n{result['stderr']}")
        parts.append(f"argv: {result.get('argv')}")
        return "\n\n".join(parts)
    parts = [f"[ERROR] {result.get('error', 'codex failed')}"]
    if "returncode" in result:
        parts.append(f"returncode={result['returncode']}")
    if parsed["errors"]:
        parts.append("errors:\n" + "\n".join(parsed["errors"]))
    if parsed["saw_events"]:
        parts.append(_codex_trailer(parsed))
        if parsed["messages"]:
            parts.append(f"last agent message:\n{_response_text(parsed)}")
    elif result.get("stdout"):
        parts.append(f"stdout:\n{result['stdout']}")
    if result.get("stderr"):
        parts.append(f"stderr:\n{result['stderr']}")
    parts.append(f"argv: {result.get('argv')}")
    return "\n\n".join(parts)


_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")

_COMMON_PROPS: dict[str, Any] = {
    "prompt": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Prompt sent verbatim to codex on stdin. codex runs its full agentic loop "
            "and this server returns the final agent message."
        ),
    },
    "model": {
        "type": "string",
        "description": "Pass -m <MODEL>: the model slug for this call, overriding config.toml.",
    },
    "config": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Configuration overrides as key=value strings, one per element. Each entry maps "
            'to a separate codex -c flag (for example "model_reasoning_effort=high"). '
            "Values are parsed as TOML by codex, so quote strings that are not bare words."
        ),
    },
    "ephemeral": {
        "type": "boolean",
        "description": "Pass --ephemeral: do not persist the session's rollout file to disk.",
    },
    "skip_git_repo_check": {
        "type": "boolean",
        "description": (
            "Pass --skip-git-repo-check: allow running in a directory that is not inside "
            "a git repository. Without it codex refuses such a working directory."
        ),
    },
    "extra_args": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Raw CLI flags appended verbatim (one token per element). "
            "Use to reach new or uncommon codex exec flags without updating this server. "
            "Do not pass --json or a prompt: the server adds both."
        ),
    },
    "env": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Extra environment variables for the codex subprocess.",
    },
    "timeout_seconds": {
        "type": "integer",
        "minimum": 30,
        "maximum": 3600,
        "description": (
            "Hard wall-clock timeout for the codex subprocess in seconds "
            f"(default {DEFAULT_TIMEOUT})."
        ),
    },
}

_CODEX_PROPS: dict[str, Any] = {
    "prompt": _COMMON_PROPS["prompt"],
    "cd": {
        "type": "string",
        "description": (
            "Pass -C <DIR>: the working directory codex runs in. Defaults to this server's "
            "own working directory."
        ),
    },
    "model": _COMMON_PROPS["model"],
    "config": _COMMON_PROPS["config"],
    "sandbox": {
        "type": "string",
        "enum": list(_SANDBOX_MODES),
        "description": (
            "Pass --sandbox <MODE>: read-only, workspace-write or danger-full-access. "
            "Overrides sandbox_mode from config.toml; network access still follows the "
            "[sandbox_workspace_write] section there."
        ),
    },
    "add_dir": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Extra writable directories. Each entry maps to a separate codex --add-dir flag "
            "(repeatable, not comma-joined)."
        ),
    },
    "profile": {
        "type": "string",
        "description": "Pass -p <PROFILE>: the config.toml profile to load.",
    },
    "ephemeral": _COMMON_PROPS["ephemeral"],
    "skip_git_repo_check": _COMMON_PROPS["skip_git_repo_check"],
    "extra_args": _COMMON_PROPS["extra_args"],
    "env": _COMMON_PROPS["env"],
    "timeout_seconds": _COMMON_PROPS["timeout_seconds"],
}

_REPLY_PROPS: dict[str, Any] = {
    "thread_id": {
        "type": "string",
        "minLength": 1,
        "description": (
            "The thread to continue: the thread_id from a previous result's [codex] line. "
            "Maps to codex exec resume <THREAD_ID>."
        ),
    },
    "prompt": _COMMON_PROPS["prompt"],
    "model": _COMMON_PROPS["model"],
    "config": _COMMON_PROPS["config"],
    "ephemeral": _COMMON_PROPS["ephemeral"],
    "skip_git_repo_check": _COMMON_PROPS["skip_git_repo_check"],
    "extra_args": _COMMON_PROPS["extra_args"],
    "env": _COMMON_PROPS["env"],
    "timeout_seconds": _COMMON_PROPS["timeout_seconds"],
}

_CODEX_TOOL = Tool(
    name="codex",
    description=(
        "Run a single non-interactive Codex session (`codex exec`) with the given prompt and "
        "return its final message. codex is an agentic coding assistant that reads and, "
        "depending on the sandbox, edits files inside the working directory. Forward-compatible: "
        "unknown CLI flags can be passed via `extra_args`. The codex binary path is configurable "
        "via $CODEX_CMD."
    ),
    inputSchema={
        "type": "object",
        "properties": _CODEX_PROPS,
        "required": ["prompt"],
    },
)

_REPLY_TOOL = Tool(
    name="codex-reply",
    description=(
        "Continue a previous Codex session (`codex exec resume <THREAD_ID>`) with a follow-up "
        "prompt and return the new final message. Only the flags `codex exec resume` accepts "
        "are exposed; the working directory and sandbox come from the current configuration "
        "(this server's own flags and config.toml), not from the original session."
    ),
    inputSchema={
        "type": "object",
        "properties": _REPLY_PROPS,
        "required": ["thread_id", "prompt"],
    },
)

_BOOL_FIELDS = ("ephemeral", "skip_git_repo_check")
_STR_FIELDS = ("prompt", "thread_id", "cd", "model", "sandbox", "profile")
_STR_ARRAY_FIELDS = ("config", "add_dir", "extra_args")

# Parameter names the deprecated `codex mcp-server` used, mapped to what this server calls
# them. A client migrated from that server is the most likely source of an unknown key, and
# a working directory or thread id it passed under the old name must not be dropped on the
# floor: the call would run in the wrong directory or start a new thread without a word.
_RENAMED_FROM_MCP_SERVER = {
    "cwd": "cd",
    "threadId": "thread_id",
    "conversationId": "thread_id",
}


def _validate_args(
    args: dict[str, Any], *, required: tuple[str, ...], allowed: dict[str, Any]
) -> str | None:
    """Return the first schema violation, or None when the arguments are usable.

    The low-level Server advertises `inputSchema` and never applies it, so this walks the
    properties by hand. Checking only that the required keys are present is not enough:
    every value here reaches argv.
    """
    for key in args:
        if key in allowed:
            continue
        renamed = _RENAMED_FROM_MCP_SERVER.get(key)
        if renamed in allowed:
            return f"unknown parameter `{key}`; this server calls it `{renamed}`"
        return f"unknown parameter `{key}`"
    for key in required:
        # `is None`, not `not in`: an explicit null passes a presence check but then reaches
        # argv as the string "None".
        if args.get(key) is None:
            return f"`{key}` is required"
    for key in _STR_FIELDS:
        value = args.get(key)
        if value is not None and not isinstance(value, str):
            return f"`{key}` must be a string, got {type(value).__name__}"
    for key in required:
        if not args[key]:
            return f"`{key}` must not be empty"
    for key in _BOOL_FIELDS:
        value = args.get(key)
        # An exact type check rather than truthiness: `bool("false")` is True.
        if value is not None and not isinstance(value, bool):
            return f"`{key}` must be a boolean, got {type(value).__name__}"
    for key in _STR_ARRAY_FIELDS:
        value = args.get(key)
        if value is None:
            continue
        # A bare string satisfies `for d in add_dir` and `list(extra_args)` but iterates
        # character by character, so "docs" would expand into four --add-dir flags.
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return f"`{key}` must be an array of strings"
    for item in args.get("config") or []:
        # codex parses `-c key=value`; a bare key would be rejected there with a message
        # that no longer names the parameter the client got wrong.
        if "=" not in item:
            return f"`config` entries must be key=value strings, got {item!r}"
    sandbox = args.get("sandbox")
    if sandbox is not None and sandbox not in _SANDBOX_MODES:
        return f"`sandbox` must be one of {', '.join(_SANDBOX_MODES)}, got {sandbox!r}"
    env = args.get("env")
    if env is not None and (
        not isinstance(env, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    ):
        return "`env` must be an object mapping strings to strings"
    timeout = args.get("timeout_seconds")
    if timeout is not None:
        # bool subclasses int, so it has to be excluded before isinstance would accept it
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            return f"`timeout_seconds` must be an integer, got {type(timeout).__name__}"
        if not 30 <= timeout <= 3600:
            return f"`timeout_seconds` must be between 30 and 3600, got {timeout}"
    return None


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"[ERROR] {text}")], is_error=True)


async def _invoke(arguments: dict[str, Any] | None, *, reply: bool) -> CallToolResult:
    """The body of both tools, taking a plain dict so callers need no SDK request object."""
    args = dict(arguments or {})

    # `codex exec resume` does not take `cd`, `sandbox`, `add_dir` or `profile`; they are
    # absent from _REPLY_PROPS, so the unknown-key check refuses them by name, where codex's
    # own error would only name the flag.
    invalid = _validate_args(
        args,
        required=("thread_id", "prompt") if reply else ("prompt",),
        allowed=_REPLY_PROPS if reply else _CODEX_PROPS,
    )
    if invalid is not None:
        return _error_result(f"input validation: {invalid}")

    argv = _build_argv(
        thread_id=args.get("thread_id") if reply else None,
        cd=args.get("cd"),
        model=args.get("model"),
        config=args.get("config"),
        sandbox=args.get("sandbox"),
        add_dir=args.get("add_dir"),
        profile=args.get("profile"),
        ephemeral=bool(args.get("ephemeral", False)),
        skip_git_repo_check=bool(args.get("skip_git_repo_check", False)),
        extra_args=args.get("extra_args"),
    )

    result = await _run_codex(
        argv=argv,
        prompt=args["prompt"],
        timeout=int(args.get("timeout_seconds") or DEFAULT_TIMEOUT),
        env_overrides=args.get("env"),
    )
    parsed = _parse_events(result.get("stdout", ""))
    text = _format_result(result)
    # Name what _build_argv left out, and the value used instead, so a caller does not take a
    # run in the server's directory (say) for a run in the one it asked for.
    held = {} if reply else _SERVER_SET
    dropped = [param for param in _SINGLE_USE_FLAGS if param in held and args.get(param)]
    structured: dict[str, Any] | None = None
    if parsed["thread_id"]:
        # The same shape the deprecated `codex mcp-server` returned, so a client that reads
        # structuredContent.threadId keeps working.
        structured = {"threadId": parsed["thread_id"], "content": _response_text(parsed)}
    if dropped:
        lines = [
            f"{p}={json.dumps(args[p], ensure_ascii=False)}; "
            f"this server runs with {held[p].written}"
            for p in dropped
        ]
        logger.warning("per-call parameters ignored: %s", " | ".join(lines))
        text += (
            "\n\nignored, because this server's own command line already sets the flag "
            "and codex takes it only once:\n" + "\n".join(lines)
        )
        # A client that reads only structuredContent learns it here.
        structured = {
            **(structured or {}),
            "ignoredParameters": [
                {"name": p, "value": args[p], "serverValue": held[p].value} for p in dropped
            ],
        }
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
        is_error=_run_failed(result),
    )


async def codex(arguments: dict[str, Any] | None) -> CallToolResult:
    return await _invoke(arguments, reply=False)


async def codex_reply(arguments: dict[str, Any] | None) -> CallToolResult:
    return await _invoke(arguments, reply=True)


async def on_list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None
) -> ListToolsResult:
    # The caller cannot see the server's command line, so the `codex` schema says which
    # parameters it already fixes, rather than inviting values that _build_argv would drop.
    if not _SERVER_SET:
        return ListToolsResult(tools=[_CODEX_TOOL, _REPLY_TOOL])
    props = dict(_CODEX_PROPS)
    for param, setting in _SERVER_SET.items():
        props[param] = {
            **props[param],
            "description": (
                f"{props[param]['description']} This server's own command line already "
                f"sets {setting.written}, so a value passed here is ignored."
            ),
        }
    codex_tool = _CODEX_TOOL.model_copy(
        update={"input_schema": {**_CODEX_TOOL.input_schema, "properties": props}}
    )
    return ListToolsResult(tools=[codex_tool, _REPLY_TOOL])


async def on_call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
    # An escaping exception would become a JSON-RPC error, which clients treat as a transport
    # failure rather than as something to show the model. CancelledError derives from
    # BaseException, so peer cancellation still propagates and _run_codex's killpg still runs.
    try:
        if params.name == "codex":
            return await codex(params.arguments)
        if params.name == "codex-reply":
            return await codex_reply(params.arguments)
        return _error_result(f"unknown tool: {params.name}")
    except Exception as exc:
        logger.exception("%s failed", params.name)
        return _error_result(f"{type(exc).__name__}: {exc}")


server: Server = Server(
    _PKG_NAME,
    version=_pkg_version(_PKG_NAME),
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
)
# v2 attaches an OpenTelemetryMiddleware by default. It is a no-op without an exporter, but
# it puts a tracing layer on the path of every request and reads OTEL_* environment
# variables in-process. This file is meant to be the whole story, so the layer is dropped
# rather than left implicit; delete this line to get the SDK default back.
server.middleware.clear()


async def _amain() -> None:
    logging.basicConfig(
        level=os.environ.get("CODEX_CLI_MCP_SLIM_LOG_LEVEL", "INFO"),
        format="[codex-cli-mcp-slim] %(levelname)s %(message)s",
    )
    if SERVER_ARGS:
        logger.info("server-level codex exec flags: %s", SERVER_ARGS)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


_USAGE = """\
usage: codex-cli-mcp-slim [CODEX_EXEC_FLAGS...]

Start the MCP server on stdio. Every argument is placed right after `codex exec`
on every tool call (for example: -c model_reasoning_effort=high -C /srv/scratch).
"""


def main() -> None:
    # Everything on the server's own command line is handed to codex exec, verbatim, on
    # every call. The two exceptions exist only so that a person who runs the command by
    # hand to see what it is does not start a stdio server waiting on a terminal.
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(_USAGE, end="")
        return
    if sys.argv[1:] == ["--version"]:
        print(f"{_PKG_NAME} {_pkg_version(_PKG_NAME)}")
        return
    _set_server_args(sys.argv[1:])
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
