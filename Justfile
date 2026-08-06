# datasette-litestream dev tasks
#
# Ports: Datasette on 8002, Vite dev server on 5180.
# Quick start:
#   just frontend        # build the admin UI once
#   just dev             # run Datasette, then open http://localhost:8002/-/litestream
# Live UI development (two terminals):
#   just frontend-dev    # terminal 1: Vite HMR
#   just dev-with-hmr    # terminal 2: Datasette pointed at the Vite dev server

ds_port := "8002"
vite_port := "5180"
litestream_version := "0.5.12"
vite_base := "http://localhost:" + vite_port + "/-/static-plugins/datasette_litestream/"

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

# Type-check the backend (ty)
check-backend:
  uv run ty check datasette_litestream

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
    curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v{{litestream_version}}/litestream-{{litestream_version}}-${os}-$(uname -m).tar.gz" \
      | tar -xz -C .bin litestream
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
    demo/demo.db \
    -c demo/datasette.yml \
    -p {{ds_port}} \
    -s permissions.litestream-view-status true \
    -s permissions.litestream-manage true \
    {{flags}}

# Run Datasette pointed at the Vite dev server for live admin-UI development
# with hot-module reload. Start `just frontend-dev` in another terminal first.
# Datasette auto-restarts on Python/HTML changes (needs watchexec).
dev-with-hmr *flags: demo-db
  DATASETTE_LITESTREAM_VITE_PATH={{vite_base}} \
  watchexec --stop-signal SIGKILL -e py,html --ignore '*.db' --restart --clear -- \
    just dev {{flags}}
