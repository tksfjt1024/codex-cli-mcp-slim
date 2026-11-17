"""The opt-in `readOnlyHint` declaration on the listed tools.

Claude Code runs adjacent MCP tool calls concurrently only when each tool declares
`readOnlyHint: true`. The declaration is stronger than what `codex` does, so the server
makes it only when `$CODEX_CLI_MCP_SLIM_READ_ONLY_HINT` asks for it, and otherwise lists
exactly the tools it always has.
"""

import json
import os
import subprocess
import sys
import threading

import pytest

from codex_cli_mcp_slim import server

ENV = "CODEX_CLI_MCP_SLIM_READ_ONLY_HINT"


def _wire(tool):
    return tool.model_dump(by_alias=True, exclude_none=True)


async def test_unset_lists_the_tools_without_annotations(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    listed = await server.on_list_tools(None, None)
    assert [_wire(tool) for tool in listed.tools] == [
        _wire(server._CODEX_TOOL),
        _wire(server._REPLY_TOOL),
    ]
    assert all("annotations" not in _wire(tool) for tool in listed.tools)


@pytest.mark.parametrize("value", ["1", "true", "TRUE"])
async def test_enabled_declares_read_only_on_codex_and_codex_reply(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    listed = await server.on_list_tools(None, None)
    assert [tool.name for tool in listed.tools] == ["codex", "codex-reply"]
    # Only readOnlyHint is declared: destructiveHint and idempotentHint mean nothing next to
    # it, and openWorldHint is about something else.
    assert [_wire(tool)["annotations"] for tool in listed.tools] == [{"readOnlyHint": True}] * 2


@pytest.mark.parametrize("value", ["", "0", "false"])
async def test_disabled_values_list_the_tools_without_annotations(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    listed = await server.on_list_tools(None, None)
    assert all(tool.annotations is None for tool in listed.tools)


async def test_enabled_changes_nothing_but_the_annotation(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    listed = await server.on_list_tools(None, None)
    for tool, original in zip(listed.tools, [server._CODEX_TOOL, server._REPLY_TOOL], strict=True):
        assert {k: v for k, v in _wire(tool).items() if k != "annotations"} == _wire(original)


async def test_enabled_also_covers_the_schema_rewritten_for_server_level_flags(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    # Restored after the test, as test_basic's start_server_with does.
    monkeypatch.setattr(server, "SERVER_ARGS", server.SERVER_ARGS)
    monkeypatch.setattr(server, "_SERVER_SET", server._SERVER_SET)
    server._set_server_args(["-C", "/srv"])
    listed = await server.on_list_tools(None, None)
    assert (
        "already sets -C /srv," in listed.tools[0].input_schema["properties"]["cd"]["description"]
    )
    assert [tool.annotations.read_only_hint for tool in listed.tools] == [True, True]


@pytest.mark.parametrize("value", ["yes", "2", "on", "tru"])
def test_an_unrecognised_value_is_an_error_not_off(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(ValueError, match=ENV):
        server._read_only_hint_enabled()


def _list_tools_over_stdio(env_value):
    """Start the server as a client would and return the `tools` of a tools/list reply."""
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if env_value is not None:
        env[ENV] = env_value
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    # Read the reply before closing stdin: the server stops serving at end of input, and a
    # client that has sent everything and hung up can lose the last reply.
    proc = subprocess.Popen(
        [sys.executable, "-m", "codex_cli_mcp_slim.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    # A server that never answers must fail the test, not hang the run.
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        for line in proc.stdout:
            reply = json.loads(line)
            if reply.get("id") == 2:
                return reply["result"]["tools"]
        raise AssertionError("the server closed stdout without answering tools/list")
    finally:
        watchdog.cancel()
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_over_stdio_the_hint_reaches_the_client_only_when_asked_for():
    asked = _list_tools_over_stdio("1")
    assert {t["name"]: t["annotations"] for t in asked} == {
        "codex": {"readOnlyHint": True},
        "codex-reply": {"readOnlyHint": True},
    }
    default = _list_tools_over_stdio(None)
    assert [t["name"] for t in default] == ["codex", "codex-reply"]
    assert all("annotations" not in t for t in default)


def test_over_stdio_an_unrecognised_value_stops_the_server_at_startup():
    # The documented failure is that the process exits, not that tools/list fails later.
    env = {k: v for k, v in os.environ.items() if k != ENV}
    env[ENV] = "yes"
    done = subprocess.run(
        [sys.executable, "-m", "codex_cli_mcp_slim.server"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert done.returncode != 0
    assert ENV in done.stderr
