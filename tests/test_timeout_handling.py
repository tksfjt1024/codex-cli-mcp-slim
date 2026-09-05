"""Regression tests for timeout handling: partial-output preservation and process-group cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock

from codex_cli_mcp_slim.server import _kill_process_group, _run_codex


async def test_narrow_race_reports_success_when_process_finishes_at_timeout_edge(
    tmp_path: Path, monkeypatch
) -> None:
    # exercises the communicate_task.done() branch when it fires right at the timeout edge
    argv = [sys.executable, "-c", "import sys; sys.stdout.write('done'); sys.exit(0)"]
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(fut, timeout):
        if timeout == 3:
            await real_wait_for(fut, timeout=10)
            raise asyncio.TimeoutError
        return await real_wait_for(fut, timeout=timeout)

    monkeypatch.setattr("codex_cli_mcp_slim.server.asyncio.wait_for", fake_wait_for)
    result = await _run_codex(prompt="p", argv=argv, timeout=3)
    assert result["ok"]
    assert result["returncode"] == 0
    assert "done" in result["stdout"]


async def test_timeout_preserves_partial_stdout(tmp_path: Path) -> None:
    argv = [
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.write('partial-output\\n'); "
        "sys.stdout.flush(); time.sleep(60)",
    ]
    result = await _run_codex(prompt="p", argv=argv, timeout=1)
    assert not result["ok"]
    assert "timeout after 1s" in result["error"]
    assert "partial-output" in result["stdout"]


async def test_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    grandchild_code = (
        'import os, time; open(os.environ["PID_FILE"], "w").write(str(os.getpid())); time.sleep(60)'
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        "time.sleep(60)"
    )
    argv = [sys.executable, "-c", parent_code]
    result = await _run_codex(
        prompt="p",
        argv=argv,
        timeout=2,
        env_overrides={"PID_FILE": str(pid_file)},
    )
    assert not result["ok"]

    for _ in range(20):
        if pid_file.exists():
            break
        await asyncio.sleep(0.1)
    assert pid_file.exists(), "grandchild never started"
    grandchild_pid = int(pid_file.read_text())

    for _ in range(20):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"grandchild {grandchild_pid} still alive after process-group kill")


async def test_timeout_kills_descendants_after_leader_exits_early(tmp_path: Path) -> None:
    # leader exits before the timeout fires; killpg must not depend on the leader still being alive
    pid_file = tmp_path / "descendant.pid"
    descendant_code = (
        'import os, time; open(os.environ["PID_FILE"], "w").write(str(os.getpid())); time.sleep(30)'
    )
    leader_code = (
        "import subprocess, sys; "
        "sys.stdout.write('leader-output\\n'); sys.stdout.flush(); "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}])"
    )
    argv = [sys.executable, "-c", leader_code]

    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await _run_codex(
        prompt="p",
        argv=argv,
        timeout=1,
        env_overrides={"PID_FILE": str(pid_file)},
    )
    elapsed = loop.time() - start

    assert not result["ok"]
    assert "leader-output" in result["stdout"]
    assert elapsed < 15, f"_run_codex took {elapsed:.1f}s; leader's early exit should not block"

    assert pid_file.exists(), "descendant never started"
    descendant_pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"descendant {descendant_pid} still alive after leader exited early")


async def test_caller_cancellation_kills_process(tmp_path: Path) -> None:
    # cancelling _run_codex itself must still kill the subprocess, not just unwind the coroutine
    pid_file = tmp_path / "proc.pid"
    argv = [
        sys.executable,
        "-c",
        'import os, time; open(os.environ["PID_FILE"], "w").write(str(os.getpid())); '
        "time.sleep(30)",
    ]
    task = asyncio.ensure_future(
        _run_codex(prompt="p", argv=argv, timeout=20, env_overrides={"PID_FILE": str(pid_file)})
    )
    for _ in range(50):
        if pid_file.exists():
            break
        await asyncio.sleep(0.1)
    assert pid_file.exists(), "process never started"
    proc_pid = int(pid_file.read_text())

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    for _ in range(30):
        try:
            os.kill(proc_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"process {proc_pid} still alive after caller cancellation")


async def test_timeout_does_not_leak_communicate_task(tmp_path: Path) -> None:
    # second wait_for(timeout=10) can also time out; communicate_task must be cancelled, not leaked
    pid_file = tmp_path / "descendant.pid"
    descendant_code = (
        "import os, time; "
        "os.setsid(); "
        'open(os.environ["PID_FILE"], "w").write(str(os.getpid())); '
        "time.sleep(30)"
    )
    leader_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        "time.sleep(30)"
    )
    argv = [sys.executable, "-c", leader_code]

    before = asyncio.all_tasks()
    result = await _run_codex(
        prompt="p",
        argv=argv,
        timeout=1,
        env_overrides={"PID_FILE": str(pid_file)},
    )
    assert not result["ok"]

    leaked = {t for t in asyncio.all_tasks() - before if not t.done()}
    assert not leaked, f"leaked tasks after double timeout: {leaked}"

    for _ in range(20):
        if pid_file.exists():
            break
        await asyncio.sleep(0.1)
    if pid_file.exists():
        descendant_pid = int(pid_file.read_text())
        with contextlib.suppress(ProcessLookupError):
            os.kill(descendant_pid, signal.SIGKILL)


def test_kill_process_group_sends_sigkill_to_own_pid(monkeypatch) -> None:
    # start_new_session=True guarantees pgid==pid, so killpg(proc.pid) works after leader exits
    calls = []
    monkeypatch.setattr(
        "codex_cli_mcp_slim.server.os.killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
    )
    proc = MagicMock()
    proc.pid = 999
    _kill_process_group(proc)
    assert calls == [(999, signal.SIGKILL)]
    proc.kill.assert_called_once()


def test_kill_process_group_suppresses_process_lookup_error(monkeypatch) -> None:
    def raise_lookup(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr("codex_cli_mcp_slim.server.os.killpg", raise_lookup)
    proc = MagicMock()
    proc.pid = 999
    proc.kill.side_effect = ProcessLookupError
    _kill_process_group(proc)  # must not raise


def test_kill_process_group_suppresses_permission_error_from_killpg(monkeypatch) -> None:
    def raise_permission(pgid, sig):
        raise PermissionError

    monkeypatch.setattr("codex_cli_mcp_slim.server.os.killpg", raise_permission)
    proc = MagicMock()
    proc.pid = 999
    _kill_process_group(proc)  # must not raise
    proc.kill.assert_called_once()


def test_kill_process_group_suppresses_permission_error_from_proc_kill(monkeypatch) -> None:
    # proc.kill() fallback must suppress PermissionError symmetrically with the killpg suppress list
    monkeypatch.setattr("codex_cli_mcp_slim.server.os.killpg", lambda pgid, sig: None)
    proc = MagicMock()
    proc.pid = 999
    proc.kill.side_effect = PermissionError
    _kill_process_group(proc)  # must not raise


def test_kill_process_group_falls_back_to_proc_kill_on_other_oserror(monkeypatch) -> None:
    # killpg raising an OSError subclass other than the two we anticipated must not skip proc.kill()
    def raise_other_oserror(pgid, sig):
        raise OSError(5, "I/O error")

    monkeypatch.setattr("codex_cli_mcp_slim.server.os.killpg", raise_other_oserror)
    proc = MagicMock()
    proc.pid = 999
    _kill_process_group(proc)  # must not raise, and must still fall back to proc.kill()
    proc.kill.assert_called_once()
