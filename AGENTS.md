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
 binary, FastAPI, uvicorn, pydantic, and ortools for the CP-SAT squad optimizer)
 plus test deps (httpx, pytest) are installed into the system Python via
 `pip --break-system-packages` and refreshed by the cloud update script; there is
 no virtualenv. `ortools` (declared `>=9.10` in `pyproject.toml`) is required by
 `fantasy_analytics.optimizer` / the `fantasy-optimize` CLI.
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
 unless `RUN_LIVE_MATCH_STATS=1`. Some test modules import sibling test modules
 (e.g. `test_optimizer` imports `FakeClient` from `test_ingestion`), so run a
 single module via discovery too: `PYTHONPATH=src python3 -m unittest discover -s
 tests -p test_optimizer.py -v`, not `-m tests.test_optimizer`.
- Ensure the schema exists before running DB tools/API:
  `PYTHONPATH=src python3 -m fantasy_analytics.db.cli upgrade`.
- Backtesting needs **no** network, only an imported season:
  `PYTHONPATH=src python3 -m fantasy_analytics.backtest_cli --output data/backtest`
  (~50s for a full 30-tour season; add `--no-optimize` or `--tour <id>` while
  iterating). It exits `3` when its leakage audit finds a violation.
- Frontend e2e (`cd frontend && BACKEND_URL=http://127.0.0.1:8000 npm run e2e`)
  needs both the API and `npm run start` up, plus a tour whose projections are
  persisted (`fantasy-forecast --tour <fantasy_tour_id>`), otherwise the player-card
  test has no forecast to show. One admin test runs a *real* season import through
  the UI and stays skipped unless `RUN_LIVE_REFRESH_E2E=1` (needs outbound network
  and publishes a new snapshot).
- The admin API is `PYTHONPATH=src python3 -m fantasy_analytics.api --host
 127.0.0.1 --port 8000`. `POST /admin/ingestion/rpl/refresh` returns `202` and a
 job id immediately, then a detached worker subprocess runs the import + quality
 gate; poll `GET /admin/ingestion/runs/{id}` until `succeeded`. A full RPL season
 import takes ~45s and needs outbound access to the live Sports.ru GraphQL API.
- The discovery/ingest/match-stats CLIs also call the live Sports.ru GraphQL API
 and need outbound network, e.g.
 `PYTHONPATH=src python3 -m fantasy_analytics --output data/discovery`.
