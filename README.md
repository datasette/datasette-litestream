# datasette-litestream

[![PyPI](https://img.shields.io/pypi/v/datasette-litestream.svg)](https://pypi.org/project/datasette-litestream/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-litestream?include_prereleases&label=changelog)](https://github.com/datasette/datasette-litestream/releases)
[![Tests](https://github.com/datasette/datasette-litestream/workflows/Test/badge.svg)](https://github.com/datasette/datasette-litestream/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-litestream/blob/main/LICENSE)

A Datasette <-> Litestream plugin.

This version targets **Litestream 0.5.x**. It runs a single long-lived
`litestream replicate` daemon with its [control socket](https://litestream.io/)
enabled, and registers/unregisters databases with that daemon at runtime — so
databases can be added to or removed from replication without restarting
Litestream. (Litestream 0.5 replicates each database to exactly one
destination; the older multi-replica `replicas:` lists are no longer supported.)

## Installation

The plugin requires a recent alpha version of Datasette 1.0:

    pip install 'datasette>=1.0a20'

Then install this plugin in the same environment as Datasette:

    datasette install datasette-litestream

## Usage

### Replicate a single database to S3

To replicate `my_database.db` to S3, use the following configuration in your `datasette.yml` file:

```yaml
databases:
  my_database:
    plugins:
      datasette-litestream:
        replica: s3://my-bucket/my_database
```

Then make sure you export `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` with your S3 credentials, then run with:

```
datasette my_database.db -c datasette.yml
```

### Replicate all databases

If you have multiple attached databases in Datasette and want to replicate all of them, the top-level `all-replicate` key can be used.

```yaml
plugins:
  datasette-litestream:
    all-replicate: s3://my-bucket/$DB_NAME
```

When `all-replicate` is used, a new replica URL is generated for each attached database. In this case, if you had a database named `parking_tickets` and another named `city_budget`, then `datasette-litestream` will replicate them to `s3://my-bucket/parking_tickets` and `s3://my-bucket/city_budget`.

This is done with "variables" that `datasette-litestream` replaces in the `all-replicate` URL. The supported variables are:

- `$DB_NAME`: The name of the Datasette database to replicate.
- `$DB_DIRECTORY`: The full parent directory that the SQLite database resides.
- `$PWD`: The current working directory of the Datasette process.

Databases attached as immutable (`datasette -i data.db`) are never replicated:
Litestream opens databases read-write and switches them to WAL journal mode,
which would modify a file Datasette promises never to change. `all-replicate`
skips immutable databases with a startup warning, and configuring a `replica`
directly on an immutable database fails startup with an error.

## Config

Plugin configuration lives in your `datasette.yml` (passed with `-c`, or via `-s` for individual settings). The plugin generates a minimal Litestream daemon config (control socket plus the optional metrics address) and registers databases with the daemon at runtime.

### Top-level

The following are valid keys that are allowed when specifying top-level plugin configuration:

- `all-replicate`: A template replica URL used to replicate all attached Datasette databases, see above for details. (A list is accepted for backwards compatibility, but only the first entry is used.)
- `replicate-internal`: Also replicate Datasette's internal database. Set to `true` to derive the replica URL from the `all-replicate` template (using `_internal` as the database name), or to a template replica URL to use directly. Requires running Datasette with `--internal /path/to/internal.db` — without that the internal database is an ephemeral temp file, and a warning is printed to the console and shown on the admin page instead. The same warning is emitted for any attached in-memory database the configuration would otherwise replicate.
- `metrics-addr`: Defines the [`addr:` Litestream option](https://litestream.io/reference/config/#metrics), which will expose a Prometheus endpoint at the given address. This endpoint is **unauthenticated** and also serves Go's `/debug/pprof` handlers, and an address without a host part (like `:9090`) listens on **all interfaces** — bind it to loopback (`127.0.0.1:9090`) or firewall it in production. The plugin prints a startup warning (also shown on the admin page) for all-interfaces binds.
- `restrict-runtime-replicas`: When `true`, the runtime register API only accepts the replica URL derived from configuration (a database-level `replica` or the `all-replicate` template) — caller-supplied URLs that differ are rejected. Defaults to `false`. See the permissions note under [Admin UI](#admin-ui).
- `logging`: Controls the Litestream daemon's logging. Litestream's log output is always captured to a log file (shown on the admin page) instead of being interleaved with Datasette's console output. Sub-keys:
  - `logging.level`: One of `debug`, `info`, `warn`, `error`. Defaults to `info`.
  - `logging.type`: Log format, `text` or `json`. Defaults to `text`.
  - `logging.path`: Write logs to this file (opened in append mode) instead of a session-scoped temporary file. Useful for long-lived deployments where you want the logs somewhere durable (and rotatable).
- `credentials`: S3 credentials, either static keys or a dynamic source (see [Dynamic Credentials](#dynamic-credentials) below). Static keys and a dynamic source cannot be combined. Sub-keys:
  - `credentials.access-key-id`: An alternate way to provide a S3 access key (though the `AWS_ACCESS_KEY_ID` environment variable is preferred).
  - `credentials.secret-access-key`: An alternate way to provide a S3 secret key (though the `AWS_SECRET_ACCESS_KEY` environment variable is preferred).
  - `credentials.session-token`: Optional AWS session token for temporary credentials (e.g., when using AWS STS).
  - `credentials.file`: Path to a JSON file containing credentials.
  - `credentials.command`: A CLI command to execute that returns JSON credentials. Cannot be combined with `credentials.file`.
  - `credentials.refresh-interval`: How often (in seconds) to check for credential changes. Required when using `credentials.file` or `credentials.command` (and only valid alongside one of them).

None of these keys are required.

Earlier versions of this plugin accepted the credential keys as flat top-level keys (`access-key-id`, `credentials-file`, `credentials-refresh-interval`, etc.); they now live inside the `credentials` block and the flat spellings are rejected at startup.

Configuration is validated at startup: an unrecognized key (or an invalid value) raises an error instead of being silently ignored, so typos surface immediately.

Example:

```yaml
plugins:
  datasette-litestream:
    all-replicate: s3://my-bucket/$DB_NAME
    metrics-addr: 127.0.0.1:5001
    credentials:
      access-key-id: $YOUR_KEY
      secret-access-key: $YOUR_SECRET
    logging:
      level: warn
      path: /var/log/litestream.log
```

Individual keys can also be set from the command line with `-s`, for example:

```bash
datasette . -s plugins.datasette-litestream.logging.level warn
```

### Dynamic Credentials

For environments where credentials rotate or are fetched dynamically (e.g., from a secrets manager), you can configure `datasette-litestream` to read credentials from a file or execute a command, and periodically check for changes.

**Important:** You cannot specify both `credentials.file` and `credentials.command` - use one or the other. A dynamic source also cannot be combined with the static keys (`access-key-id` etc.) — the file or command provides them.

#### Reading credentials from a file

Create a JSON file with your credentials:

```json
{
  "access-key-id": "AKIAIOSFODNN7EXAMPLE",
  "secret-access-key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  "session-token": "optional-session-token-for-temporary-credentials"
}
```

The `session-token` field is optional and only needed when using temporary AWS credentials (e.g., from AWS STS).

Then configure the plugin to read from this file:

```yaml
plugins:
  datasette-litestream:
    credentials:
      file: /path/to/credentials.json
      refresh-interval: 300  # Check every 5 minutes
```

#### Reading credentials from a command

You can also execute a CLI command that outputs JSON credentials. This is useful for integrating with secrets managers or credential vending services:

```yaml
plugins:
  datasette-litestream:
    credentials:
      command: ./fetch_creds.sh --bucket my-bucket
      refresh-interval: 300  # Check every 5 minutes
```

The command should output JSON to stdout in the same format:

```json
{
  "access-key-id": "AKIAIOSFODNN7EXAMPLE",
  "secret-access-key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  "session-token": "optional-session-token"
}
```

The `session-token` field is optional.

#### How credential refresh works

1. On startup, credentials are loaded from the file or command
2. Every `refresh-interval` seconds, the file is re-read or the command is re-executed
3. If the credentials have changed, `datasette-litestream` will:
   - Stop the current litestream daemon
   - Restart it with the new credentials in its environment
   - Re-register every database that was being replicated
4. If loading credentials fails during a refresh check, the Datasette process will exit with an error

Credentials are passed to Litestream through the daemon's environment (as
`AWS_*` variables) rather than written into the generated config file.

### Database-level

The following option is allowed on database-level plugin configuration.

- `replica`: the single replica URL for this database (e.g. `s3://...` or `file://...`).

```yaml
databases:
  my_database:
    plugins:
      datasette-litestream:
        replica: s3://my-bucket/my_database
```

> **Note:** A deprecated `replicas:` list is still accepted for backwards
> compatibility, but only its first entry is used, since Litestream 0.5
> replicates each database to a single destination. The per-database tuning
> options from the 0.3.x plugin (`monitor-interval`, `checkpoint-interval`,
> `min-checkpoint-page-count`, `max-checkpoint-page-count`) are not currently
> exposed when registering databases over the control socket.

### Litestream binary

The plugin resolves the `litestream` binary to run, in this order:

1. The `DATASETTE_LITESTREAM_BINARY` environment variable, if set.
2. A binary bundled inside the installed wheel (`datasette_litestream/bin/litestream`), when present.
3. `litestream` found on `PATH`.

A Litestream **0.5.x** binary is required (the plugin depends on its control
socket).

## Adding and removing databases at runtime

Because the Litestream daemon stays running with its control socket enabled, you
can add or remove databases from replication without restarting it. Two routes
are provided, both gated behind the `litestream-manage` permission:

- `POST /-/litestream/register` — body `{"database": "<name>", "replica": "<url>"}`.
  Registers an attached Datasette database with Litestream. If `replica` is
  omitted, the URL is resolved from the database's `replica` config or the
  top-level `all-replicate` template.
- `POST /-/litestream/unregister` — body `{"database": "<name>", "timeout": <seconds>}`.
  Removes a database from replication. Litestream performs a final sync to the
  replica before dropping it.

For example, with a [Datasette API token](https://docs.datasette.io/en/latest/authentication.html#api-tokens):

```bash
curl -X POST http://localhost:8001/-/litestream/register \
  -H "Authorization: Bearer dstok_..." \
  -H "Content-Type: application/json" \
  -d '{"database": "my_database", "replica": "s3://my-bucket/my_database"}'
```

Immutable databases cannot be registered: the register route responds with a
400 error and the admin UI does not offer them (see the note under
[Replicate all databases](#replicate-all-databases)).

The current set of replicating databases, along with daemon version and uptime,
is also shown on the admin UI described below.

## Admin UI

The plugin ships a small Svelte/TypeScript admin interface at **`/-/litestream`**
(also linked from the Datasette menu). It shows the running daemon (version, PID,
uptime), a live table of replicating databases (status, replica destination, and
last sync time, refreshed by polling), and — for users who can manage — controls
to sync, stop, start, remove, and register databases on the fly.

Two permissions gate it:

- `litestream-view-status` — view the admin page and read replication status.
- `litestream-manage` — add/remove databases and run sync/start/stop actions.

Grant `litestream-manage` carefully: registering a database with a replica URL
of the caller's choosing amounts to **read access to every attached database**
(replicate it to a destination the caller controls, e.g. their own S3 bucket)
and, via `file://` replicas, **write access to any server path the Datasette
process can write to** (litestream creates its replica directory tree under
the given path). Replica URLs supplied to the register API are validated
against the schemes litestream supports, and setting the top-level
`restrict-runtime-replicas: true` option limits runtime registration to
operator-configured destinations only.

A user with only `litestream-view-status` sees the dashboard in read-only mode
(no management controls). Grant these with Datasette's standard
[permissions/allow blocks](https://docs.datasette.io/en/latest/authentication.html),
e.g.:

```yaml
permissions:
  litestream-view-status:
    id: "*"
  litestream-manage:
    id: admin
```

## Development

The backend tests use [uv](https://docs.astral.sh/uv/) and need a Litestream
0.5.x binary on `PATH` (or pointed to via `LITESTREAM_TEST_BINARY`):

```bash
cd datasette-litestream
uv run pytest
```

An opt-in end-to-end test of [dynamic credential rotation](#dynamic-credentials)
runs against a local [versitygw](https://github.com/versity/versitygw) S3
gateway — real server-side credential enforcement, no cloud account needed. It
replicates with one user's credentials, deletes that user and verifies
replication fails, then rotates the credentials file and verifies the refresh
loop restarts litestream and the replica catches up. It needs a `versitygw`
binary on `PATH` alongside litestream:

```bash
just test-versitygw
```

The admin UI lives in `frontend/` (Svelte 5 + TypeScript + Vite). Build it into
the Python package (writes `datasette_litestream/manifest.json` and
`datasette_litestream/static/gen/`) before running Datasette or the backend
tests that exercise the page:

```bash
just frontend          # npm install + vite build
just test-frontend     # vitest unit tests
```

To try the plugin against a generated demo database (creates `demo/demo.db`,
grants both litestream permissions, and replicates to `demo/backups/`), run:

```bash
just dev               # then open http://localhost:8002/-/litestream
```

For live UI development with hot-module reload, run the Vite dev server and a
Datasette that loads modules from it (auto-restarts on Python/HTML changes):

```bash
just frontend-dev      # Vite dev server on :5180 (terminal 1)
just dev-with-hmr      # Datasette pointed at the Vite dev server (terminal 2)
```