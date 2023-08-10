# datasette-litestream

[![PyPI](https://img.shields.io/pypi/v/datasette-litestream.svg)](https://pypi.org/project/datasette-litestream/)
[![Changelog](https://img.shields.io/github/v/release/datasette-io/datasette-litestream?include_prereleases&label=changelog)](https://github.com/datasette-io/datasette-litestream/releases)
[![Tests](https://github.com/datasette-io/datasette-litestream/workflows/Test/badge.svg)](https://github.com/asg017/datasette-litestream/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette-io/datasette-litestream/blob/main/LICENSE)

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

## Development

To set up this plugin locally, first checkout the code. Then create a new virtual environment:

    cd datasette-litestream
    python3 -m venv venv
    source venv/bin/activate

Now install the dependencies and test dependencies:

    pip install -e '.[test]'

To run the tests:

    pytest
