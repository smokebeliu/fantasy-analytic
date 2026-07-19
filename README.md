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

## Requirements

- Python 3.11 or newer
- Network access to `https://www.sports.ru/gql/graphql/`

The prototype has no runtime dependencies outside the Python standard library.

## Run

```bash
PYTHONPATH=src python3 -m fantasy_analytics --output data/discovery
```

Select the active season:

```bash
PYTHONPATH=src python3 -m fantasy_analytics \
  --current \
  --output data/current-season
```

Select an exact season:

```bash
PYTHONPATH=src python3 -m fantasy_analytics \
  --season-name 2025/2026 \
  --output data/2025-2026
```

Disable representative player-history requests:

```bash
PYTHONPATH=src python3 -m fantasy_analytics \
  --history-samples-per-role 0
```

Generated files:

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

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Model artifacts

- [`docs/data-model.md`](docs/data-model.md) documents confirmed identifiers,
  grains, mappings and unresolved questions.
- [`schema/postgres.sql`](schema/postgres.sql) is the proposed PostgreSQL model.

Sports.ru does not publish this GraphQL API as a stable developer interface.
The collector must preserve raw responses and use contract tests before the
model is promoted beyond the prototype.