import numpy as np


def sector_sums_full_padding(
    bundle, coarse, sector, angular, inverse_distance, psf, full_local_angular, orientation=1.0
):
    """Retain the original FFT support even when unused local bins are removed."""
    from .covariance_hough import CovarianceInformationBank
    from .local_hough import sector_sums

    full = np.asarray(full_local_angular)
    motions = np.c_[
        (full @ sector.jacobian.T + sector.slope * inverse_distance) * orientation,
        np.full(len(full), inverse_distance * orientation),
    ]
    offsets = (
        coarse["dt"][None, :, None] * motions[:, None, :2]
        + coarse["curvature"][None] * motions[:, None, 2:]
        + coarse["pointing"][None]
    )
    maximum = float(np.max(abs(offsets)))
    if "bank" not in bundle or bundle["bank"].max_offset < maximum:
        scene = bundle["scene"]
        bundle["bank"] = CovarianceInformationBank(
            bundle["temporal"],
            scene.time_weight,
            scene.pixel_weight,
            bundle["groups"],
            psf,
            max_offset=float(np.ceil(maximum) + 0.5),
        )
    return sector_sums(bundle, coarse, sector, angular, inverse_distance, psf, orientation)


def restrict_specs(specs, masks):
    """Remove fine motions and unused local bins without changing their values."""
    result = []
    if len(specs) != len(masks):
        raise ValueError("One mask per inverse-distance slice is required")
    for spec, mask in zip(specs, masks):
        mask = np.asarray(mask)
        if mask.dtype != bool or mask.shape != (len(spec["angular"]),) or not mask.any():
            raise ValueError("Each slice requires a nonempty boolean fine-motion mask")
        labels = np.asarray(spec["labels"])
        if labels.shape != mask.shape or labels.dtype.kind not in "iu":
            raise ValueError("Invalid fine-to-local index map")
        if labels.min() < 0 or labels.max() >= len(spec["local_angular"]):
            raise ValueError("Local motion index out of bounds")
        used, remap = np.unique(labels[mask], return_inverse=True)
        reduced = dict(
            inverse_distance=spec["inverse_distance"],
            angular=np.array(spec["angular"][mask], copy=True),
            local_angular=np.array(spec["local_angular"][used], copy=True),
            labels=remap,
        )
        np.testing.assert_array_equal(
            reduced["local_angular"][reduced["labels"]], spec["local_angular"][labels[mask]]
        )
        result.append(reduced)
    return result
