"""Tests for tool dispatch, hand-rolled argument validation, event parsing and result formatting.

The validation tests carry weight: the low-level Server advertises the schema and never
applies it, so the checks in server.py are the only thing standing between a client's
argument and codex's argv.
"""

import json

from mcp.types import CallToolRequestParams

from codex_cli_mcp_slim.server import (
    _codex_trailer,
    _format_result,
    _parse_events,
    _run_failed,
    codex,
    codex_reply,
    on_call_tool,
    on_list_tools,
)


def _text(result):
    return result.content[0].text


def _events(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _ok_run(stdout, stderr=""):
    return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": stderr, "argv": ["codex"]}


COMPLETED = _events(
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "first"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "final"}},
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7},
    },
)

FAILED = _events(
    {"type": "thread.started", "thread_id": "thread-2"},
    {"type": "turn.started"},
    {"type": "error", "message": "Unsupported value: 'none'"},
    {"type": "turn.failed", "error": {"message": "Unsupported value: 'none'"}},
)


async def test_list_tools_exposes_the_two_mcp_server_tool_names():
    result = await on_list_tools(None, None)
    assert [tool.name for tool in result.tools] == ["codex", "codex-reply"]


async def test_unknown_tool_name_is_reported():
    result = await on_call_tool(None, CallToolRequestParams(name="does_not_exist", arguments={}))
    assert "unknown tool" in _text(result)
    assert result.is_error is True


async def test_missing_arguments_key_does_not_raise():
    result = await on_call_tool(None, CallToolRequestParams(name="codex"))
    assert result.is_error is True
    assert "`prompt` is required" in _text(result)


async def test_null_prompt_is_rejected_as_a_missing_key():
    result = await codex({"prompt": None})
    assert result.is_error is True
    assert "`prompt` is required" in _text(result)


async def test_empty_prompt_is_rejected():
    result = await codex({"prompt": ""})
    assert result.is_error is True
    assert "must not be empty" in _text(result)


async def test_reply_requires_thread_id():
    result = await codex_reply({"prompt": "again"})
    assert result.is_error is True
    assert "`thread_id` is required" in _text(result)


async def test_reply_rejects_flags_resume_does_not_take():
    # codex's own error would name the flag; naming the parameter is what the client can act on.
    result = await codex_reply({"thread_id": "t", "prompt": "p", "cd": "/repo"})
    assert result.is_error is True
    assert "unknown parameter `cd`" in _text(result)


async def test_string_true_does_not_enable_ephemeral():
    # bool("false") is True; a client typo must not silently flip a flag.
    result = await codex({"prompt": "p", "ephemeral": "true"})
    assert result.is_error is True
    assert "must be a boolean" in _text(result)


async def test_string_add_dir_is_rejected():
    # A bare string iterates character by character: "docs" would become four --add-dir flags.
    result = await codex({"prompt": "p", "add_dir": "docs"})
    assert result.is_error is True
    assert "`add_dir` must be an array of strings" in _text(result)


async def test_config_entries_must_be_key_value():
    result = await codex({"prompt": "p", "config": ["model_reasoning_effort"]})
    assert result.is_error is True
    assert "key=value" in _text(result)


async def test_sandbox_must_be_a_known_mode():
    result = await codex({"prompt": "p", "sandbox": "yolo"})
    assert result.is_error is True
    assert "`sandbox` must be one of" in _text(result)


async def test_env_must_map_strings_to_strings():
    result = await codex({"prompt": "p", "env": {"A": 1}})
    assert result.is_error is True
    assert "`env` must be an object" in _text(result)


async def test_timeout_seconds_bool_is_rejected():
    result = await codex({"prompt": "p", "timeout_seconds": True})
    assert result.is_error is True
    assert "`timeout_seconds` must be an integer" in _text(result)


async def test_timeout_seconds_out_of_range_is_rejected():
    result = await codex({"prompt": "p", "timeout_seconds": 5})
    assert result.is_error is True
    assert "between 30 and 3600" in _text(result)


def test_parse_events_takes_thread_id_messages_usage_and_status():
    parsed = _parse_events(COMPLETED)
    assert parsed["thread_id"] == "thread-1"
    assert parsed["messages"] == ["first", "final"]
    assert parsed["usage"]["output_tokens"] == 7
    assert parsed["turn_status"] == "completed"
    assert parsed["errors"] == []


def test_parse_events_dedupes_the_error_codex_reports_twice():
    parsed = _parse_events(FAILED)
    assert parsed["turn_status"] == "failed"
    assert parsed["errors"] == ["Unsupported value: 'none'"]


def test_parse_events_skips_lines_that_are_not_json_objects():
    parsed = _parse_events("not json\n[1, 2]\n" + COMPLETED)
    assert parsed["thread_id"] == "thread-1"
    assert parsed["messages"][-1] == "final"


def test_parse_events_reports_whether_anything_looked_like_an_event():
    assert _parse_events("plain text output\n")["saw_events"] is False
    assert _parse_events(COMPLETED)["saw_events"] is True


def test_trailer_carries_thread_id_status_and_usage():
    trailer = _codex_trailer(_parse_events(COMPLETED))
    assert trailer.startswith("[codex] thread_id=thread-1 status=completed")
    assert "input_tokens=100" in trailer
    assert "cached_input_tokens=40" in trailer
    assert "output_tokens=7" in trailer


