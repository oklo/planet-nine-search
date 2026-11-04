# Search method

Version `tess-gaia-hough-v1` searches an operator-supplied, 140 × 140 pixel
field in three or four TESS sectors. The field bundle freezes spacecraft-TDB
cadences, pointing, WCS, observer positions, Gaia DR3 stars, conservative motion
masks, and any injection cases. No known-object track enters proposal generation.
The bundled observer ephemeris avoids spacecraft-kernel downloads on workers.

1. Download public TESS archive rows, check their version and byte counts, and
   retain native float32 strips. Average good exposures into 30-minute bins;
   flagged-only bins remain flagged. Every completed strip has a checksum.
2. Fit the Gaia scene, stellar amplitudes, image derivatives, and detector
   backgrounds. Stop before searching if abrupt-pixel masks exceed 2% of the
   initially weighted area. Accepted fits feed the search without a second fit.
3. Learn 16 noise-weighted temporal modes in four spatial folds. Forward both
   data and source models through the spatial and temporal projections.
4. Accumulate local Hough maps and register them across sectors on an orbital
   grid spanning 200–1500 AU. The operator's conservative masks restrict orbital
   inclination to at most 60 degrees. Keep 24 sky locations with eight distinct
   orbit alternatives each. The Metal selector certifies its compressed metric
   and checks its first and last batch in every distance slice against the
   ordinary PyTorch selector. Unsupported or uncertified cases use PyTorch.
5. Score every retained proposal with native-cadence calibrated PRFs and solar
   two-body trajectories. Require three sectors with at least 95% contained
   center coverage; add identifiable partial-sector measurements. Combine
   inverse-variance flux estimates and subtract the square root of the sector
   consistency chi-square from the formal S/N for ranking.
6. Repeat for negative flux and reversed motion. A positive candidate above
   both control maxima requests follow-up and stops calibration. These finite
   control samples do not establish a false-alarm probability.
7. Otherwise add each predeclared source to native pixels *before* averaging,
   retaining native float32 rounding and the original noise weights. Refit all
   foreground models. Require a recovered flux increment within 10%, absolute
   template RMS error at most 0.002 e-/s, and relative template error at most 10%.
   Run the same blind search. Score every proposal within 1.5 pixels RMS of the
   injected path, using up to 17 fixed good epochs per observed sector. Recovery
   requires a score above the already frozen control maximum.

`result.json` records dependency versions, source and input hashes, quality
screens, candidate measurements, controls, and assessed injection recoveries.
Full proposal tables and selector audits remain beside it. The coordinator
receives a compact result; it does not receive credentials or image cubes.

Conditional S/N is not discovery significance. Neither a clean field nor a
small set of successful injections establishes global completeness or posterior
exclusion. Source colors, radial velocity, grid mismatch, crowding, unmodeled
systematics, and the selected trial population matter. The coarse grid uses
zero radial velocity and straight Cartesian propagation; final trajectories
use solar two-body propagation. Light-time variation and planetary perturbations
are omitted. The release contains no trained neural classifier.

The maintained worker target is Linux or macOS with 32 GB RAM. Apple MPS is
validated on real data; CPU kernels are covered by numerical tests. CUDA uses
the PyTorch path and has not yet been validated on hardware. Disk limits apply
to the chosen work directory, including retained results; dependency installs
and third-party caches are additional. The worker never deletes old results
implicitly. Reuse the same directory after interruption to resume checked rows.
