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

## Data quality and reconciliation

Before analytics run, `fantasy-quality` validates an imported snapshot. It
evaluates the latest successful ingestion run (or `--run-id`), records every
violation in `data_quality_issues` and publishes the snapshot (sets
`ingestion_runs.is_active`) only when no blocking issue is found, so an invalid
snapshot never supersedes the last valid one. At most one run per season is
active at a time.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.quality_cli          # latest run
PYTHONPATH=src python3 -m fantasy_analytics.quality_cli --run-id 1
```

The command prints a JSON report with the expected and actual value of every
check and exits non-zero when a blocking issue is present. Checks are split into
`blocking` (empty catalog, unresolved club references, duplicate fixtures, a
player with minutes but no match history) and `warning` (result and points
reconciliation gaps that the 72-hour Sports.ru adjustment window can still
explain). Reconciliation compares club season aggregates against results derived
from `club_match_stats` and each player's season fantasy total against the sum
of their per-match stats.

## Analytical features

Once a snapshot is published, `fantasy-features` builds a reproducible,
leakage-free dataset for a target tour from the *active* snapshot. Every feature
is derived strictly from matches that kicked off before the tour's transfer
deadline (the `cutoff`), so the dataset never sees the tour it predicts. It only
reads the database — no Sports.ru API call.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.features_cli \
  --tour 1786 \
  --output data/features
```

Without `--tour` the next non-finished tour is chosen; a fully finished season
requires an explicit tour (which is what backtesting in step 12 needs). Each row
carries `player`, `tour`, `cutoff` and `feature_version` and includes rolling
3/5/10-match metrics, per-90 rates, start and appearance shares, home/away club
and opponent strength, rest days, availability, and separate appearance
probability and expected-minutes estimates. Outputs are written to the chosen
directory: `features.json` (metadata, feature dictionary and rows),
`features.csv` and `feature-dictionary.json`. The committed feature dictionary
and the missing-value strategy live in
[`docs/feature-dictionary.md`](docs/feature-dictionary.md).

## Points forecast

Once the feature dataset can be built, `fantasy-forecast` projects the expected
fantasy points every player scores in a target tour and persists them to
`player_forecasts`. It produces an interpretable, event-based model
(`poisson_events`) alongside two baselines (`season_mean` and `recent_form`) so
the main model can always be compared. Appearance points build on the expected
minutes and appearance probability from the features; team goals are modelled as
Poisson rates from the venue attack/defence, driving the clean-sheet
probability; and goals, assists, saves, ball recoveries and cards are projected
from the player's per-90 rates. Each projected event is converted into points
with a versioned scoring table (reconstructed from the season's own per-match
points), so `expected_points` is the exact sum of its stored `components`.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.forecast_cli \
  --tour 1786 \
  --output data/forecast
```

Without `--tour` the next non-finished tour is used; a fully finished season
requires an explicit tour. Every row is stamped with the model name/version, the
feature version, the scoring version and the ingestion run it was built from, and
the computation is pure arithmetic, so recomputing on the same snapshot is
deterministic. Persistence is idempotent per run/tour/model; pass `--no-persist`
to only write the `forecast.json`/`forecast.csv` artifacts. The forecast design
and the reconstructed scoring rules live in
[`docs/data-model.md`](docs/data-model.md).

## Squad optimizer

Once forecasts can be built, `fantasy-optimize` selects the optimal fantasy
squad for a target tour: the full roster, the starting eleven, the captain, the
vice-captain and the ordered bench. It is a genuine integer program solved with
OR-Tools CP-SAT that maximises the expected points of the starting eleven plus
the captain (counted twice). The budget and per-role squad/starting limits come
from `season_rules`, and the club limit and transfer limit come from the target
`fantasy_tours` row — nothing is hard-coded. It only reads the database (through
the forecast builder) and never calls the Sports.ru API.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.optimizer_cli \
  --tour 1786 \
  --output data/optimizer
```

Without `--tour` the next non-finished tour is used; a fully finished season
requires an explicit tour. By default a fresh squad is built. Pass
`--current-squad` (comma-separated fantasy player ids) to switch to
limited-transfers mode, which keeps the existing roster and changes at most the
tour's transfer limit (override it with `--max-transfers`). `--model` selects the
forecast model to optimize on (`poisson_events` by default). Every result is
re-checked by an independent validator, an infeasible problem raises a clear
error, and the computation is deterministic. The full result is written to
`optimizer.json`. The optimizer design lives in
[`docs/data-model.md`](docs/data-model.md).

## Manual ingestion API

`fantasy-api` serves a small FastAPI control plane that triggers a full refresh
on demand (no scheduler). The request only enqueues a job and returns
immediately; a separate worker process runs the import followed by the quality
gate, so a snapshot is published only after both succeed. Job state lives in the
`ingestion_jobs` table, so statuses survive an API restart, and a partial unique
index plus a PostgreSQL advisory lock guarantee at most one refresh per
tournament at a time (no Redis).

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.db.cli upgrade   # ensure schema
PYTHONPATH=src python3 -m fantasy_analytics.api --host 127.0.0.1 --port 8000
```

Trigger an RPL refresh and poll its status:

```bash
# Returns 202 with a job id immediately; a second call while it runs returns 409.
curl -X POST http://127.0.0.1:8000/admin/ingestion/rpl/refresh

# Read the job back by the id from the response above.
curl http://127.0.0.1:8000/admin/ingestion/runs/1
```

The optional JSON body selects a season (`{"season_name": "2025/2026"}`,
`{"season_id": "59"}` or `{"current": true}`); the default is the latest
completed season. A successful job records the import counts, the quality verdict
and a `data_freshness` timestamp; a failed job records a bounded, credential-free
error message. The worker can also be run directly for a queued job:
`PYTHONPATH=src python3 -m fantasy_analytics.ingestion_worker <job_id>`.

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
- [`docs/feature-dictionary.md`](docs/feature-dictionary.md) documents the
  analytical feature dataset, its leakage guarantees and missing-value strategy.
- [`docs/development-plan.md`](docs/development-plan.md) is the agent-oriented
  execution roadmap and must be updated after every completed step.
- [`src/fantasy_analytics/db/models.py`](src/fantasy_analytics/db/models.py) is
  the authoritative SQLAlchemy definition of the PostgreSQL schema, materialized
  by the Alembic migrations in
  [`src/fantasy_analytics/migrations`](src/fantasy_analytics/migrations).

Sports.ru does not publish this GraphQL API as a stable developer interface.
The collector must preserve raw responses and use contract tests before the
model is promoted beyond the prototype.