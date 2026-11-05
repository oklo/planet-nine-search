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
unreviewed result, and at most eight new claims per day. Leases renew for at most
six hours. Unreviewed results expire after 24 hours; the task returns to the
queue and the worker needs reapproval. Three failed or expired leases also
require reapproval. Worker reports never permanently retire tasks.

```sh
php admin.php requeue TASK_ID "reason"
php admin.php revoke WORKER_ID
php admin.php rotate WORKER_ID /private/replacement-token.txt
php admin.php reserve WORKER_ID 4
php admin.php expire
```

Rotation invalidates leases and keeps the worker's history and claim limits.
Reapprove after delivering the replacement token privately. `reserve` leaves
that many pending tasks for other workers. All state changes have an audit trail.

## Trusted review

`results` exports one bounded submission per line. Save the relevant line as
`submission.json`. On the trusted master, review it against the **operator's**
task file, with a fresh directory. Remote files never enter the replay cache.

```sh
php admin.php results
p9 review field.json submission.json --directory review-001 --key /private/review.key
P9_REVIEW_KEY=/private/review.key php admin.php accept review-001/receipt.json
```

Generate the shared private key once as 32 random bytes encoded in hexadecimal;
set its file mode to 0600. The master signs only after a fresh archive acquisition
and complete search replay, including applicable injections. Candidate omissions,
altered measurements, or differing outcomes cannot earn acceptance. A failed
replay creates no receipt. A disagreement requeues the task and suspends approval.
The receipt binds inputs, code, environment, task, submission, and checks actually
performed. It adds no sensitivity credit; population accounting remains a separate
master operation. Include replay cost when estimating volunteer throughput.

## Deployment and recovery

For v1, stop claims, make a private backup, deploy v2, then run `migrate`.
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
