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

Start PostgreSQL, apply `schema/postgres.sql` and run discovery:

```bash
make start
```

The first run creates `.env`, builds the discovery image and initializes the
database. Generated artifacts are written to `./data/discovery`.

The same flow without Make:

```bash
cp .env.example .env
mkdir -p data
docker compose up -d --build --wait postgres
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

Python 3.11 or newer is required. The prototype has no runtime dependencies
outside the standard library.

```bash
PYTHONPATH=src python3 -m fantasy_analytics --output data/discovery
```

Disable representative player-history requests:

```bash
PYTHONPATH=src python3 -m fantasy_analytics \
  --history-samples-per-role 0
```

Run tests without Docker:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Model artifacts

- [`docs/data-model.md`](docs/data-model.md) documents confirmed identifiers,
  grains, mappings and unresolved questions.
- [`docs/development-plan.md`](docs/development-plan.md) is the agent-oriented
  execution roadmap and must be updated after every completed step.
- [`schema/postgres.sql`](schema/postgres.sql) is the proposed PostgreSQL model.

Sports.ru does not publish this GraphQL API as a stable developer interface.
The collector must preserve raw responses and use contract tests before the
model is promoted beyond the prototype.