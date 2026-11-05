import copy
import subprocess
import sys

import pytest

from p9search.review import bounded_json, outcomes_agree


def result():
    return {
        "task_id": "a",
        "source_sha256": "frozen",
        "launch": {"source_sha256": "frozen"},
        "state": "searched",
        "candidate_count": 1,
        "candidates": [{"parameters": [1.0, 2.0], "score": 10.0}],
        "injections": [{"assessed": True, "recovered": True}],
        "control_maximum": 5.0,
    }


def test_fabricated_flux_omissions_and_altered_inputs_cannot_match_replay():
    truth = result()
    assert outcomes_agree(copy.deepcopy(truth), truth)
    for key, value in [
        ("candidates", []),
        ("candidate_count", 0),
        ("control_maximum", 0.0),
        ("inputs", {"fabricated": True}),
        ("source_sha256", "changed"),
        ("injections", [{"assessed": True, "recovered": False}]),
    ]:
        changed = copy.deepcopy(truth)
        changed[key] = value
        assert not outcomes_agree(changed, truth)


@pytest.mark.parametrize(
    "raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', "[" * 25 + "0" + "]" * 25, " " * 2000001]
)
def test_untrusted_report_is_bounded(raw):
    with pytest.raises(ValueError):
        bounded_json(raw)


def test_runtime_guard_stops_a_stalled_local_search():
    source = "from p9search.limits import ResourceGuard; import time\nwith ResourceGuard(2**30, .1): time.sleep(10)"
    child = subprocess.run([sys.executable, "-c", source], capture_output=True, timeout=5)
    assert child.returncode == 75
    assert b"runtime limit" in child.stderr
