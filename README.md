# Planet Nine Search

Search TESS images for slow moving sources. Workers use Gaia-guided stellar
subtraction, a joint-sector Hough search, and native-cadence PRF photometry.
Negative-flux and reversed-motion searches provide comparison controls.

Requires Linux or macOS, Python 3.11–3.13, and 32 GB RAM. Apple Silicon GPU
execution is validated; CPU execution is slower. CUDA support is experimental.

```sh
git clone https://github.com/oklo/planet-nine-search.git
cd planet-nine-search
python -m venv .venv
source .venv/bin/activate
pip install .
p9 worker --directory ./work --max-disk-gb 12 --reserve-gb 20
```

The worker registers with [oklo.org](https://oklo.org/planet-nine/), claims one
field, downloads its public TESS pixels, and submits a compact result. Run it
again for another field, or add `--continuous`. Claims expire if a worker
disappears. Downloads resume; completed evidence stays in your work directory.
Use `p9 status` to inspect the shared queue and `p9 --help` for local runs.

Scores are conditional statistics, **not discovery significance**. Submitted
results remain unverified. A field with no flags is not a calibrated exclusion;
injection recovery and independent candidate review are separate requirements.

See [the method](docs/method.md) for scope and [coordination](coordinator/README.md)
for hosting your own queue. Tests: `pip install '.[dev]' && pytest`.
