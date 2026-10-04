"""Tests for argv construction, including how per-call flags meet server-level ones.

The subprocess is never started here; the tests that go through the tool replace
`_run_codex` with a stub that records the argv it was handed.
"""

import pytest

from codex_cli_mcp_slim import server
from codex_cli_mcp_slim.server import CODEX_CMD, _build_argv


def _ok_run(**kwargs):
    stdout = (
        '{"type": "thread.started", "thread_id": "t"}\n'
        '{"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}}\n'
        '{"type": "turn.completed", "usage": {}}\n'
    )
    return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": "", "argv": kwargs["argv"]}


@pytest.fixture
def start_server_with(monkeypatch):
    """Set the server's own command line the way main() does, restored after the test."""
    monkeypatch.setattr(server, "SERVER_ARGS", server.SERVER_ARGS)
    monkeypatch.setattr(server, "_SERVER_SET", server._SERVER_SET)
    return server._set_server_args


def test_build_argv_minimal():
    argv = _build_argv(server_args=[])
    assert argv == [CODEX_CMD, "exec", "--json", "-"]


def test_build_argv_ends_with_json_and_stdin_prompt():
    # --json and `-` stay last so nothing in extra_args can capture `-` as its own value,
    # and so the prompt is read from stdin rather than taken from argv.
    argv = _build_argv(server_args=[], extra_args=["--output-schema", "/tmp/schema.json"])
    assert argv[-2:] == ["--json", "-"]
    assert argv[-4:-2] == ["--output-schema", "/tmp/schema.json"]


def test_build_argv_server_args_come_right_after_exec():
    server_args = ["-c", "model_reasoning_effort=high", "-C", "/srv/scratch"]
    argv = _build_argv(server_args=server_args)
    assert argv[:6] == [CODEX_CMD, "exec", *server_args]


def test_build_argv_per_call_config_follows_server_args_so_it_wins():
    # codex applies repeated -c flags in order, last one wins; a per-call override therefore
    # has to come after the server-level one.
    argv = _build_argv(
        server_args=["-c", "model_reasoning_effort=high"],
        config=["model_reasoning_effort=low"],
    )
    positions = [i for i, tok in enumerate(argv) if tok == "-c"]
    assert argv[positions[0] + 1] == "model_reasoning_effort=high"
    assert argv[positions[1] + 1] == "model_reasoning_effort=low"


def test_build_argv_resume_is_a_subcommand_before_per_call_flags():
    argv = _build_argv(server_args=["-c", "k=v"], thread_id="thread-1", model="m")
    assert argv[:6] == [CODEX_CMD, "exec", "-c", "k=v", "resume", "thread-1"]
    assert argv[6:8] == ["-m", "m"]


def test_build_argv_cd_maps_to_dash_c_upper():
    argv = _build_argv(server_args=[], cd="/repo")
    assert argv[argv.index("-C") + 1] == "/repo"


def test_build_argv_model_maps_to_dash_m():
    argv = _build_argv(server_args=[], model="gpt-example")
    assert argv[argv.index("-m") + 1] == "gpt-example"


def test_build_argv_config_is_repeated_not_comma_joined():
    argv = _build_argv(server_args=[], config=["a=1", "b=2", "c=3"])
    assert argv.count("-c") == 3
    for item in ("a=1", "b=2", "c=3"):
        assert argv[argv.index(item) - 1] == "-c"
    assert "a=1,b=2,c=3" not in argv


def test_build_argv_add_dir_is_repeated_not_comma_joined():
    argv = _build_argv(server_args=[], add_dir=["/a", "/b", "/c"])
    assert argv.count("--add-dir") == 3
    for d in ("/a", "/b", "/c"):
        assert argv[argv.index(d) - 1] == "--add-dir"
    assert "/a,/b,/c" not in argv


def test_build_argv_sandbox():
    argv = _build_argv(server_args=[], sandbox="read-only")
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_build_argv_profile():
    argv = _build_argv(server_args=[], profile="research")
    assert argv[argv.index("-p") + 1] == "research"


def test_build_argv_boolean_flags():
    argv = _build_argv(server_args=[], ephemeral=True, skip_git_repo_check=True)
    assert "--ephemeral" in argv
    assert "--skip-git-repo-check" in argv
    argv = _build_argv(server_args=[])
    assert "--ephemeral" not in argv
    assert "--skip-git-repo-check" not in argv


