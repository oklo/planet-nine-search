# Coordinator

A PHP 8.1+ service with PDO SQLite. It needs no daemon, WordPress plugin, or
external database. SQLite must be on a local filesystem with reliable locking.

Copy this directory to a dedicated HTTPS path. Keep the database **outside the
web root**. The default is `~/.planet-nine/queue.sqlite` when installed at
`~/public_html/planet-nine`; other layouts must set `P9_DATABASE` in both the web
server and the administrative shell. Initialize once:

```sh
P9_DATABASE=/private/path/queue.sqlite php admin.php init
P9_DATABASE=/private/path/queue.sqlite php admin.php add field.json 10
P9_DATABASE=/private/path/queue.sqlite php admin.php results
```

`add` takes an optional priority; higher values are claimed first. Tasks are
immutable, content-addressed JSON. The Python worker validates their schema and
hash. Use `tools/make_task.py --help` to bundle an independently prepared geometry
plan and query Gaia. Geometry plans must be reviewed before queueing: the tool
packages them, rather than deriving footprints or selecting on image pixels.
Host the resulting ZIP at the declared HTTPS URL. Keep the JSON canonical;
`make_task.py` writes the required representation. An example is in `examples/`.

Keep a reserve for visitors by running `php admin.php reserve WORKER_ID 4` for
the operator's worker. It can claim new work only while more than four fields
are ready; other workers remain unrestricted. Claims check this atomically,
and existing leases continue normally. Set the count to zero to remove the limit.

For Apache, include `.htaccess` to disable directory listings, preserve bearer
authorization, and deny HTTP access to administrative files. For other servers,
set equivalent rules. `admin.php` additionally rejects non-CLI execution. Back
up SQLite with its online backup API, rather than copying a live WAL database.

## Protocol

All API calls use `api.php?action=...`. Only `status` uses GET; other actions
accept JSON POST. Registration accepts a worker label and a client-generated
32-byte hexadecimal token. All later calls require `Authorization: Bearer TOKEN`.
Only token hashes are stored. Credentials on workers have mode 0600.

| Action | Behavior |
|---|---|
| `register` | Idempotently register a token; ten new workers per IP per day. |
| `claim` | Atomically claim the highest-priority fitting task; one active claim per worker. |
| `heartbeat` | Renew the matching worker and lease token for 30 minutes. |
| `release` | Return unfinished work; quarantine after three reported failures. |
| `submit` | Save an unverified result; accept identical retries, reject conflicting results. |
| `status` | Report aggregate queue counts without exposing credentials. |

Expired claims become available on the next claim request. Stale workers cannot
renew, release, or submit after reassignment. Workers stop their search if lease
renewal fails beyond its deadline. Network failure during submission leaves the
exact result locally for retry. Independent review is required before crediting
results; registration is open, so submitted science is not trusted evidence.

Integration tests use a temporary SQLite database and four PHP worker processes.
Run `pytest tests/test_coordinator.py`; they skip when PHP is unavailable.

## Coverage page

`coordination/` is an optional, buildless sky map for oklo.org; it uses the site's
existing WordPress theme. It is unlinked and requests `noindex`. Publish all six
files, including `.htaccess`, together. `snapshot.json` contains reviewed field
states and sector WCS polygons; `prior.png` is the matching reference density.
Refresh these from the reviewed search ledger, never from unverified submissions.
The page labels its snapshot time and polls the aggregate queue once per minute.
