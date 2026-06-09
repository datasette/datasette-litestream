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
vite_base := "http://localhost:" + vite_port + "/-/static-plugins/datasette_litestream/"

# Build the Svelte/Vite admin UI into the Python package
frontend *flags:
  npm install --prefix frontend
  npm run build --prefix frontend {{flags}}

# Vite dev server (HMR) for the admin UI
frontend-dev *flags:
  npm run dev --prefix frontend -- --port {{vite_port}} {{flags}}

# Type-check the frontend
check-frontend:
  npm run check --prefix frontend

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
    python3 - <<'PY'
import sqlite3
db = sqlite3.connect("demo/demo.db")
db.execute("create table if not exists events(id integer primary key, name text)")
db.executemany("insert into events(name) values (?)", [(f"event {i}",) for i in range(25)])
db.commit(); db.close()
print("created demo/demo.db")
PY
  fi

# Run Datasette with the plugin against the demo database. Grants both
# litestream permissions to everyone so the admin UI is usable, then visit
# http://localhost:8002/-/litestream
dev *flags: demo-db
  DATASETTE_SECRET=abc123 uv run datasette \
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
