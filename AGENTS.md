# AGENTS.md

## Cursor Cloud specific instructions

This VM is provisioned for the persistence work (development-plan Step 1):
PostgreSQL 16 plus SQLAlchemy 2, Alembic and psycopg 3.

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

- Python 3.12 system interpreter. SQLAlchemy 2, Alembic and psycopg 3 (binary)
  are installed into the system Python via `pip --break-system-packages` and
  refreshed by the cloud update script; there is no virtualenv.
- The `alembic` CLI lives in `~/.local/bin` (added to `PATH` by `~/.bashrc`).
- The project package is imported via `PYTHONPATH=src`; it is not pip-installed.

### Lint / test / run

- Unit tests: `PYTHONPATH=src python3 -m unittest discover -s tests -v`
  (also documented in `README.md`).
- The discovery CLI calls the live Sports.ru GraphQL API and needs outbound
  network: `PYTHONPATH=src python3 -m fantasy_analytics --output data/discovery`.
