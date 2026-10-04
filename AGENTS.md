# AGENTS.md — codex-cli-mcp-slim

This file gives AI coding agents (Claude Code, Codex, Cursor, and other tools
that speak MCP) the project-specific context required to work on this
repository. Claude Code reads it via `CLAUDE.md`, which is a one-line
`@AGENTS.md` import shim.

## What this project is

A **thin bridge** that exposes the Codex CLI (`codex exec`) to MCP clients
(Claude Code, Cursor MCP, and other MCP-speaking tools), standing in for the
deprecated `codex mcp-server` command, whose removal is merged upstream
(openai/codex#42993) and lands in the release after 0.153.x.

- **Input**: two MCP tools, `codex` and `codex-reply` — the names the deprecated
  server exposed, so existing client configuration keeps its server and tool
  names. Parameters the old server named differently (`cwd`, `threadId`) are
  refused with a message naming the new name; nothing is silently ignored.
- **Output**: an argv-list subprocess invocation of `codex exec` (or
  `codex exec resume <THREAD_ID>` for `codex-reply`) with `--json` and the prompt
  on stdin. The binary path is configurable via `$CODEX_CMD`.

`codex` is an agentic coding assistant: given a working directory it
autonomously reads files, runs commands inside its sandbox and performs the
requested task. History management, caching and streaming are **deliberately
not provided**; `codex` keeps its own session rollouts, and `codex-reply` is the
only continuation this server offers.

## Architectural decisions (load-bearing principles)

This project starts from "**don't blindly route your prompts and code through
someone else's wrapper**." Recent supply-chain incidents (`xz-utils` backdoor,
`postmark-mcp` typosquat, npm `chalk`/`debug` compromise) inform every design
choice; **default toward auditability**.

- **Auditable**: the core implementation is a single file
  (`src/codex_cli_mcp_slim/server.py`) that anyone can read end-to-end in one
  sitting. If a feature would meaningfully grow this size, prefer **dropping or
  omitting** the feature over splitting the file.
- **Minimum dependencies**: `[project].dependencies` in `pyproject.toml` is
  **`mcp` only**, bounded to `>=2.0.0,<3`. Adding a direct dependency expands
  supply-chain surface; always check whether the standard library can do the job
  first. The upper bound is load-bearing: MCP Python SDK 2.0.0 changed the
  low-level `Server` API, and the next major will too. Raise it only in the same
  change that migrates to the next major API. `jsonschema` is a required
  dependency of `mcp` itself, which is why the argument checking in `server.py`
  is hand-written: declaring `jsonschema` directly would install nothing new, but
  it would spend the "direct dependencies are `mcp` only" rule on a job the
  standard library can do.
- **Faithful CLI mapping**: typed parameters mirror the actual `codex exec` flag
  names one-to-one (`cd` → `-C/--cd`, `model` → `-m`, `config` → `-c`,
  `sandbox` → `--sandbox`, `add_dir` → `--add-dir`, `profile` → `-p`,
  `ephemeral` → `--ephemeral`, `skip_git_repo_check` → `--skip-git-repo-check`,
  `thread_id` → `resume <THREAD_ID>`). Do **not** soften or rename flags: the
  parameter surface should make it obvious which CLI flags an invocation actually
  sets. The old server's `cwd` is not carried over for that reason.
- **Prompt over stdin**: `_build_argv` ends every argv with `--json -` and
  `_run_codex` writes the prompt down the child's stdin pipe. The prompt therefore
  never appears in `ps` output and is not bounded by the argv size limit. Do not
  move it back into argv.
- **`--json` is not optional**: the thread id, the failure of a turn and the token
  usage exist only in the event stream. `_parse_events` folds that stream into the
  few facts the server reports, and `_run_failed` reads the turn's own verdict
  from it. Skipping unknown lines is deliberate: a future codex release adding
  event types must not turn every call into an error.
- **Server-level flags**: everything on the server's own command line is placed
  right after `codex exec` on every call (`SERVER_ARGS`). This is the one thing
  the sibling projects (`antigravity-cli-mcp-slim`, `gemini-cli-mcp-slim`) do not
  have. It exists because `codex` reads the model, the reasoning effort and the
  working directory from `config.toml` or flags, and a client that wants several
  fixed configurations (one entry per reasoning effort, say) would otherwise need
  one `$CODEX_HOME` per entry.
- **Forward-compatible**: unknown / rare flags flow through
  `extra_args: string[]` verbatim. The server should not need to be re-released
  every time the upstream CLI grows a new flag.