def test_trailer_marks_missing_fields_as_unknown():
    assert _codex_trailer(_parse_events("")) == "[codex] thread_id=UNKNOWN status=UNKNOWN"


def test_format_result_returns_last_message_then_trailer():
    text = _format_result(_ok_run(COMPLETED))
    assert text.startswith("final\n\n[codex] thread_id=thread-1")
    assert "first" not in text


def test_run_failed_on_nonzero_exit():
    assert _run_failed({"ok": False, "stdout": COMPLETED}) is True


def test_run_failed_on_failed_turn_even_with_exit_zero():
    # Observed: an unsupported reasoning effort fails the turn inside the model API while
    # the process still shuts down with exit code 0.
    assert _run_failed(_ok_run(FAILED)) is True


def test_failed_turn_text_names_the_error_and_the_thread():
    text = _format_result(_ok_run(FAILED))
    assert text.startswith("[ERROR]")
    assert "Unsupported value: 'none'" in text
    assert "thread_id=thread-2 status=failed" in text


def test_run_failed_when_only_errors_and_no_message():
    stdout = _events({"type": "error", "message": "boom"})
    assert _run_failed(_ok_run(stdout)) is True


def test_recovered_error_is_not_a_failure_but_stays_visible():
    stdout = _events(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "error", "message": "transient"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {}},
    )
    assert _run_failed(_ok_run(stdout)) is False
    text = _format_result(_ok_run(stdout))
    assert text.startswith("answer\n\n[codex]")
    assert "transient" in text


def test_exit_zero_without_agent_message_is_a_warning_not_a_failure():
    stdout = _events({"type": "thread.started", "thread_id": "t"}, {"type": "turn.completed"})
    assert _run_failed(_ok_run(stdout)) is False
    text = _format_result(_ok_run(stdout))
    assert text.startswith("[WARNING] codex exited successfully but produced no agent message.")
    assert "thread_id=t" in text


def test_non_event_stdout_is_shown_verbatim_when_there_is_no_message():
    text = _format_result(_ok_run("plain text output\n"))
    assert "stdout:\nplain text output" in text


def test_nonzero_exit_text_includes_stderr_and_argv():
    text = _format_result(
        {
            "ok": False,
            "error": "codex failed",
            "returncode": 2,
            "stdout": "",
            "stderr": "error: the argument '--model <MODEL>' cannot be used multiple times",
            "argv": ["codex", "exec"],
        }
    )
    assert text.startswith("[ERROR] codex failed")
    assert "returncode=2" in text
    assert "cannot be used multiple times" in text
    assert "argv: ['codex', 'exec']" in text


async def test_invoke_returns_structured_content_like_mcp_server_did(monkeypatch):
    async def fake_run(**kwargs):
        return _ok_run(COMPLETED)

    monkeypatch.setattr("codex_cli_mcp_slim.server._run_codex", fake_run)
    result = await codex({"prompt": "p"})
    assert result.is_error is False
    assert result.structured_content == {"threadId": "thread-1", "content": "final"}


async def test_invoke_passes_prompt_over_stdin_not_argv(monkeypatch):
    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return _ok_run(COMPLETED)

    monkeypatch.setattr("codex_cli_mcp_slim.server._run_codex", fake_run)
    await codex({"prompt": "the prompt text"})
    assert seen["prompt"] == "the prompt text"
    assert "the prompt text" not in seen["argv"]
    assert seen["argv"][-1] == "-"


def test_main_help_and_version_do_not_start_the_server(monkeypatch, capsys):
    # A person typing the command by hand gets text back instead of a stdio server that
    # waits on the terminal; anything else on the command line is passed through to codex.
    from codex_cli_mcp_slim import server

    monkeypatch.setattr(server.sys, "argv", ["codex-cli-mcp-slim", "--help"])
    server.main()
    assert capsys.readouterr().out.startswith("usage: codex-cli-mcp-slim")
    monkeypatch.setattr(server.sys, "argv", ["codex-cli-mcp-slim", "--version"])
    server.main()
    assert capsys.readouterr().out.startswith("codex-cli-mcp-slim ")


async def test_unknown_parameter_is_rejected_not_ignored():
    # Silently dropping a key would run the call with a different configuration than the
    # client asked for; a working directory is the case that matters most.
    result = await codex({"prompt": "p", "working_dir": "/repo"})
    assert result.is_error is True
    assert "unknown parameter `working_dir`" in _text(result)


async def test_old_mcp_server_parameter_names_are_named_in_the_error():
    result = await codex({"prompt": "p", "cwd": "/repo"})
    assert result.is_error is True
    assert "unknown parameter `cwd`; this server calls it `cd`" in _text(result)
    result = await codex_reply({"threadId": "t", "prompt": "p"})
    assert result.is_error is True
    assert "unknown parameter `threadId`; this server calls it `thread_id`" in _text(result)


def test_whitespace_only_message_with_an_error_is_a_failure():
    # _format_result treats a blank message as "no agent message"; is_error has to agree,
    # or a text opening with [ERROR] would come back as a successful call.
    stdout = _events(
        {"type": "error", "message": "boom"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "  \n"}},
    )
    assert _run_failed(_ok_run(stdout)) is True
    assert _format_result(_ok_run(stdout)).startswith("[ERROR]")
