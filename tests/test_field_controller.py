import fcntl
from unittest.mock import Mock

import pytest

from tools.field_controller import distributed_cycle, standalone_cycle, tick


def site(ready=0, submitted=0, accepted=0, hold=False):
    adapter = Mock()
    adapter.counts.return_value = dict(pending=ready, submitted=submitted)
    adapter.publication_state.return_value = accepted, hold
    adapter.choose.return_value = None
    adapter.expand.return_value = dict(mode="route_added")
    return adapter


def test_empty_queue_recovers_and_generates_despite_worker_error():
    adapter = site(submitted=8, accepted=8)
    adapter.recover_worker.side_effect = OSError("worker offline")
    assert standalone_cycle(adapter)["mode"] == "route_added"
    adapter.record_worker_error.assert_called_once_with("worker offline")


def test_fixed_supplier_prepares_without_local_worker():
    adapter = site()
    adapter.choose.return_value = ("route", "field")
    result = distributed_cycle(adapter)
    assert result is adapter.prepare.return_value
    adapter.prepare.assert_called_once_with("route", "field")
    adapter.recover_worker.assert_not_called()


@pytest.mark.parametrize(
    "ready,submitted,accepted,hold,mode",
    [
        (100, 0, 0, False, "buffered"),
        (3, 3, 0, False, "buffered"),
        (3, 0, 3, False, "waiting_for_publication"),
        (0, 0, 0, True, "waiting_for_publication"),
        (2, 3, 3, False, "route_added"),
    ],
)
def test_admission(ready, submitted, accepted, hold, mode):
    assert distributed_cycle(site(ready, submitted, accepted, hold))["mode"] == mode


def test_busy_producer_leaves_status_alone(tmp_path):
    path = tmp_path / "lock"
    cycle, report = Mock(), Mock()
    with path.open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        assert tick(cycle, path, report) == "producer_busy"
    cycle.assert_not_called()
    report.assert_not_called()
    cycle.side_effect = BlockingIOError("catalog busy")
    assert tick(cycle, path, report) == "retry"
    assert "catalog busy" in report.call_args.args[0]["error"]
