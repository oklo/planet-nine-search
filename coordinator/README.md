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
php admin.php approve WORKER_ID
```

Generate canonical task JSON and bounded geometry bundles with
`tools/make_task.py`. `--empirical-star-sectors` enables T < 12 empirical templates
in named sectors. `tools/make_prior.py` builds a physical envelope from the pinned
Brown reference CSV; pass its output as `--prior`. Review geometry, motion masks,
and injection choices before enqueueing. Workers allow bundle downloads only
from `https://oklo.org` unless given another trusted `--bundle-origin`.

Registration creates a pending worker. Approval allows one active lease, one
unreviewed result, and at most eight new claims per day by default (operator
configurable from 1 to 96). Leases renew for at most
six hours. Unreviewed results expire after 24 hours; the task returns to the
queue and the worker needs reapproval. Three failed or expired leases also
require reapproval. Worker reports never permanently retire tasks.

```sh
php admin.php requeue TASK_ID "reason"
php admin.php revoke WORKER_ID
php admin.php rotate WORKER_ID /private/replacement-token.txt
php admin.php reserve WORKER_ID 4
php admin.php claim-limit WORKER_ID 12
php admin.php expire
```

Rotation invalidates leases and keeps the worker's history and claim limits.
Reapprove after delivering the replacement token privately. `reserve` leaves
that many pending tasks for other workers. All state changes have an audit trail.

## Review

`results` exports submissions. `review-plan TASK_ID` assigns a signed review
plan after submission: three successful full replays per worker/build/device,
then a 10% random audit rate. The saved decision is reused on retries. Candidates
always require full replay. A changed build starts probation again.

```sh
php admin.php results
P9_REVIEW_KEY=/private/review.key php admin.php review-plan TASK_ID > plan.json
p9 review field.json submission.json --plan plan.json --directory review-001 --key /private/review.key
P9_REVIEW_KEY=/private/review.key php admin.php accept review-001/receipt.json
```

The operator supplies the task and a private 32-byte hexadecimal review key
(mode 0600). The reviewer checks identity, versions, input manifests, control
thresholds, and injection arithmetic on every report. Without `--plan`, it runs
a full replay. Full replay requires the submitted release; `--approved-source`
can explicitly allow an older release for a lightweight check.

For an operator-controlled machine, `php admin.php trust-local WORKER_ID on`
allows local verification without probation. Supply `--local-run /path/to/TASK_ID`
to check its retained artifacts; run that worker with `--keep-evidence` until
review finishes. Local runs also receive 10% random audits.
Never grant this setting to a volunteer or use a path supplied in a submission.
Rotation and revocation remove local trust.

Receipts record `full_replay`, `trusted_local`, or `statistical`; only the first
claims independent replay. A failed audit suspends approval, resets probation,
and places unchecked results from that worker/build on `audit_hold` for
reassessment. Update or withdraw their published sensitivity before resuming
accounting. Checksums detect changed files, not omitted computation. Statistical
acceptance is a sampling policy, not proof that every trajectory was searched.
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
