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

The first start registers the worker and claims an available field automatically.
No GitHub account or approval request is needed. Each worker downloads one field,
searches it, submits a compact result, and moves on after acknowledgement.
There is no daily quota or pause for pending review. The coordinator checks result
identity, numerical consistency, and diagnostic checksums without repeating the
search. Full reruns are an explicit diagnostic or scientific follow-up choice.
Completed image arrays are deleted once compact submission evidence is ready,
including during network outages. After acknowledgement, only 32 compressed
receipts remain (at most 256 MiB). Routine interrupted downloads are discarded and
recomputed. Unacknowledged results and evidence are preserved.
Use `--keep-data` or `--keep-evidence` only for local diagnosis.

Routine waits are silent; `work/worker-state.json` records the current state.
Each search log and the session helper's output log retain at most 1 MiB. The helper
keeps one bounded previous log. Use `--verbose` for state-change messages or
`--log-file /private/path/worker.log` to bound output under another service manager.
Continuous workers retry queue, disk and temporary network waits.
Unknown errors, integrity failures, and memory/runtime overruns stop for diagnosis.
Use `p9 status` for queue counts and `p9 --help` for commands. Defaults limit each search to
24 GiB resident memory and four hours; disk and memory limits are adjustable.
The 12 GiB allocation covers the work directory. Installation files, the small
upstream PRF calibration cache, and operating-system swap are separate.
For images on a shared drive, keep the token on a private disk with
`--credentials ~/.config/planet-nine/worker.json`.

For an agent-managed background worker, use the
[worker skill](skills/planet-nine-worker/SKILL.md). Its session helper starts,
checks and stops a continuous worker; ordinary waits do not need an agent turn.
Codex users can copy `skills/planet-nine-worker` into `~/.codex/skills/`.

Scores are conditional statistics, not discovery significance. Injection
recovery measures sensitivity under the declared models; it does not establish
population-wide exclusion. Empirical templates for T < 12 stars are enabled
per sector in each task, because their benefit varies between fields.

[Method](docs/method.md) · [Coordinator](coordinator/README.md) ·
[Tested macOS/Python 3.12 environment](environments/macos-arm64-python312.txt)

Tests: `pip install '.[dev]' && pytest`. Coordinator tests require PHP/SQLite.

Pixel masks remove unreliable measurements without rejecting the rest of a field.
Native injections rebuild the same foreground model and masks; only admitted
recoveries enter sensitivity accounting. Historical results retain their original
quality policy.
