"""Small full-orchestration regression for N66's empty fourth-sector response."""

from types import SimpleNamespace

import numpy as np
import pytest

from p9search import search
from p9search.storage import DiskBudget

from .test_tasks import specification


@pytest.mark.parametrize(
    "empty_fourth,alter_empty,expected",
    [(True, False, True), (False, False, False), (True, True, None)],
)
def test_informative_sectors_alone_can_qualify_only_for_unchanged_empty_injection(
    tmp_path, monkeypatch, empty_fourth, alter_empty, expected
):
    cases = [
        {
            "parameters": [1.0, 1.0, 0.0, 0.0, 0.002],
            "flux": 10.0,
            "radial_velocity": 0.0,
            "label": "source",
        }
    ]
    spec = specification(sectors=[1, 28, 68, 95], injections=cases)
    data = tmp_path / "data"
    data.mkdir()
    np.savez(data / "motion_masks.npz", slice_00=np.ones(1, bool))
    for sid in spec["sectors"]:
        folder = data / f"s{sid:04d}"
        folder.mkdir()
        np.save(folder / "raw.npy", np.zeros((4, 2, 2)))

    def sector(directory, frame):
        return SimpleNamespace(
            directory=directory,
            meta={"sector": int(directory.name[1:])},
            cadences={"native_keep": np.ones(4, bool)},
            geom={"keep": np.ones(4, bool)},
            exact_positions=lambda *a, **k: np.ones((4, 2)) * 30,
        )

    def template(path, meta, cadences):
        value = 0.0 if meta["sector"] == 95 and empty_fourth else 1.0
        return np.full((4, 2, 2), value), (slice(None), slice(None), slice(None))

    def inject(sec, path, flux):
        value = template(path, sec.meta, sec.cadences)[0] * flux
        if sec.meta["sector"] == 95:
            if not empty_fourth:
                value *= 0.5  # A present but failed transfer cannot be exempted.
            elif alter_empty:
                value[0, 0, 0] = 1.0
        return value

    def fit(sec, raw_override=None, **kw):
        amplitude = float(raw_override.mean()) if raw_override is not None else 0.0
        return {
            "projector": SimpleNamespace(
                score=lambda *a: {"identifiable": True, "flux": amplitude}
            ),
            "masked_fraction": 0.0,
            "weights": None,
        }

    class Hough:
        def __init__(self, *a):
            pass

        def __call__(self, sectors, models, specs, **kw):
            return {
                sign: [
                    {
                        "parameters": np.array(cases[0]["parameters"]),
                        "sign": sign,
                        "orientation": kw["orientation"],
                    }
                ]
                for sign in kw["signs"]
            }, {}

    def native(proposal, sectors, models):
        injected = models[0]["projector"].score(None)["flux"] > 0
        value = (
            10.0
            if injected
            else (1.0 if proposal["sign"] == 1 and proposal["orientation"] == 1 else 3.0)
        )
        return {"coherent_score": value, "flux": value, "sigma": 1.0}, []

    monkeypatch.setattr(search, "fetch_bundle", lambda *a: data)
    monkeypatch.setattr(search, "acquire", lambda *a: None)
    monkeypatch.setattr(search, "SkyFrame", lambda *a: None)
    monkeypatch.setattr(search, "SectorGeometry", sector)
    monkeypatch.setattr(search, "fit", fit)
    monkeypatch.setattr(search, "prepare", lambda s, m: m)
    monkeypatch.setattr(search, "bin_template", template)
    monkeypatch.setattr(search, "inject", inject)
    monkeypatch.setattr(
        search,
        "grids",
        lambda s: (
            [
                {
                    "inverse_distance": 0.002,
                    "local_angular": np.zeros((1, 2)),
                    "angular": np.zeros((1, 2)),
                }
            ],
            {},
        ),
    )
    monkeypatch.setattr(search, "restrict_specs", lambda s, m: s)
    monkeypatch.setattr(search, "AuditedHoughSearch", Hough)
    monkeypatch.setattr(search, "score_native_proposal", native)
    monkeypatch.setattr(search, "trajectory_distances", lambda *a: (np.zeros((1, 1)), {}))
    budget = DiskBudget(tmp_path, 2**30, 0)
    if expected is None:
        with pytest.raises(ValueError, match="changed native"):
            search.run(spec, tmp_path, budget)
    else:
        result = search.run(spec, tmp_path, budget)
        assert result["state"] == "searched"
        response = result["injections"][0]
        assert response["assessed"] == expected
        assert response["recovered"] is (True if expected else None)
        assert result["source_sha256"] == result["launch"]["source_sha256"]
