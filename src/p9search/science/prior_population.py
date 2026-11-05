"""Solar two-body propagation of the rounded P9 reference orbital elements.

These are planning coordinates, not precise candidate ephemerides. Element
rounding must be assessed against the catalog's reference sky positions.
Angles are J2000 ecliptic degrees; outputs are equatorial AU and AU/day.
"""

import csv

import numpy as np
from astropy.coordinates import get_body_barycentric_posvel
from astropy.time import Time

from ..policy import POPULATION_SHA256
from ..storage import sha256
from .physical_prior import POLICY, envelope

GAUSS_K = 0.01720209895
OBLIQUITY = np.deg2rad(23.439291111)


def elements_state(a, e, inclination, longitude_perihelion, node, mean_anomaly, days=0.0):
    a, e, inclination, longitude_perihelion, node, mean_anomaly, days = np.broadcast_arrays(
        *[
            np.asarray(x, float)
            for x in [a, e, inclination, longitude_perihelion, node, mean_anomaly, days]
        ]
    )
    if (
        np.any(a <= 0)
        or np.any((e < 0) | (e >= 1))
        or not all(
            np.isfinite(x).all()
            for x in [a, e, inclination, longitude_perihelion, node, mean_anomaly, days]
        )
    ):
        raise ValueError("Finite elliptic elements and time offsets required")
    n = GAUSS_K / a**1.5
    mean = (np.deg2rad(mean_anomaly) + n * days + np.pi) % (2 * np.pi) - np.pi
    eccentric = np.where(e < 0.8, mean, np.where(mean >= 0, np.pi, -np.pi))
    for _ in range(50):
        correction = (eccentric - e * np.sin(eccentric) - mean) / (1 - e * np.cos(eccentric))
        eccentric -= correction
        if np.max(abs(correction)) < 1e-13:
            break
    else:
        raise ArithmeticError("Kepler equation did not converge")
    c, s = np.cos(eccentric), np.sin(eccentric)
    b = np.sqrt(1 - e * e)
    plane_position = np.stack([a * (c - e), a * b * s], axis=-1)
    rate = n / (1 - e * c)
    plane_velocity = np.stack([-a * s * rate, a * b * c * rate], axis=-1)
    inc = np.deg2rad(inclination)
    asc = np.deg2rad(node)
    arg = np.deg2rad(longitude_perihelion - node)
    ca, sa = np.cos(asc), np.sin(asc)
    cw, sw = np.cos(arg), np.sin(arg)
    ci, si = np.cos(inc), np.sin(inc)
    x_axis = np.stack([ca * cw - sa * sw * ci, sa * cw + ca * sw * ci, sw * si], axis=-1)
    y_axis = np.stack([-ca * sw - sa * cw * ci, -sa * sw + ca * cw * ci, cw * si], axis=-1)
    rotation = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, np.cos(OBLIQUITY), -np.sin(OBLIQUITY)],
            [0.0, np.sin(OBLIQUITY), np.cos(OBLIQUITY)],
        ]
    )
    basis = np.stack([x_axis @ rotation.T, y_axis @ rotation.T], axis=-1)
    return (
        np.einsum("...ij,...j->...i", basis, plane_position),
        np.einsum("...ij,...j->...i", basis, plane_velocity),
    )


def sky_coordinates(vector):
    vector = np.asarray(vector, float)
    return np.stack(
        [
            np.rad2deg(np.arctan2(vector[..., 1], vector[..., 0])) % 360,
            np.rad2deg(np.arctan2(vector[..., 2], np.linalg.norm(vector[..., :2], axis=-1))),
        ],
        axis=-1,
    )


def sky_separation_arcsec(first, second):
    def rays(sky):
        ra, dec = np.moveaxis(np.deg2rad(sky), -1, 0)
        return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)

    chord = np.linalg.norm(rays(first) - rays(second), axis=-1)
    return np.rad2deg(2 * np.arcsin(np.minimum(1, chord / 2))) * 3600


