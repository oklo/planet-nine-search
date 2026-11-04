# Release validation

Validated on 4 November 2026 in a fresh Python 3.12 environment on an Apple
M4 Max, with NumPy 2.5.4, SciPy 1.18.1, PyTorch 2.15.0, Astropy 7.2.3,
tesscube 1.2.1, and lkprf 1.1.0.

- Numerical, native-rounding, corruption, task-validation, disk-guard, and worker
  failure tests passed. PHP integration tests passed separately on the actual
  host with four concurrent server workers: exclusive claims, stale-owner
  rejection, idempotent submission, priorities/budgets, and failure quarantine.
- On a real four-sector field (TESS sectors 1, 28, 68, 95), all quality masks
  matched the research pipeline exactly. Twelve sampled native flux estimates
  differed by at most 0.018 of their conditional uncertainty across the dependency
  change; relative uncertainty differences were below 0.00004.
- A full replay evaluated 182,277 retained motions from a 518,911-motion grid,
  selected and scored 192 proposals in each of the physical, negative-flux, and
  reversed-motion arms, and retained the same eight follow-up flags. The control
  maximum was 16.5972, compared with 16.5928 in the reference. Runtime audits of
  the accelerated selector all passed. Cached-data replay took 296 seconds.
- Four newly downloaded native rows and both averaged arrays were bitwise equal
  to the archived reference. Restart verified their hashes and skipped them.

These checks establish port and transport fidelity on the tested data. They do
not clear the eight candidates, validate CUDA hardware, or calibrate completeness
and false-alarm probabilities across the survey. Full field specifications and
result manifests retain input hashes and installed dependency versions so those
comparisons can be extended.
