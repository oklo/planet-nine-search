import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

import p9search
from p9search.storage import DirectoryLock, load_bounded, write_json

SCRIPT = Path(__file__).resolve().parents[1] / "skills/planet-nine-worker/scripts/session.py"
spec = importlib.util.spec_from_file_location("worker_session", SCRIPT)
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


def eventually(check):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail("Process did not reach the expected state")


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """Exercise detached processes without registering or acquiring any field."""
    package = tmp_path / "fake/p9search"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        Path(p9search.__file__).read_text()
        + f"\n__path__.append({str(Path(p9search.__file__).parent)!r})\n"
    )
    (package / "__main__.py").write_text("""
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from p9search.storage import DirectoryLock, write_json

if sys.argv[1] == "run":
    task = Path(sys.argv[2])
    with DirectoryLock(task.parent.parent, "search.lock"):
        write_json(task.with_name("progress.json"), {"stage": "scoring"})
        while True:
            time.sleep(0.05)

root = Path(sys.argv[sys.argv.index("--directory") + 1])
if (root / "fail").exists():
    print("p9: deliberate test failure", flush=True)
    sys.exit(1)
with DirectoryLock(root, "worker.lock"):
    task = root / ("a" * 64) / "task.json"
    write_json(task, {})
    child = subprocess.Popen([sys.executable, "-m", "p9search", "run", str(task)],
                             start_new_session=True)
    write_json(root / "child.json", {"pid": child.pid})
    print(f"Claimed test: {task.parent}", flush=True)
    try:
        while True:
            if (root / "submit").exists() and child.poll() is None:
                child.terminate()
                child.wait()
                print("Submitted for independent review.", flush=True)
                print("awaiting review", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        if child.poll() is None:
            child.terminate()
        child.wait()
        (root / "released").touch()
""")
    main = package / "__main__.py"
    body = main.read_text()
    main.write_text(
        "import sys\nfrom p9search.diagnostics import capture_output\n"
        "from contextlib import nullcontext\n"
        "log = sys.argv[sys.argv.index('--log-file') + 1] if '--log-file' in sys.argv else None\n"
        "with capture_output(log) if log else nullcontext():\n" + "    exec(" + repr(body) + ")\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    private = tmp_path / "private"
    private.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    yield private, work, ["--directory", str(work)]
    session.stop(private)


def test_detached_start_duplicate_status_and_clean_stop(worker):
    private, work, arguments = worker
    # Invoke a separate controller that exits: the worker must outlive it.
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            [os.environ["PYTHONPATH"], str(Path(p9search.__file__).parent.parent)]
        ),
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--session", str(private), "start", *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    first = json.loads(result.stdout)
    assert first["state"] == "running"
    assert session.start(private, arguments)["pid"] == first["pid"]
    report = eventually(
        lambda: (r if r.get("progress") else None) if (r := session.status(private)) else None
    )
    assert report["progress"]["stage"] == "scoring"
    child = psutil.Process(load_bounded(work / "child.json")["pid"])
    assert session.stop(private)["state"] == "exited"
    assert (work / "released").exists()
    assert not child.is_running()
    assert (private / "session.json").stat().st_mode & 0o777 == 0o600


def test_submitted_task_is_not_reported_as_active(worker):
    private, work, arguments = worker
    session.start(private, arguments)
    eventually(lambda: session.status(private).get("progress"))
    (work / "submit").touch()
    eventually(lambda: "awaiting review" in session.status(private, details=True)["last_output"])
    assert "task_id" not in session.status(private)
    assert (work / ("a" * 64) / "progress.json").exists()


@pytest.mark.parametrize("name", ["worker.lock", "search.lock"])
def test_existing_worker_or_search_prevents_start(worker, name):
    private, work, arguments = worker
    with DirectoryLock(work, name), pytest.raises(RuntimeError, match="Another process"):
        session.start(private, arguments)
    assert not (private / "session.json").exists()


def test_crash_is_visible_and_not_automatically_restarted(worker):
    private, work, arguments = worker
    (work / "fail").touch()
    report = session.start(private, arguments)
    assert report["state"] == "exited"
    assert "p9: deliberate test failure" in report["last_output"]
    pid = report["pid"]
    assert session.status(private)["pid"] == pid
    (work / "fail").unlink()
    restarted = session.start(private, [])
    assert restarted["pid"] != pid
    assert restarted["directory"] == str(work.resolve())
    assert "deliberate test failure" in (private / "worker.previous.log").read_text()


def test_stale_identity_cannot_signal_a_different_process(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = {
            "pid": process.pid,
            "created": psutil.Process(process.pid).create_time() - 1,
            "command": process.args,
            "directory": str(tmp_path),
            "log": str(tmp_path / "worker.log"),
        }
        write_json(tmp_path / "session.json", record)
        assert session.stop(tmp_path)["state"] == "exited"
        assert process.poll() is None
        record["created"] += 1
        record["command"] = ["wrong-command"]
        write_json(tmp_path / "session.json", record)
        assert session.stop(tmp_path)["state"] == "exited"
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait()


def test_restarts_preserve_absolute_settings_without_growing_command(worker):
    private, work, arguments = worker
    session.start(private, arguments)
    first = load_bounded(private / "session.json")["command"]
    for _ in range(3):
        session.stop(private)
        session.start(private, [])
        assert load_bounded(private / "session.json")["command"] == first
    assert "last_output" not in session.status(private)
