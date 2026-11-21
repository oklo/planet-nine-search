"""Reject ambiguous dependency metadata before leasing scientific work."""

from types import SimpleNamespace

import pytest

from p9search import provenance, worker
from p9search.cli import parser
from p9search.storage import DiskBudget, read_json


def distributions(monkeypatch, entries):
    monkeypatch.setattr(
        provenance.importlib.metadata,
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": name}, version=version) for name, version in entries
        ],
    )


def test_manifest_rejects_conflicting_versions_independent_of_name_spelling(monkeypatch):
    distributions(monkeypatch, [("Astropy", "7.2.3"), ("astropy", "7.2.4")])
    with pytest.raises(ValueError, match="Conflicting installed versions"):
        provenance.execution_manifest()


def test_repeated_identical_distribution_does_not_change_manifest(monkeypatch):
    distributions(monkeypatch, [("astropy", "7.2.3"), ("astropy", "7.2.3")])
    assert provenance.execution_manifest()["dependencies"] == {"astropy": "7.2.3"}


def test_conflicting_environment_stops_before_registration_or_claim(tmp_path, monkeypatch):
    distributions(monkeypatch, [("lkprf", "1.1.0"), ("lkprf", "1.1.1")])

    def no_client(*args):
        pytest.fail("An ambiguous environment must not contact the coordinator")

    monkeypatch.setattr(worker, "Client", no_client)
    args = parser().parse_args(["worker", "--continuous", "--directory", str(tmp_path)])
    with pytest.raises(ValueError, match="Conflicting installed versions"):
        worker.work(args, DiskBudget(tmp_path, 2**30, 0))
    assert read_json(tmp_path / "worker-state.json")["state"] == "held"
    assert not (tmp_path / "credentials.json").exists()
