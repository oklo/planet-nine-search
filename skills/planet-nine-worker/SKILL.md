---
name: planet-nine-worker
description: Set up a Planet Nine worker for one field or continuous searching, diagnose interruptions, and resume ordinary participant work without coordinator privileges.
---

Use the participant's installed `planet-nine-search` environment. Install from the public repository's README if needed. Keep the same environment, private credential file, work directory and resource limits across sessions.

## Start

Check the host before installing. Use macOS or Linux and Python 3.11–3.13 with venv and pip. We recommend 32 GB RAM for headroom. A 16 GB machine can try one field. An 8 GB machine is experimental and some fields can exceed its memory limit. Check free space on the work volume: the example below allows 12 GiB for work and leaves 20 GiB free, plus room for the installed environment. Check for an existing worker. If Python is missing or unsupported, use an available user-level installer (for example, `uv python install 3.12`) or explain the specific prerequisite that needs the user's help.

From version 0.5.8 the worker defaults to two threads and a memory limit of 60% of physical RAM rounded down to whole GiB, from 1 to 12 GiB. On an 8, 16 or 32 GiB host this gives 4, 9 or 12 GiB respectively. On older installations, pass the corresponding `--max-memory-gb` value and `--threads 2` explicitly when starting a worker. Preserve resource limits the participant has chosen.

Install the public package in a dedicated environment following the README. Run one installer at a time. Verify with `python -m pip check` and `p9 worker --help` in that environment. Reuse a healthy existing setup. Before claiming, run `.venv/bin/python skills/planet-nine-worker/scripts/check_host.py ./work` using the intended work path and launch permissions. This checks access to the calibration cache and the process monitor used for memory limits. If the agent sandbox blocks these, request permission to run the worker on the host and repeat the check.

If the user has not chosen a mode, ask once: “Search one field, or keep searching until you tell me to stop?” For one field, run:

```sh
.venv/bin/p9 worker --directory ./work --credentials ~/.config/planet-nine/worker.json --max-disk-gb 12 --reserve-gb 20
```

Report a submission only after its receipt arrives. If no field is available, say so. The one-field command exits when the queue is empty.

Once the user has authorized continuous searching, start one persistent worker with the bundled helper. From the repository root, for example:

```sh
.venv/bin/python skills/planet-nine-worker/scripts/session.py start -- \
  --directory /path/to/work --credentials ~/.config/planet-nine/worker.json \
  --max-disk-gb 12 --reserve-gb 20
```

Use a real work path on the intended disk. Choose limits that leave room for the user's other work. Credentials belong on a private disk.

Starting the worker registers it and claims an available field automatically. Complete installation and start the worker; no approval request or GitHub issue is needed for a new participant. Verify its current task and fresh download or search progress before saying it is searching. Preserve the same credentials across restarts. After submitting a field and receiving acknowledgement, let the worker clean up and claim the next one. There is no daily quota or requirement to wait for scientific review.

An `approval_wait` means this identity has been held for review, rather than routine onboarding. Report that the worker is paused for review and preserve its process and credentials. Provide only its public `worker_id` when needed for support; never print its token or credential file. Do not register a replacement to evade the hold. Report actual installation or registration errors with the failed step.

The helper detaches `p9 worker --continuous` from the conversation and records its identity. The process continues between agent turns, while the computer is awake. It does not restart after a reboot or an unexplained crash. Calling `start` again without worker arguments reuses the saved settings. If another service already manages this worker, inspect that service instead of launching a duplicate. Different workers need separate credentials, directories and session paths.

## Monitor and recover

Use the same Python and helper path for `status` or `stop`. For a nondefault session directory, put `--session /private/path` before the action. The installed skill's helper is at `scripts/session.py` beside this file.

Check roughly every ten minutes during an active supervision session. Use the host's scheduling facility for later checks if available and authorized; ending an agent turn does not schedule another one. Avoid spending tokens on rapid polling. Record only task transitions, submissions and interventions. Keep routine polls and agent acknowledgements out of the user's conversation. Do not append every status response to a file; replace a compact current-state record. Report actionable failures and requested summaries. After escalating a blocker, keep monitoring at lower frequency and report changes. End supervision when the user asks or another active supervisor takes over; a final status report alone is not a handoff.

`status` reports the live process, current worker state, free space, and the current search child's progress and timestamp. Put `--details` before `status` to include the log tail; an exited worker includes it automatically. A running process alone is not evidence of progress. Read the named log and the current task's `worker.log` when progress stops. `p9 status` reports shared queue counts; zero active leases can mean no worker is currently searching.

- **Searching or downloading:** leave fresh progress running. Downloads and scoring can be quiet; inspect the child and its logs before declaring a stall.
- **Queue, transport or capacity wait:** the continuous worker retries with backoff. Preserve it. Check the relevant local disk or public queue. Ordinary pending reviews do not block new work. Report an unexpected review wait as a coordinator problem; do not create another identity.
- **Exited:** read the error, check that no search child remains, and fix the cause. After a confirmed transient interruption or a restored disk, run `start` again with the same paths and limits. Restart once, then verify a new claim, progress or submission. If it fails again, preserve the evidence and report the error. Do not loop through failed searches.
- **Integrity, provenance, repeated scientific failure or an unknown error:** hold for review. Do not change task specifications, thresholds, source checks or acceptance rules.

The helper refuses overlapping sessions and checks process identity before stopping. Use `stop` to release an active lease and stop its search child cleanly. Do not kill an unrelated process or erase lock files to force a restart.

The worker removes completed image arrays once compact submission evidence is ready, before acknowledgement. It preserves the exact unacknowledged result and evidence for retry. After acknowledgement it keeps 32 compressed receipts, capped at 256 MiB. Routine interrupted downloads are discarded for recomputation. Each search log and session log is capped at 1 MiB; one previous session log is retained. Leave `--keep-data` and `--keep-evidence` off for routine work. Preserve unacknowledged results and credentials; resubmission is automatic after a restart. Do not delete evidence to get past a disk limit or lower the user's reserve without permission.

Do not send failure emails. Keep bounded local diagnostics and report actionable failures in the conversation.

Report completed submissions separately from reviewed results and the search’s measured sensitivity. A participant cannot approve its own results, replenish the coordinator's queue, or infer expectation gained from its submission count. Report a sustained empty queue to the operator. The operator prepares new fields independently.
