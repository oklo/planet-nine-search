# Planet Nine Search

Search NASA’s TESS images for Planet Nine. Each worker claims a patch of sky,
subtracts stars using the Gaia catalog, tests possible orbits across several
TESS sectors, and sends its results to oklo.org.

## Run a worker

Use Linux or macOS with Python 3.11–3.13 and 32 GB of RAM.

```sh
git clone https://github.com/oklo/planet-nine-search.git
cd planet-nine-search
python -m venv .venv
source .venv/bin/activate
pip install .
p9 worker --continuous --directory ./work --max-disk-gb 12 --reserve-gb 20
```

The first run registers automatically. The command allows 12 GiB for worker
data and keeps 20 GiB free on the disk. Installation files and calibration
caches use additional space. If the work directory is shared, store the worker
token on a private disk with `--credentials ~/.config/planet-nine/worker.json`.

The worker waits when the queue is empty and claims another field as soon as
its last submission is acknowledged. Check `work/worker-state.json` for local
progress or run `p9 status` for queue counts.

For a background session, follow the [worker skill](skills/planet-nine-worker/SKILL.md).
Codex users can copy `skills/planet-nine-worker` into `~/.codex/skills/`.

[Search atlas](https://oklo.org/planet-nine/coordination/) ·
[Search method](docs/method.md) · [Validation](docs/validation.md) ·
[Coordinator](coordinator/README.md)