def test_build_argv_extra_args_appended_verbatim_before_json():
    argv = _build_argv(server_args=[], extra_args=["--ignore-rules", "--enable", "x"])
    assert argv[-5:-2] == ["--ignore-rules", "--enable", "x"]


def test_build_argv_all_typed_flags_combine():
    argv = _build_argv(
        server_args=[],
        cd="/repo",
        model="m",
        config=["k=v"],
        sandbox="workspace-write",
        add_dir=["/x"],
        profile="p",
        ephemeral=True,
        skip_git_repo_check=True,
        extra_args=["--custom"],
    )
    for token in (
        "-C",
        "/repo",
        "-m",
        "m",
        "-c",
        "k=v",
        "--sandbox",
        "workspace-write",
        "--add-dir",
        "/x",
        "-p",
        "p",
        "--ephemeral",
        "--skip-git-repo-check",
        "--custom",
    ):
        assert token in argv


# codex rejects a second -C, -m, -s, -p, --ephemeral or --skip-git-repo-check with "cannot
# be used multiple times" (exit 2), so a per-call parameter whose flag the server's own
# command line already sets is left out and the server-level value is used.


def _value_after(argv, *flags):
    return [argv[i + 1] for i, tok in enumerate(argv) if tok in flags]


def test_build_argv_drops_per_call_flags_the_server_already_sets():
    argv = _build_argv(
        server_args=["-C", "/srv/scratch", "--skip-git-repo-check"],
        cd="/other",
        skip_git_repo_check=True,
    )
    assert argv.count("--skip-git-repo-check") == 1
    assert _value_after(argv, "-C", "--cd") == ["/srv/scratch"]
    assert "/other" not in argv


@pytest.mark.parametrize(
    ("server_args", "param", "value"),
    [
        (["--cd", "/srv"], "cd", "/other"),
        (["--cd=/srv"], "cd", "/other"),
        (["-C/srv"], "cd", "/other"),
        (["-C=/srv"], "cd", "/other"),
        (["-m", "a"], "model", "b"),
        (["--model=a"], "model", "b"),
        (["-ma"], "model", "b"),
        (["-s", "read-only"], "sandbox", "workspace-write"),
        (["--sandbox=read-only"], "sandbox", "workspace-write"),
        (["-p", "a"], "profile", "b"),
        (["--profile", "a"], "profile", "b"),
        (["--ephemeral"], "ephemeral", True),
        (["--skip-git-repo-check"], "skip_git_repo_check", True),
    ],
)
def test_build_argv_recognises_every_spelling_of_a_server_level_flag(server_args, param, value):
    argv = _build_argv(server_args=server_args, **{param: value})
    assert argv == [CODEX_CMD, "exec", *server_args, "--json", "-"]


@pytest.mark.parametrize(
    "server_args",
    [
        ["-c", "-model=1"],  # the value of -c, not the model flag
        ["--", "-m"],  # past the end of the flags
    ],
)
def test_build_argv_reads_neither_a_value_nor_a_token_after_double_dash_as_a_flag(server_args):
    assert "model" not in server._params_set_by(server_args)
    argv = _build_argv(server_args=server_args, model="b")
    assert argv == [CODEX_CMD, "exec", *server_args, "-m", "b", "--json", "-"]


@pytest.mark.parametrize(
    ("server_args", "value", "written"),
    [
        (["-C", "/srv"], "/srv", "-C /srv"),
        (["-c", "k=v", "--cd", "/srv"], "/srv", "--cd /srv"),
        (["--cd=/srv"], "/srv", "--cd=/srv"),
        (["-C/srv"], "/srv", "-C/srv"),
        (["-C=/srv"], "/srv", "-C=/srv"),
        (["-C", "/my dir"], "/my dir", "-C '/my dir'"),
    ],
)
def test_params_set_by_keeps_the_server_level_value_and_its_spelling(server_args, value, written):
    assert server._params_set_by(server_args) == {"cd": (value, written)}


def test_params_set_by_reports_a_flag_without_a_value_as_true():
    assert server._params_set_by(["--ephemeral"]) == {"ephemeral": (True, "--ephemeral")}


def test_build_argv_keeps_per_call_flags_the_server_does_not_set():
    argv = _build_argv(
        server_args=["--skip-git-repo-check"],
        cd="/repo",
        model="m",
        sandbox="read-only",
        profile="p",
        ephemeral=True,
    )
    assert _value_after(argv, "-C") == ["/repo"]
    assert _value_after(argv, "-m") == ["m"]
    assert _value_after(argv, "--sandbox") == ["read-only"]
    assert _value_after(argv, "-p") == ["p"]
    assert "--ephemeral" in argv


