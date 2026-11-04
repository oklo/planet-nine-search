# Contributing

Install `pip install '.[dev]'`, then run `ruff check .`, `ruff format --check .`,
and `pytest`. Coordinator tests also require PHP with PDO SQLite. CI builds the
wheel and source archive from a clean checkout.

Keep scientific policy in the versioned protocol, orchestration in the worker,
and numerical operations in `science/`. Do not add field-specific exceptions,
research notebooks, image cubes, generated outputs, credentials, or dated driver
copies. Changes to scoring or selection require independent numerical checks
and a real-data replay; changing the task contract requires a protocol revision.

The coordinator deliberately keeps submitted results unverified. Changes to
leases must preserve exclusive claims, expiration, retry idempotency, and stale
worker rejection under concurrent requests.
