# datasette-litestream

[![PyPI](https://img.shields.io/pypi/v/datasette-litestream.svg)](https://pypi.org/project/datasette-litestream/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-litestream?include_prereleases&label=changelog)](https://github.com/datasette/datasette-litestream/releases)
[![Tests](https://github.com/datasette/datasette-litestream/workflows/Test/badge.svg)](https://github.com/datasette/datasette-litestream/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-litestream/blob/main/LICENSE)

An experimental Datasette <-> Litestream plugin.

## Installation

Install this plugin in the same environment as Datasette.

    datasette install datasette-litestream

## Usage

```yaml
plugins:
  datasette-litestream:
    metrics-addr: :9090
    all-replicate:
      - file://$PWD/$DB-backup
```

### Replicate a single database to a local directory

```yaml
databases:
  my_database:
    plugins:
      datasette-litestream:
        replicas:
          - path: ./my_database-backup
```

### Replicate a single database to S3

```yaml
databases:
  my_database:
    plugins:
      datasette-litestream:
        replicas:
          - url: s3://my-bucket/my_database
```

1. Environment variables `LITESTREAM_ACCESS_KEY_ID` and `LITESTREAM_SECRET_ACCESS_KEY`. `AWS_ACCESS_KEY_ID` `AWS_SECRET_ACCESS_KEY`
2. Environment variables with `access-key-id` and `secret-access-key` config options.
3. Raw values in `secret-access-key` and `secret-access-key` config options.

### Replicate all databases

```yaml
plugins:
  datasette-litestream:
    all-replicate:
      - s3://my-bucket/$DB
```

## Config

### Top-level

The following are valid keys that are allowed when specifying top-evel

- `all-replicate`
- `access-key-id`
- `secret-access-key`
- `metrics-addr`

None of these keys are required.

Example:

```yaml
plugins:
  datasette-litestream:
    all-replicate:
      - XXX
      - YYY
    access-key-id: $YOUR_KEY
    secret-access-key: $YOUR_SECRET
    metrics-addr: :5001
```

### Database-level

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
          - XXX
          - XXX
        monitor-interval: XXX
        checkpoint-interval: XXX
        min-checkpoint-page-count: XXX
        max-checkpoint-page-count: XXX
```

## Development

To set up this plugin locally, first checkout the code. Then create a new virtual environment:

    cd datasette-litestream
    python3 -m venv venv
    source venv/bin/activate

Now install the dependencies and test dependencies:

    pip install -e '.[test]'

To run the tests:

    pytest
