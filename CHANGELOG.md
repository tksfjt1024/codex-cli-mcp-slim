# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/tksfjt1024/codex-cli-mcp-slim/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tksfjt1024/codex-cli-mcp-slim/releases/tag/v0.1.0
