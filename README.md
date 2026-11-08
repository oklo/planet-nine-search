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
Each worker searches one field at a time. Submissions include compressed diagnostic
evidence. After acknowledgement, workers keep only 32 small receipts; images and
working files are removed. Use `--keep-data` to retain a local run for inspection.
New volunteers receive three
full reviews per software/device configuration, then 10% random audits. Trusted
local runs receive consistency checks and occasional audits. Candidates always
receive full review. Use `p9 status` for queue counts and `p9 --help` for commands. Defaults limit each search to
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
