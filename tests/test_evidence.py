import base64
import gzip
import hashlib
import json

import pytest

from p9search import evidence, retention, worker
from p9search.storage import read_json, sha256, write_json

from .test_retention import completed_job


def test_submission_transfers_exact_evidence_before_receipt_cleanup(tmp_path):
    job = completed_job(tmp_path, acknowledged=False)
    original = read_json(job / "result.json")

    class Client:
        def lease_body(self, **kw):
            return kw

        def request(self, action, body):
            report = body["result"]
            documents = evidence.unpack(report, report["evidence"])
            assert documents["launch.json"] == (job / "launch.json").read_text()
            assert (
                documents["data/s0001/archive-rows.json"]
                == (job / "data/s0001/archive-rows.json").read_text()
            )
            return retention.RECEIPT

        def clear_claim(self):
            pass

    worker.submit(Client(), job)
    retention.maintain(tmp_path)
    assert not job.exists()
    receipts = list((tmp_path / "receipts").glob("*.json.gz"))
    assert len(receipts) == 1
    receipt = json.loads(gzip.decompress(receipts[0].read_bytes()))
    assert receipt["result"] == original
    assert receipts[0].stat().st_size < 10000


def test_pack_is_deterministic_and_changed_or_missing_evidence_fails(tmp_path):
    job = completed_job(tmp_path)
    report = read_json(job / "result.json")
    first = evidence.pack(job, report)
    assert evidence.pack(job, report) == first
    docs = evidence.unpack(report, first)
    docs["launch.json"] = "{}"
    with pytest.raises(ValueError, match="checksum"):
        evidence.validate(report, docs)
    docs.pop("launch.json")
    with pytest.raises(ValueError, match="Incomplete"):
        evidence.validate(report, docs)


def test_compressed_evidence_expansion_is_bounded(tmp_path, monkeypatch):
    job = completed_job(tmp_path)
    report = read_json(job / "result.json")
    monkeypatch.setattr(evidence, "MAX_EXPANDED", 1000)
    raw = gzip.compress(b" " * 1000000, mtime=0)
    envelope = {
        "format": "json-gzip-v1",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": base64.b64encode(raw).decode(),
    }
    with pytest.raises(ValueError, match="Expanded"):
        evidence.unpack(report, envelope)


def test_acknowledged_evidence_mutation_never_prunes(tmp_path):
    job = completed_job(tmp_path)
    report = read_json(job / "result.json")
    write_json(
        job / "submission.json",
        {
            **retention.RECEIPT,
            "evidence_sha256": evidence.pack(job, report)["sha256"],
            "result_sha256": sha256(job / "result.json"),
        },
    )
    (job / "result.json").write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        retention.maintain(tmp_path)
    assert len(list(job.rglob("*.npy"))) == 3


def test_keep_evidence_retains_diagnostics_but_releases_pixels(tmp_path):
    job = completed_job(tmp_path)
    report = read_json(job / "result.json")
    write_json(
        job / "submission.json",
        {
            **retention.RECEIPT,
            "evidence_sha256": evidence.pack(job, report)["sha256"],
            "result_sha256": sha256(job / "result.json"),
        },
    )
    retention.maintain(tmp_path, keep_evidence=True)
    assert (job / "launch.json").exists()
    assert not list(job.rglob("*.npy"))
