"""Pure-function tests for argv construction. Subprocess execution is not tested here."""

from codex_cli_mcp_slim.server import CODEX_CMD, _build_argv


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
