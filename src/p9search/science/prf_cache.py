"""Build missing lkprf detector grids in a private directory, then publish atomically."""

import fcntl
import os
import tempfile
from pathlib import Path

import numpy as np
import requests
from scipy.ndimage import label, uniform_filter


def cache_directory(camera, ccd, sector):
    import fitsio
    from lkprf import PACKAGEDIR
    from lkprf.data import _tess_prefixes, _tess_prefixes_S1_3

    group = 1 if sector <= 3 else 4
    filename = f"tess-prf-cam{camera}-ccd{ccd}-sec{group}.fits"
    packaged = Path(PACKAGEDIR) / "data"
    if (packaged / filename).is_file():
        return str(packaged) + "/"
    root = Path.home() / ".cache" / "planet-nine-search" / "prf"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("PRF cache must be a private directory")
    prefix = (_tess_prefixes_S1_3 if group == 1 else _tess_prefixes)[camera][ccd]
    target = root / filename
    with (root / (filename + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            with tempfile.TemporaryDirectory(dir=root) as temp:
                path = Path(temp)
                with fitsio.FITS(str(path / filename), mode="rw") as grid:
                    for col in (45, 557, 1069, 1580, 2092):
                        for row in (1, 513, 1025, 1536, 2048):
                            url = (
                                f"https://archive.stsci.edu/missions/tess/models/prf_fitsfiles/start_s000{group}/"
                                f"cam{camera}_ccd{ccd}/tess{prefix}-prf-{camera}-{ccd}-row{row:04}-col{col:04}.fits"
                            )
                            with requests.get(url, timeout=(15, 60)) as response:
                                response.raise_for_status()
                                (path / "input.fits").write_bytes(response.content)
                            with fitsio.FITS(str(path / "input.fits")) as source:
                                data, header = source[0].read(), source[0].read_header()
                            # Match the lkprf 1.1 grid builder's edge treatment.
                            aper = data > 0.0005
                            regions = label(aper)[0]
                            core = np.bincount(regions[aper].ravel()).argmax()
                            data[uniform_filter(regions != core, size=10)] = 0
                            grid.write(data, header=header)
                os.replace(path / filename, target)
    return str(root) + "/"
