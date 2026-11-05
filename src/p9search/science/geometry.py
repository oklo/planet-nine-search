import json
from pathlib import Path

import numpy as np
import torch
from astropy.io import fits
from astropy.wcs import WCS
from scipy.integrate import solve_ivp
from scipy.interpolate import CubicSpline
from torch.nn import functional as F

from .ranking import local_contrast


def sky_vectors(sky):
    ra, dec = np.deg2rad(np.asarray(sky)).T
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


class SkyFrame:
    def __init__(self, reference_directory):
        directory = Path(reference_directory)
        self.meta = json.loads((directory / "metadata.json").read_text())
        self.wcs = WCS(fits.Header.fromtextfile(directory / "wcs.txt"))
        self.origin = np.array([self.meta["origin_column"], self.meta["origin_row"]])
        self.center = self.wcs.all_pix2world((self.origin + 69.5)[None], 1)[0]
        ra, dec = np.deg2rad(self.center)
        self.tangent = np.array(
            [
                [-np.sin(ra), np.cos(ra), 0],
                [-np.cos(ra) * np.sin(dec), -np.sin(ra) * np.sin(dec), np.cos(dec)],
            ]
        )
        self.reference_time = float(np.load(directory / "geometry.npz")["reference_time"])
        observer = directory.parent / f"observer_s{self.meta['sector']}.npz"
        z = np.load(observer)
        self.observer = CubicSpline(z["time"], z["position"])(self.reference_time)

    def ray(self, xy):
        return sky_vectors(self.wcs.all_pix2world(np.atleast_2d(xy) + self.origin, 1))


class SectorGeometry:
    def __init__(self, directory, frame):
        directory = Path(directory)
        self.directory = directory
        self.frame = frame
        self.meta = json.loads((directory / "metadata.json").read_text())
        self.geom = dict(np.load(directory / "geometry.npz"))
        self.cadences = dict(np.load(directory / "cadences.npz"))
        self.wcs = WCS(fits.Header.fromtextfile(directory / "wcs.txt"))
        self.origin = np.array([self.meta["origin_column"], self.meta["origin_row"]])
        observer = np.load(directory.parent / f"observer_s{self.meta['sector']}.npz")
        self.observer = CubicSpline(observer["time"], observer["position"])
        ray = sky_vectors(np.array([frame.center]))[0]
        vectors = ray + frame.tangent * 1e-6
        sky = np.c_[
            np.rad2deg(np.arctan2(vectors[:, 1], vectors[:, 0])) % 360,
            np.rad2deg(np.arctan2(vectors[:, 2], np.linalg.norm(vectors[:, :2], axis=1))),
        ]
        center = self.wcs.all_world2pix(frame.center[None], 1)[0]
        self.jacobian = (self.wcs.all_world2pix(sky, 1) - center).T / 1e-6
        parallax = -(self.observer(self.geom["time"]) @ frame.tangent.T) @ self.jacobian.T
        linear = np.c_[np.ones(len(parallax)), self.geom["dt"]]
        self.intercept, self.slope = np.linalg.lstsq(linear, parallax, rcond=None)[0]
        self.curvature = parallax - linear @ np.array([self.intercept, self.slope])
        self.offset = self.intercept + self.jacobian @ (frame.tangent @ frame.observer)
        self.epoch = float(self.geom["reference_time"])
        yy, xx = np.indices((140, 140))
        self.reference_grid = np.stack([xx, yy], axis=-1)
        world = frame.wcs.all_pix2world(self.reference_grid.reshape(-1, 2) + frame.origin, 1)
        self.base_grid = (self.wcs.all_world2pix(world, 1) - self.origin).reshape(140, 140, 2)

    def exact_positions(
        self, parameters, native=False, orientation=1.0, radial_velocity=0.0, kepler=False
    ):
        """Apparent detector positions from a 3D state and observer ephemeris.

        Parameters are reference apparent x,y; tangent angular velocity (rad/d);
        inverse reference observer distance (AU^-1). Optional solar two-body
        propagation replaces straight Cartesian motion for final verification.
        Light time variations and planetary perturbations are not modeled here.
        """
        x, y, qx, qy, g = np.asarray(parameters, float)
        if g <= 0:
            raise ValueError("Physical inverse distance must be positive")
        time = self.cadences["native_time"] if native else self.geom["time"]
        dt = time - self.frame.reference_time
        ray = self.frame.ray([x, y])[0]
        position = ray / g + self.frame.observer
        velocity = (np.array([qx, qy]) @ self.frame.tangent) / g + radial_velocity * ray
        if kepler:
            initial = np.r_[position, velocity]
            states = np.empty((len(time), 6))

            def rhs(t, state):
                r = state[:3]
                return np.r_[state[3:], -(0.01720209895**2) * r / np.linalg.norm(r) ** 3]

            for side in [-1, 1]:
                selected = dt * side >= 0
                if not selected.any():
                    continue
                far = dt[selected][np.argmax(abs(dt[selected]))]
                if far == 0:
                    states[selected] = initial
                    continue
                solution = solve_ivp(
                    rhs, (0, float(far)), initial, rtol=1e-11, atol=1e-12, dense_output=True
                )
                if not solution.success:
                    raise RuntimeError(solution.message)
                states[selected] = solution.sol(dt[selected]).T
            vector = states[:, :3] - self.observer(time)
        else:
            vector = position + dt[:, None] * velocity - self.observer(time)
        sky = np.c_[
            np.rad2deg(np.arctan2(vector[:, 1], vector[:, 0])) % 360,
            np.rad2deg(np.arctan2(vector[:, 2], np.linalg.norm(vector[:, :2], axis=1))),
        ]
        nominal = self.wcs.all_world2pix(sky, 1) - self.origin
        if orientation < 0:
            base = (
                self.wcs.all_world2pix(
                    self.frame.wcs.all_pix2world(np.array([[x, y]]) + self.frame.origin, 1), 1
                )[0]
                - self.origin
            )
            nominal = 2 * base - nominal
        if native:
            point = self.geom["native_pointing"] - np.median(
                self.geom["native_pointing"][self.cadences["native_keep"]], axis=0
            )
        else:
            point = self.geom["pointing"]
        return nominal + point

    def approximate_positions(self, parameters, orientation=1.0):
        x, y, qx, qy, g = parameters
        q = np.array([qx, qy]) * orientation
        g *= orientation
        base = (
            self.wcs.all_world2pix(
                self.frame.wcs.all_pix2world(np.array([[x, y]]) + self.frame.origin, 1), 1
            )[0]
            - self.origin
        )
        anchor = (
            base + self.jacobian @ q * (self.epoch - self.frame.reference_time) + self.offset * g
        )
        return (
            anchor
            + self.geom["dt"][:, None] * (self.jacobian @ q + self.slope * g)
            + self.curvature * g
            + self.geom["pointing"]
        )


