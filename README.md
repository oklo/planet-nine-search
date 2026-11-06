# Planet Nine Search

Search TESS images using Gaia-guided stellar subtraction, joint-sector Hough
accumulation, and native-cadence photometry. Requires Python 3.11–3.13 on Linux
or macOS and 32 GB RAM. Apple Silicon can accelerate the Hough search; final
photometry uses sparse CPU float64 scoring. CUDA is experimental.

```sh
git clone https://github.com/oklo/planet-nine-search.git
cd planet-nine-search
python -m venv .venv
source .venv/bin/activate
pip install .
p9 worker --continuous --directory ./work --max-disk-gb 12 --reserve-gb 20
```

Workers need operator approval at [oklo.org](https://oklo.org/planet-nine/).
Each worker searches one field at a time. Acknowledged submissions release their
downloaded pixels; up to 32 recent compact results are retained locally.
Use `--keep-data` to retain pixels for inspection. Results await a full replay
on the trusted master before acceptance. Use `p9 status` for queue
counts and `p9 --help` for local runs and review. Defaults limit each search to
24 GiB resident memory and four hours; disk and memory limits are adjustable.
For images on a shared drive, keep the token on a private disk with
`--credentials ~/.config/planet-nine/worker.json`.

Scores are conditional statistics, not discovery significance. Injection
recovery measures sensitivity under the declared models; it does not establish
population-wide exclusion. Empirical templates for T < 12 stars are enabled
per sector in each task, because their benefit varies between fields.

[Method](docs/method.md) · [Coordinator](coordinator/README.md) ·
[Tested macOS/Python 3.12 environment](environments/macos-arm64-python312.txt)

Tests: `pip install '.[dev]' && pytest`. Coordinator tests require PHP/SQLite.
