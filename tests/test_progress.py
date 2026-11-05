"""Tests for the progress notifications sent while codex runs.

An MCP client may abort a tool call that sends neither a response nor a progress
notification for too long (Claude Code: "sent no response or progress for 1800s"), and a
codex turn can outlast that. These tests run a fake codex binary through the tool and
record what reaches the request context's `session.report_progress`, or, through the SDK's
in-process client, what reaches the client's progress callback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp.client import Client
from mcp.types import CallToolRequestParams

from codex_cli_mcp_slim import server
from codex_cli_mcp_slim.server import _run_codex, on_call_tool

EVENTS = "".join(
    json.dumps(event) + "\n"
    for event in (
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )
)

CALLS = [
    ("codex", {"prompt": "p"}),
    ("codex-reply", {"thread_id": "thread-1", "prompt": "p"}),
]


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch):
    """Point CODEX_CMD at a script that reads the prompt, sleeps, then prints EVENTS."""

    def make(sleep: float) -> None:
        script = tmp_path / "codex"
        script.write_text(
            f"#!{sys.executable}\n"
            "import sys, time\n"
            "sys.stdin.read()\n"
            f"time.sleep({sleep})\n"
            f"sys.stdout.write({EVENTS!r})\n"
        )
        script.chmod(0o755)
        monkeypatch.setattr(server, "CODEX_CMD", str(script))

    return make


class Recorder:
    """Stands in for `ServerSession.report_progress`, recording each call."""

    def __init__(self, error: Exception | None = None, hang: bool = False) -> None:
        self.calls: list[tuple[float, float | None, str | None]] = []
        self.error = error
        self.hang = hang

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.calls.append((progress, total, message))
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


def _ctx(recorder: Recorder) -> SimpleNamespace:
    return SimpleNamespace(session=recorder)


@pytest.mark.parametrize(("name", "arguments"), CALLS)
async def test_progress_is_reported_every_interval_while_codex_runs(
    fake_codex, monkeypatch, name, arguments
) -> None:
    # codex finishes halfway between the second and third notification, so a second of
    # scheduling jitter either way leaves the count alone. The elapsed time in the message
    # moves with that jitter, so only its form is checked.
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 2)
    fake_codex(5)
    recorder = Recorder()

    result = await on_call_tool(
        _ctx(recorder), CallToolRequestParams(name=name, arguments=arguments)
    )

    assert result.is_error is False
    assert result.content[0].text.startswith("done\n\n[codex] thread_id=thread-1")
    assert [(progress, total) for progress, total, _ in recorder.calls] == [(1, None), (2, None)]
    assert all(re.fullmatch(r"codex running for \d+s", message) for _, _, message in recorder.calls)
    # Nothing is sent once the result is back.
    await asyncio.sleep(2.5)
    assert len(recorder.calls) == 2


async def test_progress_reaches_the_client_that_asked_for_it(fake_codex, monkeypatch) -> None:
    # Through the SDK's own request context rather than a stand-in: the notifications reach
    # the callback of the call that passed one, and a call without a callback gets none.
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.25)
    fake_codex(1)
    received: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        received.append((progress, total, message))

    async with Client(server.server) as client:
        result = await client.call_tool("codex", {"prompt": "p"}, progress_callback=on_progress)
        assert result.is_error is False
        assert received, "no progress reached the callback"
        assert [progress for progress, _, _ in received] == list(range(1, len(received) + 1))
        assert all(total is None for _, total, _ in received)
        assert all(re.fullmatch(r"codex running for \d+s", message) for _, _, message in received)

        received.clear()
        result = await client.call_tool("codex", {"prompt": "p"})
        assert result.is_error is False
        assert received == []


async def test_no_progress_when_codex_finishes_within_the_interval(fake_codex) -> None:
    fake_codex(0)
    recorder = Recorder()

    result = await on_call_tool(
        _ctx(recorder), CallToolRequestParams(name="codex", arguments={"prompt": "p"})
    )

    assert result.is_error is False
    assert recorder.calls == []


async def test_a_call_without_a_request_context_still_runs(fake_codex, monkeypatch) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.1)
    fake_codex(0.5)

    result = await on_call_tool(
        None, CallToolRequestParams(name="codex", arguments={"prompt": "p"})
    )

    assert result.is_error is False
    assert result.content[0].text.startswith("done\n\n[codex] thread_id=thread-1")


async def test_a_failing_progress_notification_leaves_the_result_alone(
    fake_codex, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.1)
    fake_codex(0.5)
    recorder = Recorder(error=RuntimeError("report_progress raised"))

    with caplog.at_level(logging.WARNING, logger="codex_cli_mcp_slim"):
        result = await on_call_tool(
            _ctx(recorder), CallToolRequestParams(name="codex", arguments={"prompt": "p"})
        )

    assert result.is_error is False
    assert result.content[0].text.startswith("done\n\n[codex] thread_id=thread-1")
    # A failed notification does not stop the next one.
    assert len(recorder.calls) >= 2
    assert "progress notification failed: RuntimeError: report_progress raised" in caplog.text


async def test_a_hanging_progress_notification_does_not_hold_the_result(
    fake_codex, monkeypatch
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.1)
    fake_codex(0.3)
    recorder = Recorder(hang=True)

    before = asyncio.all_tasks()
    result = await asyncio.wait_for(
        on_call_tool(
            _ctx(recorder), CallToolRequestParams(name="codex", arguments={"prompt": "p"})
        ),
        timeout=5,
    )

    assert result.is_error is False
    assert len(recorder.calls) == 1
    leaked = {t for t in asyncio.all_tasks() - before if not t.done()}
    assert not leaked, f"the stuck notification was left running: {leaked}"


async def test_timeout_still_reports_and_stops_the_progress_task(monkeypatch) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.2)
    recorder = Recorder()
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]

    before = asyncio.all_tasks()
    result = await _run_codex(
        prompt="p", argv=argv, timeout=1, report_progress=recorder.report_progress
    )

    assert not result["ok"]
    assert result["error"] == "timeout after 1s"
    assert recorder.calls
    leaked = {t for t in asyncio.all_tasks() - before if not t.done()}
    assert not leaked, f"tasks left running after the timeout: {leaked}"


async def test_cancellation_kills_codex_and_stops_the_progress_task(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.1)
    recorder = Recorder()
    pid_file = tmp_path / "proc.pid"
    argv = [
        sys.executable,
        "-c",
        'import os, time; open(os.environ["PID_FILE"], "w").write(str(os.getpid())); '
        "time.sleep(30)",
    ]

    before = asyncio.all_tasks()
    task = asyncio.ensure_future(
        _run_codex(
            prompt="p",
            argv=argv,
            timeout=20,
            env_overrides={"PID_FILE": str(pid_file)},
            report_progress=recorder.report_progress,
        )
    )
    for _ in range(50):
        if pid_file.exists() and recorder.calls:
            break
        await asyncio.sleep(0.1)
    assert pid_file.exists(), "process never started"
    assert recorder.calls, "no progress before the cancellation"
    proc_pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leaked = {t for t in asyncio.all_tasks() - before - {task} if not t.done()}
    assert not leaked, f"tasks left running after the cancellation: {leaked}"
    for _ in range(30):
        try:
            os.kill(proc_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc_pid, 9)
        raise AssertionError(f"process {proc_pid} still alive after caller cancellation")


async def test_cancellation_through_an_anyio_scope_still_cleans_up_communicate(
    tmp_path: Path, monkeypatch
) -> None:
    # The SDK cancels a call through an anyio cancel scope, which cancels the task again at
    # every await. The cleanup in _run_codex's `finally` awaits too, so a second
    # cancellation must not cut it short: here a descendant that left codex's process group
    # keeps the pipes open, and a communicate() task nobody cancelled would outlive the call
    # for as long as that descendant runs.
    monkeypatch.setattr(server, "PROGRESS_INTERVAL", 0.1)
    recorder = Recorder()
    pid_file = tmp_path / "escaped.pid"
    escaped_code = (
        'import os, time; open(os.environ["PID_FILE"], "w").write(str(os.getpid())); time.sleep(30)'
    )
    leader_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {escaped_code!r}], start_new_session=True); "
        "time.sleep(30)"
    )
    argv = [sys.executable, "-c", leader_code]

    before = asyncio.all_tasks()
    try:
        with anyio.CancelScope() as scope:
            asyncio.get_running_loop().call_later(0.5, scope.cancel)
            await _run_codex(
                prompt="p",
                argv=argv,
                timeout=20,
                env_overrides={"PID_FILE": str(pid_file)},
                report_progress=recorder.report_progress,
            )
        assert scope.cancelled_caught, "the cancellation did not reach _run_codex"
        leaked = {t for t in asyncio.all_tasks() - before if not t.done()}
        assert not leaked, f"tasks left running after the cancellation: {leaked}"
    finally:
        for _ in range(20):
            if pid_file.exists():
                break
            await asyncio.sleep(0.1)
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 9)