def grids(sectors, minimum=200.0, maximum=1500.0, joint_step=0.45, local_step=0.25):
    times = np.concatenate([s.geom["time"][s.geom["keep"]] for s in sectors])
    scale = max(np.linalg.norm(s.jacobian, 2) for s in sectors)
    angular_step = joint_step / (np.std(times) * scale)
    local_angular = local_step / (
        max(np.std(s.geom["dt"][s.geom["keep"]]) for s in sectors) * scale
    )
    frame = sectors[0].frame
    parallax = np.concatenate(
        [
            -(s.observer(s.geom["time"][s.geom["keep"]]) - frame.observer) @ frame.tangent.T
            for s in sectors
        ]
    )
    parallax -= parallax.mean(axis=0)
    inverse_step = joint_step / (np.sqrt(np.mean(np.sum(parallax**2, axis=1))) * scale)
    count = int(np.ceil((1 / minimum - 1 / maximum) / inverse_step)) + 1
    if not 2 <= count <= 128:
        raise ValueError("Distance lattice exceeds supported size")
    inverse = np.linspace(1 / maximum, 1 / minimum, count)
    specs = []
    for g in inverse:
        bound = np.sqrt(2) * 0.01720209895 * g**1.5
        n = int(np.ceil(bound / angular_step))
        if n > 1000 or sum(len(s["angular"]) for s in specs) + (2 * n + 1) ** 2 > 4_000_000:
            raise ValueError("Motion lattice exceeds supported size")
        iy, ix = np.mgrid[-n : n + 1, -n : n + 1]
        q = np.c_[ix.ravel(), iy.ravel()] * angular_step
        q = q[np.linalg.norm(q, axis=1) <= bound + angular_step / np.sqrt(2)]
        indices = np.rint(q / local_angular).astype(int)
        local, labels = np.unique(indices, axis=0, return_inverse=True)
        specs.append(
            dict(
                inverse_distance=float(g),
                angular=q,
                local_angular=local * local_angular,
                labels=labels,
            )
        )
    return specs, dict(
        distance_au=[minimum, maximum],
        inverse_distance_slices=count,
        joint_angular_step_rad_per_day=float(angular_step),
        local_angular_step_rad_per_day=float(local_angular),
        fine_motions=sum(len(s["angular"]) for s in specs),
        local_motions_per_sector=sum(len(s["local_angular"]) for s in specs),
        joint_rms_step_pixel=joint_step,
        local_rms_step_pixel=local_step,
        radial_velocity_in_coarse_search=0.0,
        acceleration_in_coarse_search="none; geometry approximation audited",
    )


def combine_sector_maps(banks, base_grids, offsets, labels, sign=1.0, minimum_sectors=3):
    """Register every fine motion and combine sufficient statistics, no pruning."""
    dev = banks[0].device
    h, w = banks[0].shape[-2:]
    b = len(labels)
    norm = torch.tensor([w - 1, h - 1], device=dev)
    total = torch.zeros((b, 2, h, w), device=dev)
    squared = torch.zeros((b, h, w), device=dev)
    count = torch.zeros((b, h, w), dtype=torch.int32, device=dev)
    for bank, grid, offset in zip(banks, base_grids, offsets):
        positions = grid[None] + offset[:, None, None, :]
        sampled = F.grid_sample(bank[labels], 2 * positions / norm - 1, align_corners=True)
        valid = sampled[:, 1] > 1e-20
        total += sampled
        count += valid.int()
        squared += torch.where(
            valid, sampled[:, 0] ** 2 / sampled[:, 1].clamp_min(1e-20), torch.zeros_like(squared)
        )
    den = total[:, 1].clamp_min(1e-20)
    z = sign * total[:, 0] / den.sqrt()
    chi = (squared - total[:, 0] ** 2 / den).clamp_min(0)
    coherent = z - chi.sqrt()
    return dict(
        formal=z,
        contrast=local_contrast(z),
        coherent=coherent,
        chi_sqrt=chi.sqrt(),
        valid=count >= minimum_sectors,
        numerator=total[:, 0],
        denominator=total[:, 1],
        sectors=count,
    )
