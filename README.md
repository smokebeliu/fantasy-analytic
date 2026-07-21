# Fantasy Analytics

Prototype for discovering and validating the Sports.ru Fantasy RPL data model.
It reads the internal GraphQL API, saves untouched responses and produces
normalized samples that can be used to refine the PostgreSQL schema.

## What the prototype fetches

- Fantasy tournament and all season references
- Season rules, roster constraints and tour-specific transfer limits
- Clubs, tours and matches
- Every player with price, role, status, ownership and aggregate statistics
- Season totals for every club
- A representative match history for the best player in each role
- Derived home/away and clean-sheet club statistics

The default target is the latest completed RPL season. No scheduler or
continuous collection is included.

## Docker quick start

Requirements:

- Docker with the Compose plugin
- Make (optional)
- Network access to `https://www.sports.ru/gql/graphql/`

Start PostgreSQL, apply the Alembic migrations and run discovery:

```bash
make start
```

The first run creates `.env`, builds the image, applies migrations to the
latest revision and runs discovery. Generated artifacts are written to
`./data/discovery`.

Apply migrations without running discovery:

```bash
make migrate
```

The same flow without Make:

```bash
cp .env.example .env
mkdir -p data
docker compose up -d --build --wait postgres
docker compose run --rm --build migrate
docker compose run --rm --build discovery
```

Run discovery for an exact season:

```bash
docker compose run --rm discovery \
  --season-name 2025/2026 \
  --output /app/data/2025-2026
```

Run it for the active season:

```bash
docker compose run --rm discovery \
  --current \
  --output /app/data/current-season
```

Connect to PostgreSQL:

```bash
docker compose exec postgres \
  psql -U fantasy -d fantasy
```

Useful lifecycle commands:

```bash
make test
make logs
make down
make reset  # Also deletes the local PostgreSQL volume
```

The SQL initialization scripts run only when the PostgreSQL volume is first
created. Use `make reset` after changing the prototype schema.

## Generated files

```text
data/discovery/
  raw/                                  Original GraphQL responses
  normalized/
    season.json
    players.json
    matches.json
    team-season-stats.json
    player-history-samples.json
    derived-team-match-stats.json
  report.json                           Counts and model findings
```

Generated data is ignored by Git.

## Native Python run

Python 3.11 or newer is required. The persistence layer depends on
SQLAlchemy 2, Alembic and psycopg 3, so install the project first (a virtual
environment is recommended):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Run discovery (writes JSON artifacts, no database required):

```bash
PYTHONPATH=src python3 -m fantasy_analytics --output data/discovery
```

Disable representative player-history requests:

```bash
PYTHONPATH=src python3 -m fantasy_analytics \
  --history-samples-per-role 0
```

## Database migrations

The database schema is defined by the SQLAlchemy models in
`src/fantasy_analytics/db/models.py` and versioned with Alembic. Point
`DATABASE_URL` at a PostgreSQL instance and apply migrations:

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.db.cli upgrade    # create schema
PYTHONPATH=src python3 -m fantasy_analytics.db.cli current    # show revision
PYTHONPATH=src python3 -m fantasy_analytics.db.cli downgrade  # drop schema
```

The installed project also exposes a `fantasy-migrate` console command.

## Full historical import

After the schema is migrated, `fantasy-ingest` loads a complete season into
PostgreSQL (season, rules, clubs, tours, matches, every player page with its
fantasy snapshot and season aggregate, club season aggregates and each player's
match history with minutes). Catalog rows are upserted idempotently by external
identifier, while price/score/aggregate snapshots are versioned per ingestion
run. It needs both `DATABASE_URL` and outbound access to the GraphQL API.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.db.cli upgrade      # ensure schema
PYTHONPATH=src python3 -m fantasy_analytics.ingest_cli          # latest season
```

Import an exact season and tune concurrency of the per-player history requests:

```bash
PYTHONPATH=src python3 -m fantasy_analytics.ingest_cli \
  --season-name 2025/2026 \
  --history-workers 8
```

The command prints a JSON report with per-entity counts and the duration of
each stage, and records the run in `ingestion_runs`. Fetching happens up front;
the database is written in a single transaction, so a failed page never
publishes a partial snapshot. Re-running the import leaves the number of logical
entities unchanged and only appends a new snapshot generation.

## Extended match-statistics discovery

After a season is imported, `fantasy-match-stats` probes the Sports.ru
`statQueries.football.match` API to measure how reliably the extended football
metrics (shots, possession, lineups, per-player stats, events and xG) are
populated. It samples a reproducible subset of finished matches from the catalog
(spread across the season, covering every club), stores the raw payloads and
writes a field-coverage report. It needs `DATABASE_URL` and outbound access to
the GraphQL API.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.match_stats_cli \
  --season-name 2025/2026 \
  --sample-size 40 \
  --output data/match-stats
```

Outputs land in the chosen directory: `raw/match-*.json` (untouched payloads),
`field-coverage.json`/`field-coverage.md` (the `path → type → fill → decision`
table), `match-consistency.json` and `report.json`. A committed snapshot of the
table lives in [`docs/match-stats-coverage.md`](docs/match-stats-coverage.md).

## Tests

Run the unit tests without Docker:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The persistence integration tests need a reachable PostgreSQL database. They
are skipped automatically unless `TEST_DATABASE_URL` (or `DATABASE_URL`) points
to one:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy_test
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Model artifacts

- [`docs/data-model.md`](docs/data-model.md) documents confirmed identifiers,
  grains, mappings and unresolved questions.
- [`docs/development-plan.md`](docs/development-plan.md) is the agent-oriented
  execution roadmap and must be updated after every completed step.
- [`src/fantasy_analytics/db/models.py`](src/fantasy_analytics/db/models.py) is
  the authoritative SQLAlchemy definition of the PostgreSQL schema, materialized
  by the Alembic migrations in
  [`src/fantasy_analytics/migrations`](src/fantasy_analytics/migrations).

Sports.ru does not publish this GraphQL API as a stable developer interface.
The collector must preserve raw responses and use contract tests before the
model is promoted beyond the prototype.