- **Transparent**: every subprocess invocation logs its full argv to stderr
  (`logger.info("exec %s ...")`). The prompt is logged by length only.
- **No shell**: always use `asyncio.create_subprocess_exec` with an argv list.
  `shell=True` and string-concatenated commands are forbidden.
- **Configurable binary**: keep `$CODEX_CMD` working so users can swap or wrap the
  `codex` binary.

## Build / test / lint

`uv` is the local development driver (with `.venv/`); Docker is not used. CI
(`.github/workflows/ci.yml`) installs the same `optional-dependencies` groups
via `pip`, so local and CI use the same dependency set.

```bash
uv sync --extra test --extra dev      # local
pip install -e ".[test,dev]"          # mirrors CI
.venv/bin/pytest tests/ -v
.venv/bin/ruff check .
.venv/bin/ruff format .
```

**Required before commit**: `pytest` and `ruff check` both green. A type checker
(`mypy` / `pyright`) is intentionally not configured at this size — if you want
to add one, first verify it does not conflict with the "minimum dependencies"
principle.

## Release flow

Pushing a `v*` tag runs `.github/workflows/publish.yml`, which uploads to PyPI
via Trusted Publishing and then creates the GitHub release. Both are driven by
the tag; neither is done by hand.

- The single source of truth for the version is `pyproject.toml`'s
  `[project].version`. Bump it in the same commit as the change.
- `src/codex_cli_mcp_slim/__init__.py` exposes `__version__` via
  `importlib.metadata`, so it follows `pyproject.toml` automatically.
- `CHANGELOG.md` follows Keep a Changelog: add a `## [X.Y.Z]` entry per release,
  plus the matching `[X.Y.Z]: .../compare/vA.B.C...vX.Y.Z` link at the bottom.
- Once the bump and its CHANGELOG entry are on `main`:
  `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow picks it up.

### `CHANGELOG.md` is the release body, not only documentation

Both jobs run `.github/scripts/changelog-section.sh`, which prints the section headed
exactly `## [X.Y.Z]` — the tag with its leading `v` stripped — and exits non-zero when
that section is missing or empty. `github-release` turns its output into the release
body (plus a `**Full Changelog**` line pointing at the previous tag); `publish` runs it
purely as a gate, before it uploads anything.

One script rather than two transcriptions of the same rule is the point. PyPI refuses a
second upload of a version it already holds, so a gate that merely resembled the builder
could pass while the builder later failed — and that failure is the unrecoverable one:
the version would be live on PyPI with no way to release it short of a version bump.

## Common gotchas

### Unknown parameters are refused, not ignored

`_validate_args` rejects any key the tool's schema does not list, and names the
new name for the ones the deprecated server used (`cwd` → `cd`, `threadId` /
`conversationId` → `thread_id`). A silently dropped `cwd` would run the call in
the server's own directory, and a dropped `threadId` would start a new thread,
both without a word. The template this project was copied from ignores unknown
keys; the difference is deliberate.

### The low-level `Server` advertises the schema and never applies it

`inputSchema` is documentation to the SDK. `_validate_args` walks the properties
by hand, and the `except Exception` in `on_call_tool` turns a handler exception
into a `CallToolResult(isError=True)` rather than a JSON-RPC error, which clients
treat as a transport failure. Do not thin either out on the grounds that the
schema already says so. Checking only that the required keys are present is
specifically not enough: every value reaches argv, `bool("false")` is `True`,
and a string where a list belongs iterates character by character.

The `except Exception` is deliberately not `except BaseException`:
`CancelledError` has to keep propagating so peer cancellation still reaches
`_run_codex`'s process-group kill.

### The exit code is not the whole verdict on a codex run

`codex exec` exits 0 after a turn that failed inside the model API (observed
with a reasoning effort the model does not support). `_run_failed` therefore
reads `turn.failed` and `error` events on top of the exit code, and `is_error`
follows it. codex reports one such failure twice, as an `error` event and again
inside `turn.failed`; `_parse_events` dedupes them.

### `resume` is a subcommand, and it takes fewer flags

`codex exec resume <THREAD_ID>` has to sit before the per-call flags, and it
does not accept `-C`, `--sandbox`, `--add-dir` or `-p`. `_build_argv` places it
right after the server-level flags, and the unknown-key check refuses those four
parameters on `codex-reply` by name (they are absent from `_REPLY_PROPS`), where
codex's own error would only name the flag. Server-level flags stay in front of
`resume` on purpose: codex parses them as `exec` options there, so a server
pinned to a working directory keeps it for replies. A resumed turn takes its
working directory and sandbox from the current configuration, not from the
original session (`thread_resume_params_from_config` in `codex-rs/exec`).

