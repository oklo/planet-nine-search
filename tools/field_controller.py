"""Field supply loop for a single operator, with local or distributed workers."""

import fcntl
import time

# Keep a small supply available even while the reviewer is catching up.
READY_TARGET = 100
READY_RESERVE = 3
REVIEW_BACKLOG = 3

ADVANCING = {
    "geometry_added",
    "empty_plan",
    "route_added",
    "regions_added",
    "deferred",
    "published",
    "targeted_plan",
    "geometry_deferred",
}


def supply_cycle(site):
    """Advance at most one field or geometry plan per acquisition of the producer lock."""
    site.maintain()
    counts = site.counts()
    ready = counts.get("eligible_pending", counts["pending"])
    if ready >= READY_TARGET or (
        ready >= READY_RESERVE and counts.get("submitted", 0) >= REVIEW_BACKLOG
    ):
        return dict(mode="buffered", counts=counts)

    accepted, held = site.publication_state()
    if held or (ready >= READY_RESERVE and accepted >= REVIEW_BACKLOG):
        return dict(mode="waiting_for_publication", accepted=accepted)

    choice = site.choose()
    if choice is None:
        return site.expand()
    return site.prepare(*choice)


def standalone_cycle(site):
    site.check_markers()
    try:
        site.recover_worker()
    except Exception as error:
        site.record_worker_error(str(error)[-1500:])
    else:
        site.record_worker_error(None)
    return supply_cycle(site)


def distributed_cycle(site):
    """Run on the designated supply host. Each search node owns its worker."""
    site.check_markers()
    return supply_cycle(site)


def tick(cycle, lock_path, report):
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "producer_busy"
        try:
            result = cycle()
        except Exception as error:
            result = dict(mode="retry", error=f"{type(error).__name__}: {error}"[-1500:])
        report(result)
        return result["mode"]


def serve(cycle, lock_path, report):
    while True:
        mode = tick(cycle, lock_path, report)
        time.sleep(3 if mode in ADVANCING else 90)


def publication_failure(error, integrity_errors, previous, now):
    if isinstance(error, integrity_errors):
        return dict(state="held", checked_epoch=now, reason=str(error)[:400])
    attempts = previous.get("attempts", 0) + 1
    delay = min(600, 20 * 2 ** min(attempts - 1, 5))
    return dict(
        state="retry",
        checked_epoch=now,
        attempts=attempts,
        retry_after_epoch=now + delay,
        reason=str(error)[:400],
    )


def interrupted_reservations(entries, routes):
    """Protect nominal as well as actual sample IDs during a partial preparation."""
    reserved = set()
    incomplete = set()
    for entry in entries:
        if entry.get("state") in {"preparing", "metadata_complete"}:
            incomplete.add(int(entry["field"]))
            for key in ("nominal_reference_ids", "reference_ids"):
                reserved.update(map(int, entry.get(key, [])))
    if incomplete:
        for rows in routes():
            for row in rows:
                if int(row["field"]) in incomplete:
                    reserved.update(map(int, row.get("new_reference_ids", [])))
    return reserved
