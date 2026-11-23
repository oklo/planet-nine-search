---
name: planet-nine-worker
description: Run a Planet Nine search worker in the background, check its progress, and recover interrupted sessions.
---

## Start

Install the package using the repository README. Finish installation before
starting a search. If the worker reports conflicting package versions, install
once in a clean environment and reuse the existing work directory and credentials.

From the checkout, start one worker per authorized session:

```sh
.venv/bin/python skills/planet-nine-worker/scripts/session.py start -- \
  --directory ./work --credentials ~/.config/planet-nine/worker.json \
  --max-disk-gb 12 --reserve-gb 20 --threads 4
```

The default search limits are 24 GiB of resident memory and four hours per
field. Choose limits that fit the host. Keep credentials on a private
disk. Each worker needs its own credentials, work directory and session path.
If a service manager already runs the worker, use that service.

The worker registers and claims fields automatically. The helper keeps it
running between agent turns while the machine is awake. After a reboot or
crash, `start` with no worker arguments reuses its saved settings.

## Check progress

Use the same Python and helper for `status` and `stop`. In an installed skill,
the helper is `scripts/session.py` beside this file. Put `--session PATH`
before the action if using a custom session directory.

During supervision, check about every ten minutes. Arrange later checks with
the host scheduler when authorized. Read the current task ID, child progress
and timestamp before reporting that a search is running. Use `--details status`
for the log tail. Report submissions, problems that need attention, and
requested updates. Keep routine polls quiet and diagnostics local.

| Worker state | Action |
| --- | --- |
| Searching or downloading | Check the current child and task log if progress appears to stop. Downloads and scoring can be quiet. |
| Waiting for work, disk or network | The worker retries. Check free space or run `p9 status`. Tell the operator if the queue stays empty. |
| `approval_wait` | Keep the process and credentials. Report the public worker ID to the operator. Never share the token or register a second identity to bypass review. |
| Exited | Read the error and check for a surviving search child. Once a transient cause is fixed, restart once and verify fresh progress. |
| Integrity error, repeated search failure or unknown cause | Preserve the evidence and report the failed step. Leave task parameters and acceptance thresholds intact. |

Use `stop` for a clean shutdown and lease release. After reporting a blocker,
check less frequently until it clears, supervision is handed over, or the user
asks to stop.

## Storage and results

Keep the default retention settings for ordinary searches. Once compact
evidence is ready, the worker removes completed image arrays and saves the
unacknowledged submission for retry. After acknowledgement it keeps up to 32
compressed receipts within 256 MiB. Each search log and session log is capped
at 1 MiB, with one previous session log. Use `--keep-data` or `--keep-evidence`
for diagnosis. Preserve pending submissions and the user's disk reserve.

Acknowledgement means the coordinator received the result. Review and
sensitivity accounting happen separately. A participant can report its
submissions and leaves field preparation and review to the operator.
