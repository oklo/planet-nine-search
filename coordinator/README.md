# Coordinator

PHP 8.1+ with PDO SQLite. Keep the database and review key outside the web root.
Set `P9_DATABASE` for both PHP and the administrative shell. Install on a local
filesystem with reliable SQLite locking. The hosting account is trusted; use
a separate account or container from WordPress before opening participation.
Include `.htaccess`, or equivalent access rules, to protect administrative files.

```sh
php admin.php init
php admin.php add field.json 10
php admin.php workers
```

Generate canonical task JSON and bounded geometry bundles with
`tools/make_task.py`. `--empirical-star-sectors` enables T < 12 empirical templates
in named sectors. `tools/make_prior.py` builds a physical envelope from the pinned
Brown reference CSV; pass its output as `--prior`. Review geometry, motion masks,
and injection choices before enqueueing. Workers allow bundle downloads only
from `https://oklo.org` unless given another trusted `--bundle-origin`.

Registration is automatic. A worker has one active lease and starts its next
field after its compact result is acknowledged. Claims depend on available
fields and the worker's disk budget, with no daily or pending-review quota.
Leases renew for at most six hours; expired leases return to the queue without
requiring the worker to register again. Submitted results remain available until
reviewed. Repeated reported search failures or a demonstrated result inconsistency
can pause the affected worker for diagnosis. Existing revocations remain in force.

```sh
php admin.php requeue TASK_ID "reason"
php admin.php revoke WORKER_ID
php admin.php approve WORKER_ID
php admin.php rotate WORKER_ID /private/replacement-token.txt
php admin.php expire
```

Rotation invalidates leases and preserves the worker's history.
Reapprove after delivering the replacement token privately. State changes have
an audit trail so actual problems can be investigated.

## Review

`results` exports submissions. `review-plan TASK_ID` creates a stable, signed
consistency-check plan. There is no initial replay requirement or random replay
schedule. Candidate flags are retained for follow-up without automatically
repeating the search. To investigate a particular result, pass `--full-replay`
to `p9 review` with its plan.

```sh
php admin.php results
P9_REVIEW_KEY=/private/review.key php admin.php review-plan TASK_ID > plan.json
p9 review field.json submission.json --plan plan.json --directory review-001 --key /private/review.key
P9_REVIEW_KEY=/private/review.key php admin.php accept review-001/receipt.json
```

The operator supplies the task and a private 32-byte hexadecimal review key
(mode 0600). The reviewer checks identity, versions, input manifests, control
thresholds, and injection arithmetic on every report. Without `--plan`, the manually invoked command runs
a full replay. Full replay requires the submitted release; `--approved-source`
can explicitly allow an older release for a lightweight check.

For an operator-controlled machine, `php admin.php trust-local WORKER_ID on`
allows verification against operator-selected local files. Supply `--local-run /path/to/TASK_ID`
to check its retained artifacts; run that worker with `--keep-evidence` until
review finishes. Local verification does not schedule random replays.
Never grant this setting to a volunteer or use a path supplied in a submission.
Rotation and revocation remove local trust.

Receipts record `consistency`, `full_replay`, or `trusted_local`; historical
`statistical` receipts remain valid. Only `full_replay`
claims independent replay. A demonstrated inconsistency pauses the affected worker,
and places unchecked results from that worker/build on `audit_hold` for
reassessment. Update or withdraw their published sensitivity before resuming
accounting. Checksums detect changed files, not omitted computation. Consistency checks validate the reported result without independently repeating
every trajectory.
Population accounting remains a separate operation; acceptance adds no credit.

Submissions carry compressed diagnostic files and archive provenance inside the
stored report. Review checks their hashes and limits decompression. Keep the
original field bundle and reviewed evidence; downloaded pixels can be reacquired
for follow-up. `p9 restore STORE ARCHIVE --directory restored` reads a compact
master archive without changing the original accounting.

## Deployment and recovery

The audit policy uses the existing v2 database; no schema migration is needed.
Back up before deploying the PHP files together. For v1, stop claims, back up,
deploy the current coordinator, then run `migrate`.
Migration is transactional and retains old tasks/results as `legacy`; publish
new v2 tasks and explicitly reapprove workers. Historical results are unchanged.
Deploy all PHP files together. Do not run mixed application versions.

```sh
php admin.php backup /private/queue-backup.sqlite
php admin.php migrate
P9_DATABASE=/private/restored.sqlite php admin.php restore /private/queue-backup.sqlite
```

`backup` takes a WAL-consistent SQLite snapshot. `restore` checks integrity and
requires an unused destination. Test restoration privately before switching the
service path. To roll back, restore the pre-migration backup and previous code
together while claims are stopped. Keep regular backups and run `expire` from
cron if the service is idle.

The optional `coordination/` map uses a reviewed static snapshot; unverified
submissions affect queue counts only. Noindex and an unlinked URL are not access
controls. No credentials or accepted results are served through the public API.

Tests use disposable databases and concurrent workers. On macOS without PHP,
use the cached `php:8.2-cli` image:
`P9_TEST_DOCKER=1 pytest tests/test_coordinator.py --basetemp .pytest_cache/php`.

A worker with one pending result may claim its next field; at two, it retries
after 60 seconds. `admin.php results` exports pending submissions oldest first, ordered by
integer `submitted` (Unix seconds) and `result_id`. Review services must handle
this bounded backlog before enabling the coordinator update. Drain old-source
reviews before upgrading a pinned replay environment.


### Operator handover

Stop the outgoing supplier, reviewer and publisher. Read `php admin.php operator-status` and reconcile the last reviewed result and snapshot digest with the incoming host. Set `P9_OPERATOR_FENCE` to a fresh private 64-character hex token on the incoming operator. Run `php admin.php operator-claim HOST LAST_REVIEW SNAPSHOT_SHA256` with the reconciled values. Renew with `operator-renew` at least every five minutes. Release with `operator-release` after stopping writes.

After first enrollment the coordinator requires the current token for enqueue, requeue, review-plan and acceptance. Expiry leaves those operations closed until another claim. Workers continue claiming and returning fields. Website publication still needs its snapshot digest check.

### Field memory profiles

Run `python3 profile_memory.py /path/to/queue.sqlite /path/to/fields` after publishing bundles or from a minute cron. The profiler verifies each local bundle against the frozen task digest and stores a scheduling estimate for pending and leased fields. Repeated runs only inspect new fields. Keep both paths on the coordinator host. A missing or invalid bundle stays ineligible for RAM-aware workers until profiled.

The estimate uses the 140 × 140 stamp, cadence count, in-stamp stellar templates, detector and Gaia columns, and overlapping sector fits. The initial model includes 50% headroom. It is a scheduling estimate for the full search, including refits, and can overestimate fields that finish at quality screening. Worker logs report sampled peak RSS for comparison.

RAM-aware claims send `memory_bytes`. Operators can set the same cap for an older managed worker in `settings` under `worker-ram:<authenticated worker id>`, in bytes. Unconfigured older clients retain their existing claim behavior. A confirmed memory exit defers that field for the reporting worker at the same or a smaller allocation. Other workers can still claim it if it fits their budget.
