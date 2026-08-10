# Changelog

## Unreleased (0.3a0)

This release ports the plugin from Litestream 0.3.x to **Litestream 0.5**,
replacing the one-process-per-config model with a single long-running daemon
managed over its control socket. Databases are now registered with the daemon
at runtime, which enables a new admin UI and a management API. Most
configuration keys have been renamed or restructured — existing configs will
need updating (startup errors include migration hints).

### Breaking changes

- A Litestream **0.5.x** binary is now required (the plugin depends on its
  control socket). The binary is resolved from `DATASETTE_LITESTREAM_BINARY`,
  a copy bundled in the wheel, or `PATH`.
- The top-level `all-replicate` key (a list) is now `replica-url-template`, a
  single URL template string. Lists are rejected at startup: Litestream 0.5
  replicates each database to exactly one destination.
- The database-level `replicas:` list is now a single `replica:` URL. The
  per-database tuning options (`monitor-interval`, `checkpoint-interval`,
  `min-checkpoint-page-count`, `max-checkpoint-page-count`) are no longer
  exposed.
- Credential keys moved into a `credentials:` block:
  `access-key-id`, `secret-access-key`, `session-token`, plus the former
  `credentials-file` / `credentials-command` / `credentials-refresh-interval`
  as `credentials.file` / `credentials.command` / `credentials.refresh-interval`.
  The old flat spellings are rejected at startup.
- Plugin configuration is now validated with typed (pydantic) models at
  startup: unknown keys or invalid values raise an error instead of being
  silently ignored. Static credentials cannot be combined with a dynamic
  file/command source, and `refresh-interval` is required with (and only valid
  alongside) a dynamic source.
- The legacy `/-/litestream-status` page is gone, replaced by the
  `/-/litestream` admin UI (see below), and the `prometheus-client` dependency
  was dropped. Litestream's own Prometheus endpoint is still available via
  `metrics-addr`.
- Databases attached as immutable are never replicated: a `replica` configured
  directly on an immutable database fails startup, `replica-url-template`
  skips it with a warning, and the register API rejects it. (Litestream would
  otherwise flip a file Datasette promises never to modify into WAL mode.)
- Credentials are passed to the daemon as `AWS_*` environment variables rather
  than written into the generated Litestream config file.

### Added

- **Admin UI** at `/-/litestream` (Svelte 5 + TypeScript, also linked from the
  Datasette menu): daemon version/PID/uptime, a polling table of replicating
  databases with status, replica destination and last-sync time, a per-database
  details dialog (including a copyable `litestream restore` command), and
  controls to sync, stop, start, register and unregister databases.
- Two new permissions gate the UI and API: `litestream-view-status`
  (read-only dashboard and status API) and `litestream-manage` (all mutating
  actions). The README documents why `litestream-manage` should be granted
  carefully — it amounts to read access to every attached database and, via
  `file://` replicas, write access to server paths.
- **Runtime management API** (all gated behind `litestream-manage`):
  - `POST /-/litestream/register` — start replicating an attached database
    without restarting; the replica URL comes from the body or is resolved
    from configuration.
  - `POST /-/litestream/unregister` — stop replicating (the daemon performs a
    final sync first; optional `timeout` in seconds).
  - `POST /-/litestream/api/sync|start|stop` — per-database daemon actions.
  - `GET /-/litestream/api/status` — the JSON payload behind the admin UI
    (needs `litestream-view-status`).
- Replica URLs supplied to the register API are validated against the schemes
  Litestream supports, and the new top-level `restrict-runtime-replicas: true`
  option limits runtime registration to operator-configured destinations only.
- `internal-replica-url`: replicate Datasette's internal database (requires
  `--internal /path/to/internal.db`). The internal database is addressed in
  the API with `{"internal": true}` rather than a name, so it can never be
  confused with an attached database that happens to be called `_internal`.
- `logging:` block for the daemon (`level`, `type`, `path`). Litestream's log
  output is always captured to a file (created `0600`, shown on the admin
  page) instead of interleaving with Datasette's console output.
- Startup warnings — printed to the console and surfaced on the admin page —
  for in-memory or immutable databases the configuration would otherwise
  replicate, for db-level blocks with no resolvable replica URL, and for
  `metrics-addr` binds on all interfaces (the endpoint is unauthenticated and
  also serves Go's `/debug/pprof`).
- Databases registered at runtime and later detached from Datasette can still
  be synced and unregistered under their last-known name.

### Fixed / hardened

- Credential-refresh failures no longer kill the Datasette process: the daemon
  keeps replicating with the last-known-good credentials and the loop retries
  on the next tick (also restarting a daemon left down by an earlier failed
  restart).
- Daemon restarts (credential rotation) and runtime register/unregister are
  serialized with a lock, so a rotation can no longer race the manage API and
  silently drop databases from replication; every registered database is
  re-registered after a restart.
- The Litestream daemon is terminated gracefully on Datasette shutdown, and
  the child process and temporary files are cleaned up when startup fails
  partway.
- API routes return clean JSON `5xx` responses when the daemon is unavailable
  instead of raising, and fractional control-socket timeouts are rounded up
  instead of truncated to zero.
- The litestream process object is only constructed when the plugin is
  actually configured to run.

### Development

- The admin UI lives in `frontend/` (Vite + Svelte 5 + vitest); `just
  frontend` builds it into the Python package, and the API surface is captured
  in a typed contract (`datasette_litestream/contract.py` →
  `frontend/api.d.ts`) enforced by tests.
- New tooling: `ruff` linting/formatting, `ty` type checking, a tracked
  `uv.lock`, and a pinned, checksum-verified Litestream binary for local dev;
  publishing is gated on the test suite.
- An opt-in end-to-end credential-rotation test runs against a local
  [versitygw](https://github.com/versity/versitygw) S3 gateway
  (`just test-versitygw`).
- New runtime dependencies: `datasette-plugin-router`, `datasette-vite`,
  `httpx`; `prometheus-client` removed.
