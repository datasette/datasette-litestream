# datasette-litestream

[![PyPI](https://img.shields.io/pypi/v/datasette-litestream.svg)](https://pypi.org/project/datasette-litestream/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-litestream?include_prereleases&label=changelog)](https://github.com/datasette/datasette-litestream/releases)
[![Tests](https://github.com/datasette/datasette-litestream/workflows/Test/badge.svg)](https://github.com/datasette/datasette-litestream/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-litestream/blob/main/LICENSE)

A Datasette <-> Litestream plugin.

## Installation

The plugin requires a recent alpha version of Datasette 1.0, whcih can be installed with:

    pip install datasette==1.0a6

Then install this plugin in the same environment as Datasette.

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

- `all-replicate`: A template replica URL used to replicate all attached Datasette databases, see aboce for details.
- `metrics-addr`: Defines the [`addr:` Litestream option](https://litestream.io/reference/config/#metrics), which will expose a Prometheus endpoint at the given URL. Use which caution on public Datasette instances! When defined, the metrics info will appear on the `datasette-litestream` status page.
- `access-key-id`: An alternate way to provide a S3 access key (though the `LITESTREAM_ACCESS_KEY_ID` environment variable is preferred).
- `secret-access-key`: An alternate way to provide a S3 secret key (though the `LITESTREAM_SECRET_ACCESS_KEY` environment variable is preferred).

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

To set up this plugin locally, first checkout the code. Then create a new virtual environment:

    cd datasette-litestream
    python3 -m venv venv
    source venv/bin/activate

Now install the dependencies and test dependencies:

    pip install -e '.[test]'

To run the tests:

    pytest
