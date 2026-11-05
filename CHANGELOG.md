# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2]

### Added
- While `codex` runs, the server sends an MCP progress notification every 60 seconds
  to a client that passed a `progressToken` with the call. A client that aborts a
  call after a stretch with neither a response nor progress, as Claude Code does
  after 1800 seconds by default ("sent no response or progress for 1800s"), now
  waits for a long `codex` turn until it finishes or `timeout_seconds` runs out.
  `timeout_seconds` still defaults to 1800, so a call that should run longer than 30
  minutes also needs a larger `timeout_seconds` (30 to 3600 per call) or
  `$CODEX_CLI_MCP_SLIM_TIMEOUT`, which sets the default. A client that gives up
  after less than 60 seconds without progress is not covered. In each notification,
  `progress` counts the notifications and `message` gives the elapsed time
  (`codex running for 120s`). An error raised while sending a notification is logged
  to stderr and does not change the run or its result. Tool inputs, results and the
  default timeout are unchanged.

## [0.1.1]

### Fixed
- A `codex` call no longer fails with "cannot be used multiple times" when it passes
  `cd`, `model`, `sandbox`, `profile`, `ephemeral` or `skip_git_repo_check` and the
  server's own command line already sets the same flag (in any spelling, such as
  `-C DIR`, `--cd=DIR` or `-CDIR`; another flag's value and anything after `--` do
  not count). The server-level value is used and the per-call parameter is left out
  of the command. The result names that parameter with the server-level value, after
  the `[codex]` line and in `structuredContent.ignoredParameters`, and the `codex`
  tool's schema shows the value the server already sets. `codex-reply` is unchanged:
  after `resume` codex accepts the repeat. `config` and `add_dir` still stack on
  server-level `-c` and `--add-dir`. `extra_args` is still passed through unchecked.

## [0.1.0]

### Added
- Initial release: a single-file MCP server that runs `codex exec` and exposes the
  two tools the deprecated `codex mcp-server` had, `codex` and `codex-reply`, so an
  MCP client configured for that server keeps its server and tool names after swapping
  the launch command. Parameters that the old server named differently (`cwd`,
  `threadId`) are refused with a message naming the new parameter rather than
  ignored. `codex-reply` maps to `codex exec resume <THREAD_ID>`.
- Typed parameters mirror `codex exec` flags by name (`cd`, `model`, `config`,
  `sandbox`, `add_dir`, `profile`, `ephemeral`, `skip_git_repo_check`), with
  `extra_args` for anything else.
- The prompt is sent on stdin, so it never appears in the process list and is not
  bounded by the argv size limit.
- Every call runs with `--json`; the server returns the final agent message followed
  by one `[codex]` line carrying `thread_id`, the turn's `status` and token usage, and
  flags a failed turn as an `isError` result even when codex exited 0.
- Flags given on the server's own command line are placed in front of every
  `codex exec` invocation, so one MCP-client entry can pin a reasoning effort, a
  working directory or a model for all of its calls.

[Unreleased]: https://github.com/tksfjt1024/codex-cli-mcp-slim/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/tksfjt1024/codex-cli-mcp-slim/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/tksfjt1024/codex-cli-mcp-slim/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tksfjt1024/codex-cli-mcp-slim/releases/tag/v0.1.0
