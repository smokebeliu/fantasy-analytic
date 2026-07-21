# AGENTS.md

## Cursor Cloud specific instructions

This VM is provisioned for the full pipeline: PostgreSQL 16 plus SQLAlchemy 2,
Alembic, psycopg 3, and a FastAPI/uvicorn admin control plane. The project is a
CLI + API toolkit (no browser UI): discovery, ingestion, quality gate, feature
builder and a manual-ingestion FastAPI app. See `README.md` for the full command
reference — the notes below only cover non-obvious cloud caveats.

### PostgreSQL (local, not Docker)

- Docker is **not** installed here. The `compose.yaml` / `Dockerfile` path
  (which uses `postgres:18-alpine`) is a separate flow — ignore it in this VM.
- A native **PostgreSQL 16** cluster (`main`, port 5432) is installed via the
  Ubuntu package and its data dir persists in the VM snapshot.
- It is auto-started by a managed block in `~/.bashrc` on each new interactive
  shell. If you ever hit "connection refused" from a non-interactive context,
  start it manually: `sudo pg_ctlcluster 16 main start` (check with
  `pg_isready -h localhost -p 5432`).
- Role `fantasy` / password `fantasy` owns databases `fantasy` and
  `fantasy_test`.

### Connection env vars

`~/.bashrc` exports these (SQLAlchemy 2 driver form, consumed by
`create_engine`):

- `DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy`
- `TEST_DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy_test`

Note the `+psycopg` prefix is SQLAlchemy-specific: plain libpq tools cannot parse
these URLs. To reach the DB with `psql`, use
`PGPASSWORD=fantasy psql -h localhost -U fantasy -d fantasy_test`. Integration
tests should target `TEST_DATABASE_URL` (safe to drop/recreate `public`).

### Python dependencies

- Python 3.12 system interpreter. Runtime deps (SQLAlchemy 2, Alembic, psycopg 3
 binary, FastAPI, uvicorn) plus test deps (httpx, pytest) are installed into the
 system Python via `pip --break-system-packages` and refreshed by the cloud
 update script; there is no virtualenv.
- The `alembic` CLI lives in `~/.local/bin` (added to `PATH` by `~/.bashrc`).
- The project package is imported via `PYTHONPATH=src`; it is not pip-installed,
 so the `fantasy-*` console scripts from `pyproject.toml` are not on `PATH` —
 invoke modules directly, e.g. `PYTHONPATH=src python3 -m fantasy_analytics.api`.

### Lint / test / run

- There is no configured linter; use `PYTHONPATH=src python3 -m compileall src`
 as a syntax check.
- Full test suite (unit + DB integration): `PYTHONPATH=src python3 -m unittest
 discover -s tests -v`. Integration tests auto-skip unless `TEST_DATABASE_URL`
 is set (it is exported by `~/.bashrc`); one live-network test stays skipped
 unless `RUN_LIVE_MATCH_STATS=1`.
- Ensure the schema exists before running DB tools/API:
 `PYTHONPATH=src python3 -m fantasy_analytics.db.cli upgrade`.
- The admin API is `PYTHONPATH=src python3 -m fantasy_analytics.api --host
 127.0.0.1 --port 8000`. `POST /admin/ingestion/rpl/refresh` returns `202` and a
 job id immediately, then a detached worker subprocess runs the import + quality
 gate; poll `GET /admin/ingestion/runs/{id}` until `succeeded`. A full RPL season
 import takes ~45s and needs outbound access to the live Sports.ru GraphQL API.
- The discovery/ingest/match-stats CLIs also call the live Sports.ru GraphQL API
 and need outbound network, e.g.
 `PYTHONPATH=src python3 -m fantasy_analytics --output data/discovery`.
