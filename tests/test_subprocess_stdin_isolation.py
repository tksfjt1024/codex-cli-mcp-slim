"""Regression tests for how the prompt reaches codex and for subprocess stdin isolation.

The MCP server runs over the stdio transport, where the parent's stdin is the
JSON-RPC channel from the MCP client. The codex child must never inherit that
stdin (stdin=None): a read or ioctl on the shared file description can corrupt
the channel and silently kill the server. _run_codex always opens a pipe of its
own, writes the prompt down it and closes it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

from codex_cli_mcp_slim.server import _run_codex


def _make_proc_mock(*, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


async def test_stdin_is_a_pipe_never_inherited() -> None:
    with patch(
        "codex_cli_mcp_slim.server.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = _make_proc_mock()
        await _run_codex(argv=["echo"], prompt="hi", timeout=5)
    kwargs = mock_create.call_args.kwargs
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdin"] is not None


async def test_prompt_is_written_to_the_pipe() -> None:
    with patch(
        "codex_cli_mcp_slim.server.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock_create:
        proc = _make_proc_mock()
        mock_create.return_value = proc
        await _run_codex(argv=["cat"], prompt="the prompt", timeout=5)
    proc.communicate.assert_awaited_once_with(b"the prompt")


async def test_missing_binary_is_reported_with_argv() -> None:
    result = await _run_codex(argv=["/nonexistent/codex", "exec"], prompt="p", timeout=5)
    assert result["ok"] is False
    assert "failed to launch codex binary" in result["error"]
    assert result["argv"] == ["/nonexistent/codex", "exec"]


async def test_parallel_invocations_smoke() -> None:
    # codex is not assumed to be installed in the test env; the current python stands in
    # and echoes what it read from stdin as a codex-shaped event stream.
    fake = (
        "import sys, json; p = sys.stdin.read(); "
        "print(json.dumps({'type': 'item.completed', "
        "'item': {'type': 'agent_message', 'text': p}}))"
    )
    argv = [sys.executable, "-c", fake]
    results = await asyncio.gather(
        *[_run_codex(argv=argv, prompt=f"prompt-{i}", timeout=15) for i in range(3)]
    )
    for i, r in enumerate(results):
        assert r["ok"], r
        event = json.loads(r["stdout"])
        assert event["item"]["text"] == f"prompt-{i}"
