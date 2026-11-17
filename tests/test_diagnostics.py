import os
import subprocess
import sys

import pytest

from p9search.diagnostics import LOG_BYTES, BoundedOutput, State
from p9search.storage import read_json


def test_native_child_flood_is_drained_and_tail_stays_bounded(tmp_path):
    path = tmp_path / "worker.log"
    path.write_bytes(b"old" * LOG_BYTES)
    with BoundedOutput(path) as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os; [os.write(1, b'x'*65536) for _ in range(256)]; os.write(2, b'last diagnostic')",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        while process.poll() is None:
            assert path.stat().st_size <= LOG_BYTES
            output.check()
            process.wait(timeout=10)
    assert path.stat().st_size <= LOG_BYTES
    assert path.read_bytes().endswith(b"last diagnostic")


def test_capture_bounds_python_and_native_session_output(tmp_path):
    log = tmp_path / "session.log"
    code = """
import os, sys
from p9search.diagnostics import capture_output
with capture_output(sys.argv[1]):
    print('python message')
    for _ in range(64):
        os.write(2, b'native'*65536)
    print('final status', flush=True)
print('restored output', flush=True)
"""
    child = subprocess.run([sys.executable, "-c", code, str(log)], capture_output=True, timeout=15)
    assert child.returncode == 0, child.stderr
    assert child.stdout == b"restored output\n" and not child.stderr
    assert log.stat().st_size <= LOG_BYTES
    assert log.read_bytes().endswith(b"final status\n")


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_logs_cannot_truncate_another_file(tmp_path, kind):
    original = tmp_path / "original"
    original.write_text("keep")
    target = tmp_path / "log"
    if kind == "symlink":
        target.symlink_to(original)
    else:
        os.link(original, target)
    with pytest.raises((OSError, ValueError)), BoundedOutput(target):
        pass
    assert original.read_text() == "keep"


def test_repeated_waits_replace_state_without_output_or_rewrites(tmp_path, capsys):
    state = State(tmp_path)
    state("queue_wait", "awaiting review")
    path = tmp_path / "worker-state.json"
    stamp = path.stat().st_mtime_ns
    for _ in range(10000):
        state("queue_wait", "awaiting review")
    assert path.stat().st_mtime_ns == stamp
    assert len(path.read_bytes()) < 256
    assert capsys.readouterr().out == ""
    state("searching", "next field", task_id="a" * 64)
    assert read_json(path)["state"] == "searching"


def test_log_write_failure_does_not_block_child_exit(tmp_path, monkeypatch):
    def fail(chunk):
        raise OSError("log storage unavailable")

    output = BoundedOutput(tmp_path / "worker.log")
    monkeypatch.setattr(output, "_append", fail)
    with pytest.raises(OSError, match="log storage unavailable"), output:
        process = subprocess.Popen(
            [sys.executable, "-c", "import os; [os.write(1, b'x'*65536) for _ in range(64)]"],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        assert process.wait(timeout=10) == 0
