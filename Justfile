# datasette-litestream dev tasks
#
# Ports: Datasette on 8002, Vite dev server on 5180.
# Quick start:
#   just frontend        # build the admin UI once
#   just dev             # run Datasette, then open http://localhost:8002/-/litestream
# Live UI development (two terminals):
#   just frontend-dev    # terminal 1: Vite HMR
#   just dev-with-hmr    # terminal 2: Datasette pointed at the Vite dev server

# Expose recipe arguments as "$@" so values like the all-replicate template
# (file:///...$DB_NAME) survive dev-with-hmr's watchexec/just layers without
# re-quoting.
set positional-arguments

ds_port := "8002"
vite_port := "5180"
litestream_version := "0.5.12"

# Build the Svelte/Vite admin UI into the Python package
frontend *flags:
  npm install --prefix frontend
  npm run build --prefix frontend {{flags}}

# Vite dev server (HMR) for the admin UI
frontend-dev *flags:
  npm run dev --prefix frontend -- --port {{vite_port}} {{flags}}

# Regenerate frontend/api.d.ts from the Python router's OpenAPI document.
# Run after route signature changes.
types-routes:
  #!/usr/bin/env bash
  set -euo pipefail
  tmp=$(mktemp)
  trap "rm -f $tmp" EXIT
  uv run python -c \
      'from datasette_litestream.router import router; import datasette_litestream.routes; import json; print(json.dumps(router.openapi_document_json()))' \
      > "$tmp"
  # --default-non-nullable=false: pydantic gives every Optional field a
  # default of null, and some endpoints omit those keys entirely (e.g. the
  # not-running status is just {"running": false}), so defaulted fields must
  # stay optional in the generated types.
  npx --prefix frontend openapi-typescript "$tmp" --default-non-nullable=false > frontend/api.d.ts

# Sync every contract artifact after changing contract.py or route
# signatures: the OpenAPI snapshot that tests compare against, and the
# generated frontend/api.d.ts. Commit both together.
contract-sync: types-routes
  uv run python -c \
      'from datasette_litestream.router import router; import datasette_litestream.routes; import json; print(json.dumps(router.openapi_document_json(), indent=2))' \
      > tests/openapi-snapshot.json

# Format Python code (ruff)
format-backend *flags:
  uv run ruff format datasette_litestream tests {{flags}}

# Lint Python code (ruff)
lint-backend *flags:
  uv run ruff check datasette_litestream tests {{flags}}

# Lint + type-check the backend (ruff, ty)
check-backend:
  uv run ruff check datasette_litestream tests
  uv run ruff format --check datasette_litestream tests
  uv run ty check datasette_litestream tests

# Type-check the frontend
check-frontend:
  npm run check --prefix frontend

# Type-check everything
check: check-backend check-frontend

# Frontend unit tests (vitest)
test-frontend:
  npm run test --prefix frontend

# Backend tests (pytest)
test:
  uv run pytest

# Build the frontend, then run the backend tests
test-all: frontend test

# Credential-rotation integration test against a local versitygw S3 gateway.
# Opt-in (not part of `just test`); needs versitygw + litestream >= 0.5 on PATH.
test-versitygw *flags:
  VERSITYGW_TESTS=1 uv run pytest tests/test_versitygw_credentials.py {{flags}}

# Create a small demo database to replicate (idempotent)
demo-db:
  #!/usr/bin/env bash
  set -euo pipefail
  mkdir -p demo
  if [ ! -f demo/demo.db ]; then
    sqlite3 demo/demo.db \
      "create table events(id integer primary key, name text);
       with recursive n(i) as (select 0 union all select i+1 from n where i < 24)
       insert into events(name) select 'event ' || i from n;"
    echo "created demo/demo.db"
  fi

# Download a pinned litestream binary into .bin/ (idempotent)
litestream-bin:
  #!/usr/bin/env bash
  set -euo pipefail
  if [ ! -x .bin/litestream ]; then
    mkdir -p .bin
    os=$(uname -s | tr '[:upper:]' '[:lower:]')
    asset="litestream-{{litestream_version}}-${os}-$(uname -m).tar.gz"
    curl -fsSLO "https://github.com/benbjohnson/litestream/releases/download/v{{litestream_version}}/${asset}"
    echo "$(./download.sh sha256-for "${asset}")  ${asset}" | shasum -a 256 -c -
    tar -xzf "${asset}" -C .bin litestream
    rm "${asset}"
    echo "downloaded litestream {{litestream_version}} to .bin/litestream"
  fi

# Run Datasette with the plugin against the demo database. Grants both
# litestream permissions to everyone so the admin UI is usable, then visit
# http://localhost:8002/-/litestream
dev *flags: demo-db litestream-bin
  DATASETTE_SECRET=abc123 \
  DATASETTE_LITESTREAM_BINARY={{justfile_directory()}}/.bin/litestream \
  uv run datasette \
    --root \
    -p {{ds_port}} \
    -s permissions.litestream-view-status true \
    -s permissions.litestream-manage true \
    "$@"

# Run Datasette pointed at the Vite dev server for live admin-UI development
# with hot-module reload. Start `just frontend-dev` in another terminal first.
# Datasette auto-restarts on Python/HTML changes (needs watchexec).
dev-with-hmr *flags: demo-db
  watchexec --stop-signal SIGKILL -e py,html --ignore '*.db' --restart --clear --shell=none -- \
    just dev -s plugins.datasette-vite.dev_ports.datasette_litestream {{vite_port}} "$@"
