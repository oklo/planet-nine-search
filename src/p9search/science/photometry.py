"""Score a native trajectory using every identifiable sector contribution.

The three fully covered anchor requirement is unchanged. Clipped sectors add
their rendered likelihood without rescaling flux by a coverage fraction.
"""

import numpy as np

from .sector_photometry import combine, coverage
from .templates import bin_template


def score_native_proposal(proposal, sectors, prepared, renderer=bin_template, unavailable=None):
    parameters = np.asarray(proposal["parameters"], float)
    orientation = float(proposal["orientation"])
    sign = float(proposal["sign"])
    details = []
    for sector, data in zip(sectors, prepared, strict=True):
        sid = int(sector.meta["sector"])
        good = np.asarray(sector.cadences["native_keep"], bool)
        if not good.any():
            if unavailable is not None:
                unavailable.append(dict(sector=sid, reason="No good native exposures"))
            continue
        xy = sector.exact_positions(parameters, native=True, orientation=orientation, kepler=True)
        ctx = coverage(xy, good)
        if ctx["stamp_overlap_fraction"] == 0:
            if unavailable is not None:
                unavailable.append(
                    dict(sector=sid, **ctx, reason="No good native PRF stamp overlap")
                )
            continue
        p, region = renderer(xy, sector.meta, sector.cadences)
        if len(p) != len(sector.geom["time"]):
            raise ValueError("Native source bin mismatch")
        result = data["projector"].score(p, region)
        if not result["identifiable"]:
            if unavailable is not None:
                unavailable.append(
                    dict(sector=sid, **ctx, reason="Rendered source amplitude is unidentifiable")
                )
            continue
        details.append(
            dict(
                sector=sid,
                **ctx,
                anchor=ctx["coverage"] >= 0.95,
                flux=result["flux"],
                sigma=result["sigma"],
                formal_snr=sign * result["formal_snr"],
                information_fraction=result["information_fraction"],
            )
        )
    result = combine(details, sign)
    if result is not None:
        result["information_fraction"] = float(
            np.median([r["information_fraction"] for r in details])
        )
    return result, details
