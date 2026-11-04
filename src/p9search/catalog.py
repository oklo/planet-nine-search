"""Freeze Gaia DR3 stars at each TESS sector's epoch."""

import io

import numpy as np
import requests
from astropy.io import fits
from astropy.table import Table
from astropy.time import Time
from astropy.wcs import WCS

from .storage import canonical, read_json


def catalog(directory):
    meta = read_json(directory / "metadata.json")
    wcs = WCS(fits.Header.fromtextfile(directory / "wcs.txt"))
    corners = np.array([[-8, -8], [148, -8], [148, 148], [-8, 148]], float)
    origin = [meta["origin_column"], meta["origin_row"]]
    sky = wcs.all_pix2world(corners + origin, 1)
    polygon = ",".join(f"{v:.9f}" for v in sky.ravel())
    query = (
        "SELECT source_id,ra,dec,pmra,pmdec,ref_epoch,phot_g_mean_mag,phot_bp_mean_mag,phot_rp_mean_mag "
        "FROM gaiadr3.gaia_source WHERE 1=CONTAINS(POINT('ICRS',ra,dec),"
        f"POLYGON('ICRS',{polygon})) AND phot_g_mean_mag IS NOT NULL"
    )
    for endpoint in [
        "https://gea.esac.esa.int/tap-server/tap/sync",
        "https://gaia.ari.uni-heidelberg.de/tap/sync",
    ]:
        try:
            response = requests.post(
                endpoint,
                data={
                    "REQUEST": "doQuery",
                    "LANG": "ADQL",
                    "FORMAT": "fits",
                    "MAXREC": 100000,
                    "QUERY": query,
                },
                timeout=(15, 300),
            )
            response.raise_for_status()
            if len(response.content) > 32 * 2**20:
                raise ValueError("Gaia response exceeds field limit")
            table = Table.read(io.BytesIO(response.content), format="fits")
            if not 0 < len(table) < 100000:
                raise ValueError("Gaia query empty or truncated")
            break
        except (requests.RequestException, ValueError):
            if endpoint.endswith("heidelberg.de/tap/sync"):
                raise

    def values(key):
        return np.asarray(np.ma.filled(table[key], np.nan), float)

    with np.load(directory / "cadences.npz") as cadences:
        epoch = Time(np.mean(cadences["time"]) + 2457000, format="jd", scale="tdb").jyear
    dec = values("dec") + np.nan_to_num(values("pmdec")) * (epoch - values("ref_epoch")) / 3.6e6
    ra = values("ra") + np.nan_to_num(values("pmra")) * (
        epoch - values("ref_epoch")
    ) / 3.6e6 / np.cos(np.deg2rad(values("dec")))
    xy = (
        wcs.all_world2pix(np.column_stack([ra, dec]), 1, tolerance=1e-10, maxiter=100, quiet=True)
        - origin
    )
    color = values("phot_bp_mean_mag") - values("phot_rp_mean_mag")
    g = values("phot_g_mean_mag")
    tmag = g - 0.00522555 * color**3 + 0.0891337 * color**2 - 0.633923 * color + 0.0324473
    tmag = np.where(np.isfinite(tmag), tmag, g - 0.430)
    valid = (
        np.isfinite(xy).all(axis=1)
        & np.isfinite(tmag)
        & (g < 25)
        & (xy.min(axis=1) > -7)
        & (xy.max(axis=1) < 147)
    )
    np.savez_compressed(
        directory / "stars.npz",
        xy=xy[valid],
        tmag=tmag[valid],
        g=g[valid],
        flux=10 ** ((20.44 - tmag[valid]) / 2.5),
        source_id=np.asarray(table["source_id"])[valid],
        provenance=canonical(
            {"query": query, "endpoint": endpoint, "epoch_jyear": epoch, "catalog": "Gaia DR3"}
        ),
    )