### Keep `--json -` as the last two tokens

`-` tells codex to read the prompt from stdin. If anything in `extra_args` ends
with an option that takes a value, that option would capture whatever follows
it, so the server appends `--json -` after `extra_args`, not before. The
regression test is in `tests/test_basic.py`.

### Single-value flags may not repeat

`codex` exits 2 on a second `-C`, `-m`, `--sandbox`, `-p`, `--ephemeral` or
`--skip-git-repo-check` ("cannot be used multiple times"), whatever mix of
spellings carries them. `-c` and `--add-dir` repeat (for `-c` the last one
wins). On a `codex` call, `_build_argv` therefore leaves out a per-call parameter
whose flag the server-level flags already carry (`_SINGLE_USE_FLAGS`,
`_params_set_by`): the server-level value wins because whoever started the
server fixed it. `_params_set_by` reads the token after each option in
`_VALUE_FLAGS` as that option's value and stops at `--`, so the `-m` in `-c -m`
is not taken for the model flag. `main` parses the server's command line once
(`_set_server_args`), and `_build_argv`, `_invoke` and `on_list_tools` all read
that result (`_SERVER_SET`). Dropping a parameter silently would hide that the
call ran with a value other than it asked for. `_invoke` therefore names each
dropped parameter with the server-level value used instead, after the `[codex]`
line and in `structuredContent.ignoredParameters`, and `on_list_tools` writes
that value into the parameter's description in the `codex` schema.
`codex-reply` keeps such parameters, because after `resume` they are the
subcommand's own flags and codex accepts the repeat there.
`extra_args` is never inspected, so a repeat through it still gets codex's own
error. Regression tests are in `tests/test_basic.py`.

### Never let the subprocess inherit the parent's stdin

When the server runs over the **MCP stdio transport**, the parent process's
stdin is the **JSON-RPC channel**. A child that reads from or sets ioctl flags
on a shared stdin file description can corrupt that channel and silently kill
the server. `_run_codex` always opens its own pipe (`asyncio.subprocess.PIPE`),
writes the prompt down it and closes it. Regression test:
`tests/test_subprocess_stdin_isolation.py`.

### `codex exec` records the working directory as trusted

When the working directory is not listed under `[projects]` in `config.toml`
and the sandbox lets codex write there, `codex exec` appends
`[projects."<dir>"] trust_level = "trusted"` to `config.toml` on its own. A
`-c projects.<dir>.trust_level=...` override does not suppress that write; only
a pre-existing entry does. A server that runs many calls in one scratch
directory should list that directory in `config.toml` once, or `config.toml`
grows by one entry per distinct directory.

### Don't swallow `FileNotFoundError`

If `$CODEX_CMD` points to an empty string or a non-existent path, return an error
dict (`ok: False`) that **includes the argv** so users can diagnose what was
attempted. Silently masking the exception removes any signal about why the tool
isn't working.

### `codex` must be authenticated

`codex` runs against an authenticated account or an API key. This wrapper does
not manage auth; it surfaces `codex`'s own error output on failure. Ensure
`codex` is logged in before relying on the tool.

### Treat "add a direct dependency" as a re-think trigger

Per the "minimum dependencies" principle above, anything beyond `mcp` should be
weighed against a few dozen lines of standard-library code first.

### Process-group kill uses `proc.pid` directly, not `os.getpgid(proc.pid)`

`start_new_session=True` makes the subprocess both the session and
process-group leader, so `proc.pid` **is** the pgid. `_kill_process_group`
calls `os.killpg(proc.pid, ...)` directly; calling `os.getpgid(proc.pid)`
first fails with `ProcessLookupError` once the leader has already
exited/been reaped, even if descendants in the same group are still alive
holding the stdout/stderr pipes open. A descendant that calls
`setsid()`/`setpgid()` to leave the group is out of reach either way — a
known limitation, not a bug to fix by walking the process tree.

### Don't delete the `communicate_task.done()` narrow-race check in `_run_codex`

If the subprocess finishes at (almost) the exact moment the first
`wait_for`'s timeout fires, `communicate_task` may already be done by the
time the `except asyncio.TimeoutError` handler runs. `_run_codex` checks this
before killing anything and reports the real result instead of a false
timeout. Regression test:
`test_narrow_race_reports_success_when_process_finishes_at_timeout_edge`.

## References

- Public docs: `README.md`
- License: `LICENSE` (MIT)