def propagate_batch(
    position, velocity, days, *, maximum_step=32.0, maximum_output_bytes=128 * 2**20
):
    position = np.asarray(position, float)
    velocity = np.asarray(velocity, float)
    days = np.asarray(days, float)
    if (
        position.ndim != 2
        or position.shape[1] != 3
        or not len(position)
        or velocity.shape != position.shape
        or days.ndim != 1
        or not all(np.isfinite(a).all() for a in [position, velocity, days])
        or not np.isfinite(maximum_step)
        or not 0.25 <= maximum_step <= 32.0
        or (len(days) and np.max(abs(days)) > 4000.0)
        or np.max(np.linalg.norm(velocity, axis=1)) > 0.01
    ):
        raise ValueError("Finite distant states and bounded short-baseline time offsets required")
    if len(days) * len(position) * 6 * 8 > maximum_output_bytes:
        raise MemoryError("Catalog batch output allocation exceeds declared cap")

    def acceleration(r):
        radius = np.linalg.norm(r, axis=1)
        if not np.isfinite(radius).all() or np.any(radius < 100.0):
            raise ValueError("Batch orbit left the declared radius>=100AU domain")
        return -(GAUSS_K**2) * r / radius[:, None] ** 3

    acceleration(position)
    p = np.empty((len(days), len(position), 3))
    v = np.empty_like(p)
    for side in [-1, 1]:
        r = position.copy()
        speed = velocity.copy()
        at = 0.0
        for absolute in np.unique(days[days * side >= 0] * side):
            target = float(side * absolute)
            steps = max(1, int(np.ceil(abs(target - at) / maximum_step)))
            step = (target - at) / steps
            for _ in range(steps):
                a = acceleration(r)
                k2v = acceleration(r + 0.5 * step * speed)
                k2r = speed + 0.5 * step * a
                k3v = acceleration(r + 0.5 * step * k2r)
                k3r = speed + 0.5 * step * k2v
                k4v = acceleration(r + step * k3r)
                k4r = speed + step * k3v
                r = r + step / 6 * (speed + 2 * k2r + 2 * k3r + k4r)
                speed = speed + step / 6 * (a + 2 * k2v + 2 * k3v + k4v)
            acceleration(r)
            selected = days == target
            p[selected] = r
            v[selected] = speed
            at = target
    if not np.isfinite(p).all() or not np.isfinite(v).all():
        raise ArithmeticError("Nonfinite batch result")
    return p, v


class LocalPriorPopulation:
    def __init__(self, path):
        if sha256(path) != POPULATION_SHA256:
            raise ValueError("Reference population checksum differs")
        with open(path) as stream:
            rows = [
                row
                for row in csv.DictReader(
                    (line for line in stream if not line.startswith("#")), skipinitialspace=True
                )
                if all(int(row[key]) == 0 for key in ("ZTF", "DES", "PS1"))
            ]
        if len(rows) != 21740:
            raise ValueError("Unexpected reference population size")
        self.rows = {
            key: np.array([float(row[key]) for row in rows])
            for key in ("a", "e", "inc", "varpi", "Omega", "M", "rad", "albedo", "index")
        }
        p, v = elements_state(*[self.rows[k] for k in ["a", "e", "inc", "varpi", "Omega", "M"]])
        sp, sv = get_body_barycentric_posvel(
            "sun", Time(2458270.0, format="jd", scale="tdb"), ephemeris="builtin"
        )
        self.position = p + sp.xyz.to_value("au")
        self.velocity = v + sv.xyz.to_value("au/day")
        self.cache = {}

    def context(self, frame, grid):
        epoch = frame.reference_time + 2457000.0
        if epoch not in self.cache:
            p, v = propagate_batch(self.position, self.velocity, [epoch - 2458270.0])
            pf, vf = propagate_batch(
                self.position, self.velocity, [epoch - 2458270.0], maximum_step=16.0
            )
            np.testing.assert_allclose(p, pf, atol=1e-8, rtol=0)
            np.testing.assert_allclose(v, vf, atol=1e-12, rtol=0)
            self.cache[epoch] = (p[0], v[0])
        if np.linalg.norm(frame.observer) >= POLICY["observer_solar_distance_bound_au"] - 0.1:
            raise ValueError("Observer outside prior calibration")
        p, v = self.cache[epoch]
        relative = p - frame.observer
        distance = np.linalg.norm(relative, axis=1)
        separation = sky_separation_arcsec(sky_coordinates(relative), frame.center) / 3600.0
        radius = max(
            POLICY["sky_radius_deg"],
            np.partition(separation, POLICY["minimum_models"] - 1)[POLICY["minimum_models"] - 1]
            + 1e-9,
        )
        ids = np.flatnonzero(separation <= radius)
        matrix = np.empty((len(ids), 3, 3))
        matrix[:, :, :2] = frame.tangent.T
        matrix[:, :, 2] = relative[ids] / distance[ids, None]
        components = np.linalg.solve(matrix, v[ids, :, None])[:, :, 0]
        values = np.c_[components[:, :2] / distance[ids, None], 1 / distance[ids]]
        inverse = (1 / grid["distance_au"][0] - 1 / grid["distance_au"][1]) / (
            grid["inverse_distance_slices"] - 1
        )
        result = envelope(
            values,
            self.rows["rad"][ids],
            self.rows["albedo"][ids],
            grid["joint_angular_step_rad_per_day"],
            inverse,
        )
        result.update(
            sky_radius_deg=float(radius), reference_ids=self.rows["index"][ids].astype(int).tolist()
        )
        result.pop("raw_optical_radius_max_earth")
        result.update(
            version=POLICY["version"],
            population_sha256=POPULATION_SHA256,
            epoch_jd=epoch,
            reference_frame_center=frame.center.tolist(),
        )
        return result
