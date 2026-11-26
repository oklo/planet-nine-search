# Planet Nine Search

Search TESS images using Gaia-guided stellar subtraction, joint-sector Hough
accumulation, and native-cadence photometry. Requires Python 3.11–3.13 on Linux
or macOS. A 16 GB machine can try one field. We recommend 32 GB for more
headroom, especially if you use the computer during a search. An 8 GB machine
is experimental. Apple Silicon can accelerate the Hough search; final
photometry uses sparse CPU float64 scoring.

```sh
git clone https://github.com/oklo/planet-nine-search.git
cd planet-nine-search
python3.12 -m venv .venv
source .venv/bin/activate
pip install .
p9 worker --directory ./work --max-disk-gb 12 --reserve-gb 20
```

This searches one available field. For continuous background work, use the session helper in the [worker skill](skills/planet-nine-worker/SKILL.md). The first start registers the worker and claims an available field automatically.
No GitHub account or approval request is needed. Each worker downloads one field,
searches it, submits a compact result, and in continuous mode claims the next field after acknowledgement.
There is no daily quota or pause for pending review. The coordinator checks result
identity, numerical consistency, and diagnostic checksums.
Completed image arrays are deleted once compact submission evidence is ready,
including during network outages. After acknowledgement, only 32 compressed
receipts remain (at most 256 MiB). Routine interrupted downloads are discarded and
recomputed. Unacknowledged results and evidence are preserved.
Use `--keep-data` or `--keep-evidence` only for local diagnosis.

Routine waits are silent; `work/worker-state.json` records the current state.
Each search log and the session helper's output log retain at most 1 MiB.
Use `--verbose` for state-change messages or
`--log-file /private/path/worker.log` to bound output under another service manager.
Continuous workers retry queue, disk and temporary network waits.
Unknown errors, integrity failures, and memory/runtime overruns stop for diagnosis.
Use `p9 status` for queue counts and `p9 --help` for commands. The worker uses
two CPU threads and allows four hours per field. Its default resident-memory
limit is 60% of physical RAM, rounded down to whole GiB, from 1 to 12 GiB.
That gives a 4 GiB limit on an 8 GB Mac, 9 GiB on a 16 GB Mac, and 12 GiB
on a 32 GB Mac. Some fields can exceed the smaller limits. To set your own
limits, use `--max-memory-gb` (GiB) and `--threads`.
The 12 GiB allocation covers the work directory. Installation files, the small
upstream PRF calibration cache, and operating-system swap are separate.
For images on a shared drive, keep the token on a private disk with
`--credentials ~/.config/planet-nine/worker.json`.

For an agent-managed background worker, use the
[worker skill](skills/planet-nine-worker/SKILL.md). Its session helper starts,
checks and stops a continuous worker; ordinary waits do not need an agent turn.
Codex users can copy `skills/planet-nine-worker` into `~/.codex/skills/`.

Scores are conditional statistics. Injection
recovery measures sensitivity under the declared models; it does not establish
population-wide exclusion. Empirical templates for T < 12 stars are enabled
per sector in each task, because their benefit varies between fields.

[Method](docs/method.md) · [Coordinator](coordinator/README.md) ·
[Tested macOS/Python 3.12 environment](environments/macos-arm64-python312.txt)

Tests: `pip install '.[dev]' && pytest`. Coordinator tests require PHP/SQLite.

Pixel masks remove unreliable measurements without rejecting the rest of a field.
Native injections rebuild the same foreground model and masks; only admitted
recoveries enter sensitivity accounting.
