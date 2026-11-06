"""Cheap consistency checks; these do not certify an unobserved search."""

import hashlib
import math
import re
from pathlib import Path

from .storage import canonical, load_bounded, sha256


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def check_report(spec, report, approved_sources):
    """Bind a report to trusted inputs and check its reported arithmetic."""
    require(report.get("task_id") == spec["task_id"], "Task identity differs")
    require(
        report.get("spec_sha256") == hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "Task specification differs",
    )
    require(
        report.get("protocol") == spec["protocol"] and report.get("policy") == spec["policy"],
        "Scientific policy differs",
    )
    source = report.get("source_sha256")
    require(source in approved_sources and digest(source), "Unapproved scientific release")
    launch = report.get("launch", {})
    require(
        launch.get("source_sha256") == source
        and launch.get("package_version") == report.get("package_version"),
        "Launch identity differs",
    )
    dependencies = report.get("dependencies", {})
    require(
        set(dependencies) == {"numpy", "scipy", "torch", "astropy", "tesscube", "lkprf"}
        and all(isinstance(v, str) and v for v in dependencies.values()),
        "Missing dependency versions",
    )
    require(
        all(launch.get("dependencies", {}).get(k) == v for k, v in dependencies.items()),
        "Launch dependencies differ",
    )
    require(report.get("device") in ("cpu", "mps", "cuda", "cuda:0"), "Unsupported device")
    require(
        report.get("verified") is False and report.get("exclusion_credit") == 0,
        "Worker cannot grant verification or exclusion credit",
    )
    require(
        finite(report.get("elapsed_seconds")) and report["elapsed_seconds"] > 0,
        "Invalid elapsed time",
    )
    artifacts = report.get("artifacts", {})
    require(
        isinstance(artifacts, dict)
        and {"task.json", "launch.json"} <= artifacts.keys()
        and all(
            Path(k).name == k and k.endswith(".json") and digest(v) for k, v in artifacts.items()
        ),
        "Invalid artifact manifest",
    )
    quality = report.get("quality")
    require(
        isinstance(quality, list) and 0 < len(quality) <= len(spec["sectors"]),
        "Missing sector quality checks",
    )
    require(
        [r.get("sector") for r in quality] == spec["sectors"][: len(quality)]
        and all(
            finite(r.get("masked_fraction")) and 0 <= r["masked_fraction"] <= 1 for r in quality
        ),
        "Invalid sector quality checks",
    )
    expected_inputs = {f"data/s{r['sector']:04d}/download.json" for r in quality}
    inputs = report.get("inputs", {})
    require(isinstance(inputs, dict) and set(inputs) == expected_inputs, "Input sectors differ")
    members = {"raw.npy", "error.npy", "archive-rows.json"}
    members.update(f"strips/{i:03d}-native.npy" for i in range(0, 140, 4))
    for receipt in inputs.values():
        require(
            type(receipt.get("native_cadences")) is int
            and 0 < receipt["native_cadences"] <= 25000
            and type(receipt.get("binned_cadences")) is int
            and 0 < receipt["binned_cadences"] <= 2000,
            "Invalid input dimensions",
        )
        hashes = receipt.get("sha256", {})
        require(
            set(hashes) == members and all(digest(v) for v in hashes.values()),
            "Invalid input checksums",
        )
    state = report.get("state")
    count, candidates = report.get("candidate_count", 0), report.get("candidates", [])
    require(
        type(count) is int and isinstance(candidates, list) and count == len(candidates),
        "Candidate count differs",
    )
    if state == "quality_rejected":
        require(
            not count
            and report.get("injections") == []
            and quality[-1]["masked_fraction"] > 0.02
            and all(r["masked_fraction"] <= 0.02 for r in quality[:-1]),
            "Quality rejection does not match the stop condition",
        )
        return
    require(
        state in ("searched", "needs_followup")
        and len(quality) == len(spec["sectors"])
        and all(r["masked_fraction"] <= 0.02 for r in quality),
        "Invalid search state",
    )
    maxima = report.get("arm_maxima", {})
    require(
        set(maxima) == {"physical", "negative", "reversed"}
        and all(finite(v) for v in maxima.values()),
        "Missing control arms",
    )
    threshold = report.get("control_maximum")
    require(
        finite(threshold) and threshold == max(maxima["negative"], maxima["reversed"]),
        "Control threshold differs",
    )
    require(
        type(report.get("restricted_motions")) is int and report["restricted_motions"] > 0,
        "Missing motion search",
    )
    if state == "needs_followup":
        require(count > 0 and report.get("injections") == [], "Invalid candidate state")
        return
    require(count == 0, "Candidate requires follow-up")
    measured = report.get("injections")
    require(
        isinstance(measured, list) and len(measured) == len(spec["injections"]),
        "Injection count differs",
    )
    for case, row in zip(spec["injections"], measured, strict=True):
        require(
            row.get("label") == case["label"] and row.get("flux") == case["flux"],
            "Injection identity differs",
        )
        require(type(row.get("assessed")) is bool, "Missing injection assessment")
        responses = row.get("responses")
        require(isinstance(responses, list), "Missing native response checks")
        sectors = [r.get("sector") for r in responses]
        require(
            len(sectors) == len(set(sectors)) and set(sectors) <= set(spec["sectors"]),
            "Invalid response sectors",
        )
        for response in responses:
            require(
                type(response.get("has_source")) is bool and type(response.get("assessed")) is bool,
                "Invalid source response",
            )
            if not response["assessed"]:
                continue
            if response["has_source"]:
                require(
                    finite(response.get("gain"))
                    and 0.9 <= response["gain"] <= 1.1
                    and finite(response.get("template_rms_error"))
                    and 0 <= response["template_rms_error"] <= 0.002
                    and finite(response.get("relative_template_error"))
                    and 0 <= response["relative_template_error"] <= 0.1,
                    "Native response outside admission limits",
                )
            else:
                require(
                    response.get("native_pixels_unchanged") is True,
                    "Empty source changed native pixels",
                )
        if not row["assessed"]:
            require(
                row.get("recovered") is None and bool(row.get("rejection")),
                "Unassessed injection cannot be a recovery or miss",
            )
            continue
        require(
            sum(r["has_source"] for r in responses) >= 3 and all(r["assessed"] for r in responses),
            "Injection response not admitted",
        )
        score = row.get("best_score")
        require(score is None or finite(score), "Invalid recovery score")
        require(
            type(row.get("nearby_proposals")) is int
            and row["nearby_proposals"] >= 0
            and (score is None or row["nearby_proposals"] > 0),
            "Invalid nearby proposals",
        )
        require(
            type(row.get("recovered")) is bool
            and row["recovered"] == (score is not None and score > threshold),
            "Recovery does not exceed the controls",
        )


def check_local_run(directory, spec, report):
    """Check an operator-selected run; the path is never supplied by a volunteer."""
    directory = Path(directory).absolute()
    require(
        directory.resolve() == directory and not directory.is_symlink(),
        "Local result directory is redirected",
    )
    require(
        directory.name == spec["task_id"]
        and load_bounded(directory / "task.json") == spec
        and load_bounded(directory / "result.json") == report
        and load_bounded(directory / "launch.json") == report["launch"],
        "Local run differs from submission",
    )
    for name, expected in report["artifacts"].items():
        path = directory / name
        require(not path.is_symlink() and sha256(path) == expected, "Local artifact differs")
    for name, receipt in report["inputs"].items():
        path = directory / name
        require(
            path.resolve() == path and load_bounded(path) == receipt, "Local input manifest differs"
        )
        provenance = path.parent / "archive-rows.json"
        require(
            provenance.resolve() == provenance
            and sha256(provenance) == receipt["sha256"]["archive-rows.json"],
            "Archive provenance differs",
        )