def test_build_argv_server_level_dash_c_lower_is_not_mistaken_for_cd():
    argv = _build_argv(server_args=["-c", "model_reasoning_effort=high"], cd="/repo")
    assert _value_after(argv, "-C") == ["/repo"]


def test_build_argv_repeatable_flags_still_stack_on_server_level_ones():
    argv = _build_argv(
        server_args=["-c", "k=1", "--add-dir", "/a"],
        config=["k=2"],
        add_dir=["/b"],
    )
    assert _value_after(argv, "-c") == ["k=1", "k=2"]
    assert _value_after(argv, "--add-dir") == ["/a", "/b"]


def test_build_argv_reply_keeps_per_call_flags_after_resume():
    # After `resume` the per-call flags belong to the subcommand, which codex parses on its
    # own, so a repeat of a server-level flag is accepted there and nothing is dropped.
    argv = _build_argv(
        server_args=["--skip-git-repo-check", "-m", "a"],
        thread_id="t",
        skip_git_repo_check=True,
        model="b",
    )
    after_resume = argv[argv.index("resume") + 2 :]
    assert "--skip-git-repo-check" in after_resume
    assert _value_after(after_resume, "-m") == ["b"]


async def test_tool_reports_the_per_call_parameters_it_dropped(monkeypatch, start_server_with):
    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return _ok_run(**kwargs)

    start_server_with(["-C", "/srv", "--skip-git-repo-check"])
    monkeypatch.setattr(server, "_run_codex", fake_run)
    result = await server.codex(
        {"prompt": "p", "cd": "/other", "skip_git_repo_check": True, "model": "m"}
    )
    assert seen["argv"].count("--skip-git-repo-check") == 1
    assert _value_after(seen["argv"], "-C") == ["/srv"]
    assert _value_after(seen["argv"], "-m") == ["m"]
    text = result.content[0].text
    assert result.is_error is False
    assert text.startswith("answer\n\n[codex] thread_id=t")
    assert 'cd="/other"; this server runs with -C /srv' in text
    assert "skip_git_repo_check=true; this server runs with --skip-git-repo-check" in text
    assert "model=" not in text
    assert result.structured_content == {
        "threadId": "t",
        "content": "answer",
        "ignoredParameters": [
            {"name": "cd", "value": "/other", "serverValue": "/srv"},
            {"name": "skip_git_repo_check", "value": True, "serverValue": True},
        ],
    }


async def test_tool_reports_dropped_parameters_in_structured_content_without_a_thread(
    monkeypatch, start_server_with
):
    async def fake_run(**kwargs):
        return {"ok": False, "error": "boom", "argv": kwargs["argv"]}

    start_server_with(["--model=a"])
    monkeypatch.setattr(server, "_run_codex", fake_run)
    result = await server.codex({"prompt": "p", "model": "b"})
    assert result.is_error is True
    assert result.content[0].text.startswith("[ERROR] boom")
    assert 'model="b"; this server runs with --model=a' in result.content[0].text
    assert result.structured_content == {
        "ignoredParameters": [{"name": "model", "value": "b", "serverValue": "a"}]
    }


async def test_tool_adds_nothing_when_no_per_call_parameter_was_dropped(
    monkeypatch, start_server_with
):
    async def fake_run(**kwargs):
        return _ok_run(**kwargs)

    start_server_with(["--skip-git-repo-check"])
    monkeypatch.setattr(server, "_run_codex", fake_run)
    result = await server.codex({"prompt": "p", "cd": "/repo"})
    assert result.content[0].text == "answer\n\n[codex] thread_id=t status=completed"
    assert result.structured_content == {"threadId": "t", "content": "answer"}


async def test_tool_schema_names_the_parameters_the_server_already_sets(start_server_with):
    def props():
        return {tool.name: tool.input_schema["properties"] for tool in listed.tools}

    start_server_with(["--skip-git-repo-check", "-C", "/srv"])
    listed = await server.on_list_tools(None, None)
    assert (
        "already sets --skip-git-repo-check"
        in props()["codex"]["skip_git_repo_check"]["description"]
    )
    assert "already sets -C /srv," in props()["codex"]["cd"]["description"]
    assert "already" not in props()["codex"]["model"]["description"]

    start_server_with([])
    listed = await server.on_list_tools(None, None)
    assert "already" not in props()["codex"]["skip_git_repo_check"]["description"]
