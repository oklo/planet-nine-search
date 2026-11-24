"""Score a native trajectory using every identifiable sector contribution.

The three fully covered anchor requirement is unchanged. Clipped sectors add
their rendered likelihood without rescaling flux by a coverage fraction.
"""

import numpy as np

from .sector_photometry import combine, coverage
from .templates import bin_template


def bright_axes(xy, good, meta):
    """Nearest Gaia T<8 rows and columns along the in-stamp part of a track."""
    if "gaia_dr3_bright_axes" not in meta:
        return {}
    size = meta.get("size", 140)
    track = np.asarray(xy)[good]
    track = track[((track >= -0.5) & (track < size - 0.5)).all(axis=1)]
    stars = meta["gaia_dr3_bright_axes"]
    if not len(track) or not stars:
        return {"bright_axes": {"catalog_stars": len(stars), "in_stamp_epochs": len(track)}}
    result = {"catalog_stars": len(stars), "in_stamp_epochs": len(track)}
    for axis, label in enumerate(("column", "row")):
        distances = [float(np.abs(track[:, axis] - star[axis + 1]).min()) for star in stars]
        i = int(np.argmin(distances))
        result[label] = {"pixels": distances[i], "source_id": str(stars[i][0])}
    dx, dy = track[-1] - track[0]
    result["angle_from_row_deg"] = (
        float(np.degrees(np.arctan2(abs(dy), abs(dx)))) if dx or dy else None
    )
    return {"bright_axes": result}


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
                **bright_axes(xy, good, sector.meta),
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
