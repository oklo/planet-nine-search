"""One predeclared field: quality screening, blind arms, then native injections."""

import gc
import hashlib
import importlib.metadata
import time
from pathlib import Path

import numpy as np

from . import __version__, runtime
from .archive import acquire
from .calibration import has_source, response_admitted, response_check, unchanged_pixels
from .foreground import QualityRejected, fit
from .injections import inject
from .policy import check_prior_geometry, screen_rows
from .provenance import execution_manifest, source_hash
from .science.audited_hough_selector import AuditedHoughSearch
from .science.geometry import SectorGeometry, SkyFrame, grids
from .science.hough_maps import prepare_foreground_covariance_maps
from .science.matching import trajectory_distances
from .science.motion_bank import restrict_specs, sector_sums_full_padding
from .science.photometry import score_native_proposal
from .science.point_response import tess_pixel_model
from .science.sector_photometry import coverage
from .science.templates import bin_template
from .storage import canonical, read_json, sha256, write_json
from .tasks import PROTOCOL, fetch_bundle, validate_task


def control_threshold(arms):
    if set(arms) != {"physical", "negative", "reversed"}:
        raise ValueError("Real, negative-flux, and reversed-motion arms required")
    maxima = {}
    for arm, rows in arms.items():
        scores = [r["native"]["coherent_score"] for r in rows if r["native"] is not None]
        if not scores or not np.isfinite(scores).all():
            raise ValueError("Every arm must contain finite native candidates")
        maxima[arm] = float(max(scores))
    threshold = max(maxima["negative"], maxima["reversed"])
    flags = [
        r
        for r in arms["physical"]
        if r["native"] is not None
        and r["native"]["coherent_score"] > threshold
        and r.get("prior", {}).get("eligible_for_p9_followup", True)
    ]
    return threshold, flags, maxima


def prepare(sector, model):
    psf = tess_pixel_model(np.array([69.0]), np.array([69.0]), sector.meta, radius=6)[0]
    maps, coarse, groups = prepare_foreground_covariance_maps(
        model["scene"],
        model["temporal"],
        dict(sector.geom, curvature=sector.curvature),
        psf,
        device=runtime.device(),
    )
    return {
        "projector": model["projector"],
        "maps": {
            "numerator": maps,
            "scene": model["scene"],
            "temporal": model["temporal"],
            "groups": groups,
        },
        "coarse": coarse,
        "psf": psf,
        "weights": model["weights"],
    }


def score(proposals, sectors, prepared, progress):
    records = []
    for index, proposal in enumerate(proposals):
        if index % 16 == 0:
            progress("native photometry", completed=index, total=len(proposals))
        result, details = score_native_proposal(proposal, sectors, prepared)
        records.append(
            {
                **proposal,
                "parameters": proposal["parameters"].tolist(),
                "native": result,
                "sectors": details,
            }
        )
    return records


