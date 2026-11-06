import copy
import hashlib
import secrets
from types import SimpleNamespace

import pytest

from p9search import review
from p9search.provenance import execution_manifest, source_hash
from p9search.storage import canonical, read_json, sha256, write_json
from p9search.validation import check_report

from .test_tasks import specification


def example():
    spec = specification(
        injections=[
            {
                "parameters": [1.0, 1.0, 0.0, 0.0, 0.002],
                "flux": 10.0,
                "radial_velocity": 0.0,
                "label": "source",
            }
        ]
    )
    launch = execution_manifest()
    members = {"raw.npy", "error.npy", "archive-rows.json"}
    members.update(f"strips/{i:03d}-native.npy" for i in range(0, 140, 4))
    report = {
        "task_id": spec["task_id"],
        "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "protocol": spec["protocol"],
        "policy": spec["policy"],
        "source_sha256": source_hash(),
        "package_version": launch["package_version"],
        "launch": launch,
        "dependencies": {
            k: launch["dependencies"][k]
            for k in ("numpy", "scipy", "torch", "astropy", "tesscube", "lkprf")
        },
        "device": "cpu",
        "verified": False,
        "exclusion_credit": 0,
        "elapsed_seconds": 10.0,
        "state": "searched",
        "candidate_count": 0,
        "candidates": [],
        "quality": [{"sector": s, "masked_fraction": 0.0} for s in spec["sectors"]],
        "inputs": {
            f"data/s{s:04d}/download.json": {
                "native_cadences": 100,
                "binned_cadences": 10,
                "sha256": {name: "a" * 64 for name in members},
            }
            for s in spec["sectors"]
        },
        "artifacts": {name: "b" * 64 for name in ("launch.json", "task.json")},
        "restricted_motions": 100,
        "arm_maxima": {"physical": 3.0, "negative": 4.0, "reversed": 5.0},
        "control_maximum": 5.0,
        "injections": [
            {
                "label": "source",
                "flux": 10.0,
                "assessed": True,
                "recovered": True,
                "best_score": 6.0,
                "nearby_proposals": 1,
                "responses": [
                    {
                        "sector": s,
                        "has_source": True,
                        "assessed": True,
                        "gain": 1.0,
                        "template_rms_error": 0.0,
                        "relative_template_error": 0.0,
                    }
                    for s in spec["sectors"]
                ],
            }
        ],
    }
    return spec, report


def setup_review(tmp_path, mode):
    spec, report = example()
    key = secrets.token_bytes(32)
    local = tmp_path / "worker" / spec["task_id"]
    for name, value in (("task.json", spec), ("launch.json", report["launch"])):
        write_json(local / name, value)
        report["artifacts"][name] = sha256(local / name)
    for name, receipt in report["inputs"].items():
        p = local / name
        write_json(p.parent / "archive-rows.json", {})
        receipt["sha256"]["archive-rows.json"] = sha256(p.parent / "archive-rows.json")
        write_json(p, receipt)
    write_json(local / "result.json", report)
    raw = canonical(report)
    submission = {
        "task_id": spec["task_id"],
        "worker_id": "1" * 32,
        "submission_digest": hashlib.sha256(raw.encode()).hexdigest(),
        "result_json": raw,
    }
    plan = {
        "contract": "review-plan-v1",
        **{k: v for k, v in submission.items() if k != "result_json"},
        "source_sha256": report["source_sha256"],
        "spec_sha256": report["spec_sha256"],
        "mode": mode,
        "reason": mode,
        "audit_probability": 0.1,
    }
    write_json(tmp_path / "task.json", spec)
    write_json(tmp_path / "submission.json", submission)
    write_json(tmp_path / "plan.json", review.signed_receipt(plan, key))
    keypath = tmp_path / "key"
    keypath.write_text(key.hex())
    keypath.chmod(0o600)
    args = SimpleNamespace(
        task=tmp_path / "task.json",
        submission=tmp_path / "submission.json",
        key=keypath,
        plan=tmp_path / "plan.json",
        local_run=local,
        directory=tmp_path / "review",
        approved_source=[source_hash()],
    )
    return args, spec, report, key


