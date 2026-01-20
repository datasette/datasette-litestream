# datasette-litestream

[![PyPI](https://img.shields.io/pypi/v/datasette-litestream.svg)](https://pypi.org/project/datasette-litestream/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-litestream?include_prereleases&label=changelog)](https://github.com/datasette/datasette-litestream/releases)
[![Tests](https://github.com/datasette/datasette-litestream/workflows/Test/badge.svg)](https://github.com/datasette/datasette-litestream/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-litestream/blob/main/LICENSE)

A Datasette <-> Litestream plugin.

## Installation

The plugin requires a recent alpha version of Datasette 1.0:

    pip install 'datasette>=1.0a20'

Then install this plugin in the same environment as Datasette:

    datasette install datasette-litestream

## Usage

### Replicate a single database to S3

To replicate `my_database.db` to S3, use the following configuration in your `metadata.yaml` file:

```yaml
databases:
  my_database:
    plugins:
      datasette-litestream:
        replicas:
          - url: s3://my-bucket/my_database
```

Then make sure you export `LITESTREAM_ACCESS_KEY_ID` and `LITESTREAM_SECRET_ACCESS_KEY` with your S3 credentials (or `AWS_ACCESS_KEY_ID` `AWS_SECRET_ACCESS_KEY`), then run with:

```
datasette my_database.db -m metadata.yaml
```

### Replicate all databases

If you have multiple attached databases in Datasette and want to replicate all of them, the top-level `all-replicate` key can be used.

```yaml
plugins:
  datasette-litestream:
    all-replicate:
      - s3://my-bucket/$DB_NAME
```

When `all-replicate` is used, a new replica URL is generated for each attached database. In this case, if you had a database named `parking_tickets` and another named `city_budget`, then `datasette-litestream` will replicate them to `s3://my-bucket/parking_tickets` and `s3://my-bucket/city_budget`.

This is done with "variables" that `datasette-litestream` replaces in the `all-replicate` URL. The supported variables are:

- `$DB_NAME`: The name of the Datasette database to replicate.
- `$DB_DIRECTORY`: The full parent directory that the SQLite database resides.
- `$PWD`: The current working directory of the Datasette process.

## Config

Some configuration in the `metadata.yaml` will be used to auto-generate the [`litestream.yml`](https://litestream.io/reference/config/) file under the hood. You can use this to customize the Litestream replication process.

### Top-level

The following are valid keys that are allowed when specifying top-level plugin configuration:

- `all-replicate`: A template replica URL used to replicate all attached Datasette databases, see above for details.
- `metrics-addr`: Defines the [`addr:` Litestream option](https://litestream.io/reference/config/#metrics), which will expose a Prometheus endpoint at the given URL. Use with caution on public Datasette instances! When defined, the metrics info will appear on the `datasette-litestream` status page.
- `access-key-id`: An alternate way to provide a S3 access key (though the `LITESTREAM_ACCESS_KEY_ID` environment variable is preferred).
- `secret-access-key`: An alternate way to provide a S3 secret key (though the `LITESTREAM_SECRET_ACCESS_KEY` environment variable is preferred).
- `session-token`: Optional AWS session token for temporary credentials (e.g., when using AWS STS).
- `credentials-file`: Path to a JSON file containing credentials (see Dynamic Credentials below).
- `credentials-command`: A CLI command to execute that returns JSON credentials (see Dynamic Credentials below).
- `credentials-refresh-interval`: How often (in seconds) to check for credential changes. Required when using `credentials-file` or `credentials-command`.

None of these keys are required.

Example:

```yaml
plugins:
  datasette-litestream:
    all-replicate:
      - XXX
      - YYY
    metrics-addr: :5001
    access-key-id: $YOUR_KEY
    secret-access-key: $YOUR_SECRET
```

### Dynamic Credentials

For environments where credentials rotate or are fetched dynamically (e.g., from a secrets manager), you can configure `datasette-litestream` to read credentials from a file or execute a command, and periodically check for changes.

**Important:** You cannot specify both `credentials-file` and `credentials-command` - use one or the other.

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
    credentials-file: /path/to/credentials.json
    credentials-refresh-interval: 300  # Check every 5 minutes
```

#### Reading credentials from a command

You can also execute a CLI command that outputs JSON credentials. This is useful for integrating with secrets managers or credential vending services:

```yaml
plugins:
  datasette-litestream:
    credentials-command: ./fetch_creds.sh --bucket my-bucket
    credentials-refresh-interval: 300  # Check every 5 minutes
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
2. Every `credentials-refresh-interval` seconds, the file is re-read or the command is re-executed
3. If the credentials have changed, `datasette-litestream` will:
   - Stop the current litestream process
   - Update the configuration with new credentials
   - Start a new litestream process
4. If loading credentials fails during a refresh check, the Datasette process will exit with an error

### Database-level

The following options are allowed on database-level plugin configuration.

- `replicas`
- `monitor-interval`
- `checkpoint-interval`
- `min-checkpoint-page-count`
- `max-checkpoint-page-count`

```yaml
databases:
  my_database:
    plugins:
      datasette-litestream:
        replicas:
          - s3://...
          - file://...
        monitor-interval: XXX
        checkpoint-interval: XXX
        min-checkpoint-page-count: XXX
        max-checkpoint-page-count: XXX
```

See [Litestream Database settings](https://litestream.io/reference/config/#database-settings) for more information.

## Development

To set up this plugin locally, first checkout the code. Then run the tests using [uv](https://docs.astral.sh/uv/):
```bash
cd datasette-litestream
uv run pytest
```
To run Datasette with the plugin installed:
```bash
uv run datasette -c config.yaml
```