def run(spec, directory, budget, *, origins=("https://oklo.org",)):
    validate_task(spec)
    directory = Path(directory)
    start = time.monotonic()
    launch = execution_manifest()
    write_json(directory / "launch.json", launch)
    policy = spec["policy"]

    def progress(stage, **details):
        budget.check()
        record = {"stage": stage, **details}
        write_json(directory / "progress.json", record)
        print(canonical(record), flush=True)

    data = fetch_bundle(spec, directory, budget, origins)
    frame = SkyFrame(data / f"s{spec['reference_sector']:04d}")
    sectors = [SectorGeometry(data / f"s{sid:04d}", frame) for sid in spec["sectors"]]
    report = {
        "task_id": spec["task_id"],
        "spec_sha256": hashlib.sha256(canonical(spec).encode()).hexdigest(),
        "protocol": PROTOCOL,
        "package_version": __version__,
        "device": str(runtime.device()),
        "policy": policy,
        "launch": launch,
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ["numpy", "scipy", "torch", "astropy", "tesscube", "lkprf"]
        },
        "quality": [],
        "injections": [],
        "verified": False,
        "exclusion_credit": 0,
    }

    def finish(state):
        report["inputs"] = {
            str(p.relative_to(directory)): read_json(p) for p in data.glob("s*/download.json")
        }
        report["source_sha256"] = source_hash()
        if report["source_sha256"] != launch["source_sha256"]:
            raise RuntimeError("Scientific source changed during execution")
        report["state"] = state
        report["elapsed_seconds"] = time.monotonic() - start
        report["artifacts"] = {
            p.name: sha256(p)
            for p in directory.glob("*.json")
            if p.name not in {"result.json", "progress.json"}
        }
        write_json(directory / "result.json", report)
        return report

    prepared = []
    for sector in sectors:
        acquire(sector.directory, budget, progress)
        progress("fit foreground", sector=int(sector.meta["sector"]))
        try:
            model = fit(
                sector,
                empirical_stars=int(sector.meta["sector"]) in policy["empirical_star_sectors"],
            )
        except QualityRejected as error:
            report["quality"].append(
                {"sector": int(sector.meta["sector"]), "masked_fraction": error.fraction}
            )
            return finish("quality_rejected")
        report["quality"].append(
            {"sector": int(sector.meta["sector"]), "masked_fraction": model["masked_fraction"]}
        )
        prepared.append(prepare(sector, model))
        del model
        gc.collect()
        runtime.release()
    full, grid = grids(sectors)
    check_prior_geometry(policy["prior"], frame, grid)
    local = {s["inverse_distance"]: s["local_angular"] for s in full}
    with np.load(data / "motion_masks.npz") as masks:
        specs = restrict_specs(full, [masks[f"slice_{i:02d}"] for i in range(len(full))])
    report["grid"] = grid
    report["restricted_motions"] = sum(len(s["angular"]) for s in specs)

    def sums(bundle, coarse, sector, angular, inverse, psf, orientation):
        return sector_sums_full_padding(
            bundle, coarse, sector, angular, inverse, psf, local[inverse], orientation
        )

    searcher = AuditedHoughSearch(grid, lambda name, value: write_json(directory / name, value))

    def proposals(models, label, orientation=1.0, signs=(1.0,)):
        progress("Hough search", arm=label)
        items, details = searcher(
            sectors,
            models,
            specs,
            orientation=orientation,
            signs=signs,
            maximum_locations=24,
            hypotheses=8,
            sector_sums=sums,
            progress=lambda i, n, m: progress(
                "Hough search", arm=label, slice=i, slices=n, motions=m
            ),
        )
        write_json(directory / f"{label}-search.json", details)
        # Freeze all blind proposals before consulting injection coordinates.
        write_json(
            directory / f"{label}-proposals.json",
            {
                str(k): [{**p, "parameters": p["parameters"].tolist()} for p in v]
                for k, v in items.items()
            },
        )
        return items

    physical = proposals(prepared, "physical", signs=(1.0, -1.0))
    arms = {
        "physical": score(physical[1.0], sectors, prepared, progress),
        "negative": score(physical[-1.0], sectors, prepared, progress),
        "reversed": score(
            proposals(prepared, "reversed", orientation=-1.0)[1.0], sectors, prepared, progress
        ),
    }
    screen_rows(arms["physical"], policy["prior"])
    for arm, rows in arms.items():
        write_json(directory / f"{arm}-candidates.json", rows)
    threshold, flags, maxima = control_threshold(arms)
    report.update(
        control_maximum=threshold, arm_maxima=maxima, candidates=flags, candidate_count=len(flags)
    )
    write_json(
        directory / "baseline.json", {"threshold": threshold, "maxima": maxima, "flags": flags}
    )
    if flags:
        return finish("needs_followup")
    forced = {}
    for index, case in enumerate(spec["injections"]):
        for si, (sector, model) in enumerate(zip(sectors, prepared, strict=True)):
            path = sector.exact_positions(
                case["parameters"],
                native=True,
                radial_velocity=case["radial_velocity"],
                kepler=True,
            )
            if coverage(path, sector.cadences["native_keep"])["stamp_overlap_fraction"] > 0:
                template, region = bin_template(path, sector.meta, sector.cadences)
                forced[index, si] = (
                    model["projector"].score(template, region) if has_source(template) else None
                )
                del template
    weights = [p["weights"] for p in prepared]
    prepared.clear()
    gc.collect()
    runtime.release()
    for index, case in enumerate(spec["injections"]):
        paths = [
            sector.exact_positions(
                case["parameters"],
                native=True,
                radial_velocity=case["radial_velocity"],
                kepler=True,
            )
            for sector in sectors
        ]
        injected = []
        response = {
            "label": case["label"],
            "flux": case["flux"],
            "recovered": None,
            "assessed": False,
            "responses": [],
        }
        try:
            for si, (sector, path, weight) in enumerate(zip(sectors, paths, weights, strict=True)):
                progress(
                    "native injection refit", injection=index, sector=int(sector.meta["sector"])
                )
                raw = inject(sector, path, case["flux"])
                model = fit(
                    sector,
                    raw_override=raw,
                    fixed_weights=weight,
                    empirical_stars=int(sector.meta["sector"]) in policy["empirical_star_sectors"],
                )
                if (index, si) in forced:
                    template, region = bin_template(path, sector.meta, sector.cadences)
                    baseline = np.load(sector.directory / "raw.npy", mmap_mode="r")
                    present = has_source(template)
                    if not present:
                        unchanged_pixels(baseline, raw)
                        check = {"assessed": True, "native_pixels_unchanged": True}
                    else:
                        after = model["projector"].score(template, region)
                        check = response_check(
                            baseline,
                            raw,
                            template,
                            region,
                            sector.geom["keep"],
                            case["flux"],
                            forced[index, si],
                            after,
                        )
                    response["responses"].append(
                        {"sector": int(sector.meta["sector"]), "has_source": present, **check}
                    )
                    del template, baseline
                injected.append(prepare(sector, model))
                del raw, model
            response["assessed"] = response_admitted(response["responses"])
            if not response["assessed"]:
                response["rejection"] = "Native source response or rounding fidelity failed"
                report["injections"].append(response)
                continue
            items = proposals(injected, f"injection-{index}")[1.0]
            distances, context = trajectory_distances(
                [p["parameters"] for p in items],
                case["parameters"],
                case["radial_velocity"],
                sectors,
            )
            nearby = [
                p for p, distance in zip(items, distances[:, 0], strict=True) if distance < 1.5
            ]
            write_json(
                directory / f"injection-{index}-matching.json",
                {"distances": distances[:, 0].tolist(), "context": context},
            )
            scored = score(nearby, sectors, injected, progress)
            screen_rows(scored, policy["prior"])
            write_json(directory / f"injection-{index}-candidates.json", scored)
            scores = [
                r["native"]["coherent_score"]
                for r in scored
                if r["native"] is not None
                and r.get("prior", {}).get("eligible_for_p9_followup", True)
            ]
            response.update(
                nearby_proposals=len(nearby),
                best_score=max(scores) if scores else None,
                recovered=bool(scores and max(scores) > threshold),
            )
        except QualityRejected as error:
            response["rejection"] = str(error)
        finally:
            injected.clear()
            gc.collect()
            runtime.release()
        report["injections"].append(response)
    return finish("searched")
