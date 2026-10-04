# codex-cli-mcp-slim

A thin, auditable [MCP](https://modelcontextprotocol.io) server wrapping the [Codex CLI](https://github.com/openai/codex) (`codex exec`).

[![PyPI version](https://img.shields.io/pypi/v/codex-cli-mcp-slim.svg)](https://pypi.org/project/codex-cli-mcp-slim/)
[![Python versions](https://img.shields.io/pypi/pyversions/codex-cli-mcp-slim.svg)](https://pypi.org/project/codex-cli-mcp-slim/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/tksfjt1024/codex-cli-mcp-slim/actions/workflows/ci.yml/badge.svg)](https://github.com/tksfjt1024/codex-cli-mcp-slim/actions/workflows/ci.yml)

## Why

`codex mcp-server`, the command that let other MCP clients call Codex, is
deprecated, and its removal has been merged upstream
([openai/codex#42993](https://github.com/openai/codex/pull/42993)): releases up
to 0.153.x still ship it, later ones will not. Its replacement, the Codex app
server, speaks its own JSON-RPC protocol rather than MCP. This server keeps the old
integration point alive: it exposes the same two tools, `codex` and
`codex-reply`, and runs `codex exec` underneath. `codex exec` is the Codex CLI's
non-interactive mode: one prompt in, the agent works on its own, one final
message out.

When you add an MCP server to your AI coding tool, every prompt and code snippet
you send flows through that wrapper. Most CLI-wrapping MCP servers are small,
individually maintained packages, and recent supply-chain incidents
(`xz-utils`, `postmark-mcp`, the npm `chalk`/`debug` compromise) show that
"small and useful" is not the same as "safe to trust blindly."

This project takes the opposite stance: instead of asking you to trust it, it
tries to be **easy to audit**.

- **Single file** — the whole server is `src/codex_cli_mcp_slim/server.py`,
  readable end-to-end in one sitting
- **One third-party dependency** (`mcp`) — minimal supply-chain surface
- **Faithful CLI mapping** — every typed parameter mirrors a real `codex exec`
  flag by name, so it is obvious which flags an invocation actually sets
- **Prompt over stdin** — the prompt never appears in the process list and is
  not bounded by the argv size limit
- **Forward-compatible** — any new or uncommon `codex exec` flag is reachable via
  `extra_args` without touching this server
- **Configurable binary path** — `$CODEX_CMD` lets you swap or wrap the `codex`
  binary
- **Transparent** — every invocation logs the exact argv to stderr

Read `server.py` before you install. That is the point.

## Prerequisites

- The `codex` CLI installed and on `$PATH` (or pointed to via `$CODEX_CMD`). See
  the [official Codex CLI repository](https://github.com/openai/codex). This server
  always passes `--json` and reads the prompt from stdin (`codex exec -`), both of
  which `codex exec` documents.
- `codex` already **authenticated** — this wrapper does not manage login; it
  surfaces `codex`'s own error output if the CLI is not ready.

## Installation

```bash
# Run directly without installing
uvx codex-cli-mcp-slim

# Install from PyPI
pip install codex-cli-mcp-slim

# Run from GitHub HEAD
uvx --from git+https://github.com/tksfjt1024/codex-cli-mcp-slim codex-cli-mcp-slim
```

## Usage as an MCP server

### Claude Code

```bash
claude mcp add codex uvx codex-cli-mcp-slim
```

Or manually in `~/.claude.json`:

```json
{
  "mcpServers": {
    "codex": {
      "type": "stdio",
      "command": "uvx",
      "args": ["codex-cli-mcp-slim"]
    }
  }
}
```

If `codex` is not on the launching process's `$PATH`, point `$CODEX_CMD` at it:

```json
{
  "mcpServers": {
    "codex": {
      "type": "stdio",
      "command": "uvx",
      "args": ["codex-cli-mcp-slim"],
      "env": { "CODEX_CMD": "/absolute/path/to/codex" }
    }
  }
}
```

### Replacing `codex mcp-server`

An entry that used to launch `codex mcp-server` keeps its server name and its
tool names; only `command` and `args` change. Before:

```json
{
  "mcpServers": {
    "codex": {
      "type": "stdio",
      "command": "codex",
      "args": ["mcp-server"]
    }
  }
}
```

After:

```json
{
  "mcpServers": {
    "codex": {
      "type": "stdio",
      "command": "uvx",
      "args": ["codex-cli-mcp-slim"]
    }
  }
}
```

Parameter names differ from the old server where `codex exec` names the flag
differently: `cwd` is now `cd` (the `-C/--cd` flag), and `codex-reply` takes
`thread_id` instead of `threadId`. The result's `structuredContent` field keeps
the shape the old server returned, `{"threadId": ..., "content": ...}`. It
gains an `ignoredParameters` key only when the server's own flags overrode a
per-call parameter (see [Server-level flags](#server-level-flags)).

### Other MCP clients

Any MCP-compatible client can launch the server via stdio:

```bash
uvx codex-cli-mcp-slim
```

## Server-level flags

Everything on the server's own command line is placed right after `codex exec`
on every invocation. One MCP-client entry can therefore pin a reasoning effort, a
model or a working directory for all of its calls. Two entries that differ only
in reasoning effort look like this:

```json
{
  "mcpServers": {
    "codex-medium": {
      "type": "stdio",
      "command": "uvx",
      "args": ["codex-cli-mcp-slim", "-c", "model_reasoning_effort=medium"]
    },
    "codex-high": {
      "type": "stdio",
      "command": "uvx",
      "args": ["codex-cli-mcp-slim", "-c", "model_reasoning_effort=high"]
    }
  }
}
```

A call to `codex-high` runs
`codex exec -c model_reasoning_effort=high [per-call flags] --json -`. Per-call
flags come after the server-level ones. `-c` and `--add-dir` may repeat. For
`-c` the last one wins, so a per-call `config` entry overrides a server-level
`-c`.

The other typed flags (`-C`, `-m`, `--sandbox`, `-p`, `--ephemeral`,
`--skip-git-repo-check`) may appear only once: `codex` exits with "cannot be used
multiple times" on a second one. When the server's command line already sets one
of them, in any spelling (for example `-C DIR`, `-CDIR`, `--cd=DIR`), the
server-level value wins. A token that is another flag's value (the `-m` in
`-c -m`) or that comes after `--` does not count as setting a flag. The `codex`
tool leaves the matching per-call parameter (`cd`, `model`, `sandbox`,
`profile`, `ephemeral`, `skip_git_repo_check`) out of the command and names it
in the result together with the server-level value used instead: in the text
after the `[codex]` line (`cd="/other"; this server runs with -C /srv`) and in
`structuredContent.ignoredParameters`. Its schema tells the caller which
parameters the server already sets, and to what. `codex-reply` keeps such
parameters, because after `resume` they are the subcommand's own flags and
`codex` accepts them there. `extra_args` is passed through unchecked, so a flag
repeated through it still fails with `codex`'s own error.

## Tool: `codex`

Runs a single non-interactive Codex session (`codex exec`). `codex` is an
agentic assistant: it reads and, depending on the sandbox, edits files in the
working directory to fulfil the request, then prints its final message.

The tool returns that final message followed by one metadata line:

```
[codex] thread_id=019a2b3c-1d4e-7f60-8a9b-0c1d2e3f4a5b status=completed input_tokens=13894 cached_input_tokens=11904 output_tokens=612
```

`thread_id` and `status` are always present; the token fields appear when the
run reported them. A per-call parameter that a server-level flag overrode is
named after this line, with the value used instead (see
[Server-level flags](#server-level-flags)).
`isError` is the flag on an MCP tool result that tells the
client a call failed. This server sets it when `codex` exited non-zero, when the
subprocess timed out, and when the turn itself failed. The last case matters
because `codex exec` exits 0 after a failure inside the model API; the tool
result then carries the error text instead of coming back as a successful call:

```
[ERROR] codex failed

returncode=0

errors:
Unsupported value: 'none' is not supported with the ... model.

[codex] thread_id=019a2b3c-... status=failed

argv: ['codex', 'exec', '--json', '-']
```

Pass the `thread_id` to `codex-reply` to continue the same session.

| Parameter             | Type     | Description                                                                                          |
| --------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `prompt` (required)   | string   | Prompt sent verbatim to `codex` on stdin                                                             |
| `cd`                  | string   | Pass `-C <DIR>`: the working directory; defaults to the server's own                                 |
| `model`               | string   | Pass `-m <MODEL>`                                                                                    |
| `config`              | string[] | `key=value` overrides; each maps to one `-c` (repeatable, last wins)                                |
| `sandbox`             | string   | Pass `--sandbox`: `read-only`, `workspace-write` or `danger-full-access`. See note below             |
| `add_dir`             | string[] | Extra writable directories; each maps to one `--add-dir` (repeatable, not comma-joined)              |
| `profile`             | string   | Pass `-p <PROFILE>`                                                                                  |
| `ephemeral`           | bool     | Pass `--ephemeral` (do not write the session transcript codex keeps under `$CODEX_HOME/sessions`)   |
| `skip_git_repo_check` | bool     | Pass `--skip-git-repo-check` (allow a working directory outside a git repository)                    |
| `extra_args`          | string[] | Raw CLI flags appended verbatim. Do not pass `--json` or a prompt; the server adds both              |
| `env`                 | object   | Extra environment variables for the `codex` subprocess                                               |
| `timeout_seconds`     | int      | Hard wall-clock timeout for the subprocess, 30 to 3600 (default 1800)                                |

Unknown parameters are refused rather than ignored, so a call that still uses
the old server's `cwd` gets an error naming `cd` instead of running in the
wrong directory.

### Security note: `sandbox`

`codex exec` reads its sandbox mode from its own configuration file
(`~/.codex/config.toml` by default) unless `--sandbox` is given.
`danger-full-access` removes the filesystem and network sandbox entirely;
`workspace-write` makes the working directory (and any `add_dir`) writable.
`--sandbox` overrides only the mode; whether `workspace-write` gets network
access still follows the `[sandbox_workspace_write]` section of `config.toml`.
The parameter mirrors the flag so that whichever mode a call runs
under is visible in the arguments and in the logged argv. This server does not
pass `--dangerously-bypass-approvals-and-sandbox`; reach it via `extra_args` if
you really mean it.

## Tool: `codex-reply`

Continues a previous session (`codex exec resume <THREAD_ID>`) with a follow-up
prompt and returns the new final message. Only the flags `codex exec resume`
accepts are exposed, so `cd`, `sandbox`, `add_dir` and `profile` are refused
here. The working directory and sandbox of a reply come from the current
configuration, that is, the server-level flags and `config.toml`, not from the
original session.

| Parameter               | Type     | Description                                                       |
| ----------------------- | -------- | ----------------------------------------------------------------- |
| `thread_id` (required)  | string   | The `thread_id` from a previous result's `[codex]` line           |
| `prompt` (required)     | string   | Follow-up prompt, sent on stdin                                   |
| `model`                 | string   | Pass `-m <MODEL>`                                                 |
| `config`                | string[] | `key=value` overrides; each maps to one `-c`                      |
| `ephemeral`             | bool     | Pass `--ephemeral`                                                |
| `skip_git_repo_check`   | bool     | Pass `--skip-git-repo-check`                                      |
| `extra_args`            | string[] | Raw CLI flags appended verbatim                                   |
| `env`                   | object   | Extra environment variables for the `codex` subprocess            |
| `timeout_seconds`       | int      | Hard wall-clock timeout for the subprocess, 30 to 3600 (default 1800) |

## Timeout configuration

`timeout_seconds` is this wrapper's hard wall-clock limit (default 1800, or
`$CODEX_CLI_MCP_SLIM_TIMEOUT`). On timeout, the wrapper kills the subprocess's
whole process group and then waits up to 20 additional seconds to collect any
buffered output and reap the process, so the effective ceiling is
`timeout_seconds + 20`. A timed-out call is flagged `isError` and carries
whatever `codex` had printed so far.

## Forward-compatibility example

If a future `codex exec` release adds a new flag (say `--super-mode`), use it
immediately without updating this server:

```jsonc
{
  "name": "codex",
  "arguments": {
    "prompt": "...",
    "extra_args": ["--super-mode"]
  }
}
```

## Configuration

| Environment variable           | Default | Purpose                                  |
| ------------------------------ | ------- | ---------------------------------------- |
| `CODEX_CMD`                    | `codex` | Path to the `codex` CLI binary           |
| `CODEX_CLI_MCP_SLIM_TIMEOUT`   | `1800`  | Default subprocess timeout in seconds    |
| `CODEX_CLI_MCP_SLIM_LOG_LEVEL` | `INFO`  | Logging level for stderr diagnostics     |

`codex` itself reads its configuration file and credentials from `$CODEX_HOME`
(`~/.codex` by default), so an MCP-client entry can point a server at a
dedicated configuration directory through its `env` block.

## Development

```bash
# Install dev dependencies
pip install -e ".[test,dev]"

# Lint
ruff check .

# Test
pytest
```

## License

[MIT](./LICENSE) © tksfjt1024