@pytest.mark.parametrize("mode", ["trusted_local", "statistical"])
def test_light_review_does_not_download_or_replay(tmp_path, monkeypatch, mode):
    args, _, _, key = setup_review(tmp_path, mode)
    monkeypatch.setattr(review, "replay", lambda *a: pytest.fail("Unexpected replay"))
    review.review(args)
    r = review.verify_signed(read_json(args.directory / "receipt.json"), key)
    assert r["disposition"] == "accepted" and r["review_method"] == mode
    assert r["full_replay_verified"] is False and r["replay_seconds"] == 0
    assert "archive reacquired" not in r["checks"]


def test_local_report_requires_real_matching_local_artifacts(tmp_path, monkeypatch):
    args, _, _, _ = setup_review(tmp_path, "trusted_local")
    (args.local_run / "launch.json").write_text("{}")
    monkeypatch.setattr(review, "replay", lambda *a: pytest.fail("Unexpected replay"))
    with pytest.raises(ValueError, match="Local run differs"):
        review.review(args)
    assert not (args.directory / "receipt.json").exists()


def test_edited_plan_cannot_select_light_review(tmp_path):
    args, _, _, _ = setup_review(tmp_path, "full_replay")
    signed = read_json(args.plan)
    import base64

    signed["payload"] = base64.b64encode(
        base64.b64decode(signed["payload"]).replace(b"full_replay", b"statistical")
    ).decode()
    write_json(args.plan, signed)
    with pytest.raises(ValueError, match="signature"):
        review.review(args)


def test_assigned_audit_replays_and_records_it(tmp_path, monkeypatch):
    args, _, report, key = setup_review(tmp_path, "full_replay")
    calls = []
    monkeypatch.setattr(review, "replay", lambda *a: calls.append(True) or report)
    review.review(args)
    r = review.verify_signed(read_json(args.directory / "receipt.json"), key)
    assert calls == [True] and r["full_replay_verified"] is True


def test_inconsistent_report_escalates_to_replay_and_cannot_be_accepted(tmp_path, monkeypatch):
    args, _, report, key = setup_review(tmp_path, "statistical")
    submitted = read_json(args.submission)
    wrong = copy.deepcopy(report)
    wrong["control_maximum"] = 0.0
    submitted["result_json"] = canonical(wrong)
    submitted["submission_digest"] = hashlib.sha256(submitted["result_json"].encode()).hexdigest()
    write_json(args.submission, submitted)
    plan = review.verify_signed(read_json(args.plan), key)
    plan["submission_digest"] = submitted["submission_digest"]
    write_json(args.plan, review.signed_receipt(plan, key))
    calls = []
    monkeypatch.setattr(review, "replay", lambda *a: calls.append(True) or report)
    review.review(args)
    receipt = review.verify_signed(read_json(args.directory / "receipt.json"), key)
    assert calls == [True] and receipt["disposition"] == "disagrees"
    assert receipt["review_method"] == "full_replay" and receipt["consistency_failure"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(control_maximum=0.0),
        lambda r: r["injections"][0].update(recovered=False),
        lambda r: r["injections"][0]["responses"][0].update(gain=0.5),
        lambda r: r["injections"][0].update(assessed=False),
        lambda r: r.update(injections=[]),
        lambda r: r["inputs"].clear(),
        lambda r: r.update(candidate_count=1),
        lambda r: r.update(verified=True),
        lambda r: r["artifacts"].update({"../outside.json": "c" * 64}),
    ],
)
def test_inconsistent_reports_cannot_pass_cheap_checks(mutate):
    spec, report = example()
    check_report(spec, report, [source_hash()])
    mutate(report)
    with pytest.raises(ValueError):
        check_report(spec, report, [source_hash()])


def test_report_checks_do_not_claim_to_detect_consistent_fabrication():
    spec, report = example()
    fabricated = copy.deepcopy(report)
    fabricated["injections"][0]["best_score"] = 100.0
    check_report(spec, fabricated, [source_hash()])
    assert not review.outcomes_agree(fabricated, report)
