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

The default target is the latest completed RPL season. Once a league's
current season has been imported, the API refreshes it automatically once a
night.

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

### Forecasts are materialised automatically

Running the CLI by hand is no longer a prerequisite for the read API. Nothing
used to call the forecast step, so a freshly published snapshot served
`projection: null` and the player table's forecast column was empty until
somebody remembered to run `fantasy-forecast`; the optimizer hid the problem
because it rebuilds the forecast in process instead of reading the table. Two
places now close that gap:

- the ingestion worker forecasts the next unplayed tour right after the quality
  gate publishes (reported as the `forecast` stage of the refresh job), so a
  fresh import arrives with projections stored;
- `GET /players` and `GET /players/{id}` materialise the requested tour on first
  access, so a snapshot imported by an older build heals itself.

Building one tour is three models over a few hundred players and takes well under
a second. It runs under a session-level advisory lock keyed by `(run, tour)`, so
concurrent readers never build the same tour twice, and a tour that cannot be
forecast (no fixture yet) leaves the table untouched instead of failing the read.
The CLI remains the way to forecast an arbitrary historical tour.

### Cross-season first-tour forecast

When the target season has not played a match yet (for example the first tour of
a new tournament), the forecast falls back to the **previous** season by the
shared cross-season identities (`players.stat_player_id`, `clubs.stat_team_id`):
returning players keep their prior-season history, departed players drop out, and
newcomers get documented position priors flagged `is_newcomer` /
`has_history = false`. Every row records its `stat_source` (`prior_season` vs
`current_season`), which is persisted and exposed through the read API so the
frontend separates last season's numbers from this season's. Import both seasons
first, then forecast the active season's first tour:

```bash
PYTHONPATH=src python3 -m fantasy_analytics.ingest_cli --season-name 2025/2026
PYTHONPATH=src python3 -m fantasy_analytics.ingest_cli --current            # active season
PYTHONPATH=src python3 -m fantasy_analytics.quality_cli --run-id <active_run>
PYTHONPATH=src python3 -m fantasy_analytics.forecast_cli --season 2026/2027 --tour <tour1>
```

The season-level switch resumes the pure current-season path as soon as the new
season produces a played match, so backtesting a finished season is unaffected.
Fixture-aware co-selection is handled by the optimizer (see "Head-to-head
fixtures" below); team-strength coefficients stay out of scope (step 18).

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
forecast model to optimize on (`poisson_events` by default; `ridge_stack` is the
learned model of step 23). Three knobs added in step 23 price uncertainty and
time: `--captain-risk-weight` adds that many standard deviations of a player's
forecast to his captain score (the armband goes to the upper tail, not the
mean); `--transfer-gain-sigma` widens the transfer threshold by that many
standard deviations of both forecasts; `--horizon-tours N` (with
`--horizon-decay`) forecasts the next tours from the target tour's cutoff and
lets the roster earn their discounted sum through the best eleven it could
field there (`solution.horizon_expected_points`, `is_horizon_starter` on each
player), so a transfer is judged on the run of fixtures it buys without a
bench that will not play outweighing a star who will. The bench is ordered to
maximise what the automatic
substitutions are expected to bring, given how likely each starter is to miss
the match. Every result is
re-checked by an independent validator, an infeasible problem raises a clear
error, and the computation is deterministic. The full result is written to
`optimizer.json`. The optimizer design lives in
[`docs/data-model.md`](docs/data-model.md).

The objective is lexicographic: expected points first, then — among squads that
score the same — the fewest transfers, then the least money spent. Ties are
everywhere (two bench players of the same role and price are interchangeable), and
without the middle rank the answer could ask for three transfers worth `+0.0`
points and burn an allowance the user cannot get back. Asking for transfers on an
already optimal squad therefore reports none.

The search is bounded by a budget in *deterministic* time rather than wall clock,
so the same request returns the same squad on any machine. It runs in two phases:
a single worker proves optimality for almost everything in a fraction of a second,
and only the awkward instances — a transfers request forced to keep a squad of
near-worthless players — fall back to CP-SAT's wider strategy portfolio, which is
interleaved rather than raced so ties are still broken reproducibly. A budget that
runs out yields the best squad found so far, reported as
`solution.proven_optimal: false`, instead of no answer at all; `solve_limit` on
`solve_squad`/`build_squad_optimization` raises or lowers it.

### Which replacements to make

In limited-transfers mode `solution.transfers` describes both sides of every swap
rather than two unrelated lists:

- `out` and `in` carry full player entries (name, position, club, price, expected
  points). A player who has no candidate row for the tour — he left the league or
  his club has no fixture — is reported with `unavailable: true` and his id;
- `pairs` matches them up, one entry per swap, with `delta_expected_points` and
  `delta_price`. Because the roster's per-role limits are exact, a transfer trades
  like for like, so the pairing inside a position is the only sensible reading;
  within a position the two sides are matched by price rank, which keeps each pair
  roughly budget-neutral.

### Pinned players and a chosen formation

`--locked` keeps the players you already want and fills every remaining slot
optimally; `--locked-starters` additionally forces them into the starting eleven,
and `--formation` fixes the shape as defenders-midfielders-forwards (the
goalkeepers take the remaining starting slots):

```bash
PYTHONPATH=src python3 -m fantasy_analytics.optimizer_cli \
  --tour 2283 --locked 54138,55020 --formation 3-5-2
```

Pins are checked against the rules before the solver runs, so an impossible set
(too many players in one position or club, pins alone over budget, a formation the
season does not allow) fails with a message naming the conflicting constraint
instead of a bare "infeasible". Locked players are flagged `is_locked` in the
report and re-verified by the independent validator. The same three options exist
on `POST /optimizer/squad` and `POST /optimizer/transfers` as `locked`,
`locked_starters` and `formation`.

### Head-to-head fixtures

The tour schedule is part of the objective, so the squad does not bet on both
sides of one match: the clean sheet a defence needs is exactly what the opposing
attack has to break. Two starters that meet each other are charged the magnitude
of that cancellation, so such a pair only survives when it still wins on expected
points:

```bash
# Compare the fixture-aware optimum with the fixture-blind one.
PYTHONPATH=src python3 -m fantasy_analytics.optimizer_cli --tour 2285
PYTHONPATH=src python3 -m fantasy_analytics.optimizer_cli --tour 2285 \
  --fixture-conflict-weight 0
```

`--fixture-conflict-weight` tunes how hard a clash is charged (`0` ignores the
schedule but still reports the clashes; the API accepts the same
`fixture_conflict_weight`). The report explains the effect in
`solution.fixtures`: the fixtures whose both sides are in the eleven
(`head_to_head`), the cancelling pairs with their individual penalty (`clashes`)
and the totals, next to `fixture_penalty` and `objective_score`.
`objective_expected_points` keeps its original meaning of pure expected points,
and the independent validator recomputes the penalty from the produced eleven.

## Backtesting and model comparison

Once a finished season is imported, `fantasy-backtest` replays it tour by tour to
check whether the forecast and the optimizer would actually have helped. For each
tour it rebuilds the leakage-free features at that tour's own cutoff, forecasts
every model, solves the real squad problem on those projections and then scores
everything with the points the players actually went on to earn. It only reads
the database — no Sports.ru API call.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.backtest_cli --output data/backtest
```

Restrict the run while iterating (repeat `--tour`/`--model`, or skip the solver):

```bash
PYTHONPATH=src python3 -m fantasy_analytics.backtest_cli \
  --tour 1786 --tour 1787 --model poisson_events --model season_mean
PYTHONPATH=src python3 -m fantasy_analytics.backtest_cli --no-optimize
```

Several leagues at once (step 23): repeat `--run-id`, or pass `--all-active`
for every published snapshot with played tours. Each run gets its own
`run-<id>/` sub-directory and `summary.md` / `summary.json` put the leagues
side by side, so a model change is judged on every season it can be:

```bash
PYTHONPATH=src python3 -m fantasy_analytics.backtest_cli \
  --run-id 1 --run-id 2 --output data/backtest/step23
```

By default every tour gets a fresh squad, which measures the forecast's pick of
the tour. `--carry-squad` plays the season the way the game is played: one
squad kept from tour to tour, only the tour's transfer allowance spent
(`--max-transfers` overrides it), each swap having to clear
`--min-transfer-gain` plus `--transfer-gain-sigma` standard deviations of both
forecasts; `--horizon-tours N` chooses the roster on the discounted
(`--horizon-decay`) forecast of the next tours, built from the current tour's
cutoff so nothing played in between leaks in. `--captain-risk-weight` hands
the armband to the upper tail of the forecast rather than the mean.

What the run reports:

- **Accuracy** (MAE/RMSE/bias) per tour and per position, twice: over every
  selectable player and over the players who actually took the field. The full
  population is dominated by correctly predicted zeros for players who never
  appeared, so the second view is the sharper comparison.
- **Squad quality**: the projected and the realised points of the squad each model
  produced, the points left on the bench, the best eleven those same 15 players
  could have fielded (`lineup_efficiency`), how often the captain turned out to be
  the eleven's top scorer, the hindsight optimum the tour allowed, and (step 23)
  what the game itself would have credited once its automatic substitutions and
  the vice-captain rule ran (`actual_points_autosub`).
- **Ranking** (step 23): the Spearman correlation between forecast and fact among
  the players who played, and the tour's top-25 by forecast against what they
  scored and against the real top-25 (`ranking` per model and per tour). The
  whole-pool MAE is dominated by correct zeros; the order is what a manager
  acts on.
- **Two baselines** (`season_mean`, `recent_form`) next to the event model, and an
  explicit verdict: `accept_model`, `revise_model` or `keep_baseline`, with the
  numbers behind it. The learned model (`ridge_stack`, step 23) is evaluated
  alongside as a *challenger*: it never counts as a baseline, and the verdict
  says separately whether it beat the event model on every criterion.
- **A leakage audit** that does not trust the feature builder: every row's history
  totals are recomputed from the raw appearance table restricted to matches before
  the cutoff and outside the tour. A mismatch is a violation and the command exits
  non-zero, so a broken cutoff can never look like a good backtest.
- **The least stable features**, ranked by how much a player's own value moves
  between consecutive tours relative to how much players differ.

Artifacts land in the chosen directory: `backtest.json` (the full report with the
run parameters), `tour-metrics.csv` (one row per tour and model) and `report.md`.
A committed snapshot of a full-season run lives in
[`docs/backtest-2025-2026.md`](docs/backtest-2025-2026.md).

## Manual ingestion API

`fantasy-api` serves a small FastAPI control plane that triggers a full refresh
on demand, and also runs a nightly sweep for every league whose *current*
season is already imported. A refresh only enqueues a job and returns
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

While a job runs, the worker mirrors the pipeline's progress onto the job row as a
coarse stage (`queued` → `starting` → `fetch_season` → `fetch_players` →
`fetch_history` → `persist` → `quality_gate` → `finished`) with a rough completion
percentage, so a client can show what is happening instead of a bare spinner. A
single endpoint answers "is a refresh running, and what is published" without a
job id, which is what lets the UI recover its state after a reload:

```bash
curl http://127.0.0.1:8000/admin/ingestion/rpl/status
```

It returns the in-flight job (with its `progress`), the last job, the last
successful one, the active snapshot with its `data_freshness`, the season and the
target tour, plus the stage vocabulary. A finished job's full import/quality
report stays on `GET /admin/ingestion/runs/{job_id}`; the status payload only
carries the headline counts so polling stays cheap.

### Nightly refresh

The API process (not a separate cron container) sleeps until 03:00
`Europe/Moscow` and then enqueues a `current`-season job for every league that
already has an active season in `seasons`. Catalogued-only leagues and leagues
whose only import is a finished historical season are skipped. A second
scheduled job the same local day is not created, and a league that already has
a pending/running refresh is left alone. Jobs are marked
`trigger_type=scheduled` so they show up next to the manual ones.

```bash
# Who would be refreshed tonight, and when is the next run?
curl http://127.0.0.1:8000/admin/ingestion/nightly

# Run the sweep now (still skips a league that is already refreshing).
curl -X POST 'http://127.0.0.1:8000/admin/ingestion/nightly?force=true'
```

**Everything in one go.** After a model change the whole dataset has to be
rebuilt, which used to mean every league twice plus the odds button. The
*full refresh* does that sequence for every imported league, in catalogue
order and strictly one step after another: the latest completed season (the
forecast's prior), then the current season (skipped when the league has none),
then the 1x2 line with the next-tour forecast rebuilt. Each import is a regular
job (`trigger_type=full_refresh`) visible in the per-league panel; a failing
step is recorded and the run carries on, ending `failed` so the screen says
where. The run itself is orchestrated in the API process and is not persisted:
a restart mid-run ends it, and the button is simply pressed again. The admin
screen has it as «Обновить все лиги и котировки» above the per-league panel.

```bash
curl -X POST http://127.0.0.1:8000/admin/ingestion/full-refresh   # 202, or 409 while running
curl http://127.0.0.1:8000/admin/ingestion/full-refresh           # steps and their state
```

The same sweep is also a one-shot CLI, useful if you prefer the host crontab
over the in-process scheduler:

```bash
PYTHONPATH=src python3 -m fantasy_analytics.nightly_refresh
PYTHONPATH=src python3 -m fantasy_analytics.nightly_refresh --dry-run
```

Schedule knobs (environment or `fantasy-api` flags):

| Variable | Default | Meaning |
| --- | --- | --- |
| `NIGHTLY_REFRESH_ENABLED` | `true` | Set `false` or pass `--no-nightly-refresh` to disable |
| `NIGHTLY_REFRESH_HOUR` | `3` | Local hour (0–23) |
| `NIGHTLY_REFRESH_MINUTE` | `0` | Local minute |
| `NIGHTLY_REFRESH_TZ` | `Europe/Moscow` | IANA timezone of that clock |
| `NIGHTLY_REFRESH_CATCHUP_HOURS` | `3` | After a deploy that lands just after the hour, still run tonight |

## User REST API

`fantasy-api` also serves a read API for the frontend on top of the *active*
snapshot published by the quality gate. Every read endpoint is answered
exclusively from PostgreSQL — the Sports.ru GraphQL API is never called from a
read path. Catalog data (seasons, tours, matches, players, clubs) is always
available; per-player numbers (price, availability, ownership, season score)
come from the active snapshot, and projections plus their explaining components
come from the persisted `player_forecasts` rows.

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.api --host 127.0.0.1 --port 8000
```

Read endpoints (all paginated with a bounded `limit` ≤ 200 and an `offset`):

```bash
curl "http://127.0.0.1:8000/seasons"
curl "http://127.0.0.1:8000/seasons/1"
curl "http://127.0.0.1:8000/tours?season_id=1&status=FINISHED"
curl "http://127.0.0.1:8000/matches?season_id=1&club_id=7"
# Filter by position/club/status/price and join a tour's projection + components.
curl "http://127.0.0.1:8000/players?season_id=1&tour_id=15&model=poisson_events\
&role=MIDFIELDER&min_price=8&order=projection&limit=20"
curl "http://127.0.0.1:8000/players/2?tour_id=15"   # card with history + projection
```

Every player also carries a `prior_season` block — what the same person did in the
previous season, resolved through the cross-season identity
(`players.stat_player_id`): points, average, rank, closing price, appearances with
at least a minute played, minutes, goals, assists, saves, recoveries, cards and
goals conceded. In the opening tours the current-season columns are still nearly
empty, so this is what actually tells a manager whether a player is worth buying.
The block is `null` when nothing precedes the season — unlike the cross-season
forecast, the read path refuses to fall back to a *later* season, which would
label next season as last season.

Optimizer endpoints wrap the step-8 solver (database only, no GraphQL):

```bash
curl -X POST http://127.0.0.1:8000/optimizer/squad \
  -H 'Content-Type: application/json' -d '{"tour": "1786"}'
curl -X POST http://127.0.0.1:8000/optimizer/squad \
  -H 'Content-Type: application/json' \
  -d '{"tour": "1786", "locked": ["54138"], "formation": "3-5-2"}'
curl -X POST http://127.0.0.1:8000/optimizer/transfers \
  -H 'Content-Type: application/json' \
  -d '{"tour": "1786", "current_squad": ["54138", "..."], "max_transfers": 2}'
# Ignore the tour schedule (the clashes are still reported).
curl -X POST http://127.0.0.1:8000/optimizer/squad \
  -H 'Content-Type: application/json' \
  -d '{"tour": "1786", "fixture_conflict_weight": 0}'
```

List responses carry the snapshot time (`data_freshness`); projections carry the
model, feature and scoring versions. Every error uses one envelope,
`{"error": {"type", "message", "details"}}`. Projections are read from
`player_forecasts`, which the player endpoints materialise on first access (see
"Forecasts are materialised automatically"), so no manual `fantasy-forecast` run
is needed before a tour has projections. The OpenAPI schema is committed at
[`docs/openapi.json`](docs/openapi.json) and regenerated with:

```bash
PYTHONPATH=src python3 -m fantasy_analytics.openapi_cli --output docs/openapi.json
```

## Analytical frontend

`frontend/` is a Next.js (App Router) + TypeScript app that consumes the read API
and squad optimizer. The player table shows the full season statistics as of now
(no tour filter) with position/club/status/price filters, instant client-side
sorting (including by season points), pagination and up-to-four player
comparison; the `Прогноз` column is projected for the upcoming tour and a
`Прошлый сезон` column carries last season's points. A player card (slide-over
drawer and a dedicated `/players/[id]` route) keeps the per-tour match history, a
forecast breakdown by scoring component and a `Прошлый сезон` block (points,
average, rank, appearances, minutes, goals, assists and saves or recoveries).

Hovering any player — on the pitch, in the pool or in the table — opens a compact
card with his full name, position, club, price, projection, season points and
average, plus last season's points, average, rank and appearances. A pitch card
only has room for a name and a number, and last season is the evidence that
matters most before the new one has produced any.

The squad builder validates every roster rule (size, per-role, budget, club limit,
duplicates) on the client before submission and offers three optimizer actions
that differ only in what they are allowed to keep — which a note under the toolbar
spells out, because the labels alone cannot:

- **`Собрать состав с нуля`** builds the best squad for the tour and ignores
  everything you selected;
- **`Подобрать под мою схему`** keeps the players pinned with 📌 and the chosen
  formation and fills the remaining slots optimally. While nothing is pinned and no
  formation is chosen it is unavailable with the reason on the button: it would
  return the same squad as building from scratch;
- **`Оптимизировать замены (N)`** keeps the squad you own and suggests at most `N`
  replacements, `N` defaulting to the tour's own allowance (three in the RPL) or,
  after a Sports.ru import, to the transfers the team actually has left. The
  import also brings the team's money (its value plus the bank, `budget` in the
  response), which replaces the season's opening budget in the builder and in
  the plan (`budget` on `POST /optimizer/transfers`), since prices drift and a
  squad is rarely worth exactly the opening budget mid-season. The
  result is one row per swap — who leaves, who arrives, the points gained and
  whether the replacement is dearer or cheaper — and it does *not* overwrite your
  squad, because the whole point is to compare the two.

When the resulting eleven still contains players who face each other, the result
panel names each such pair and the penalty it cost. Data freshness and the model
version are shown in the header, and loading/empty/error states are covered across
all views.

The `Обновление` screen (`/admin`) drives the manual refresh: one button enqueues
a real ingestion job, the panel shows the stage, the progress, the start time and
the elapsed duration, and it polls only while the job is in flight. A second click
is impossible while a refresh runs (the button is disabled and the API answers
`409`), the state is rediscovered from the server after a page reload, and the
screen always shows the published snapshot with its freshness and the target tour.
On success it re-renders the header with the new snapshot; on failure it shows the
bounded, credential-free error message and offers a retry. A successful import
whose snapshot the quality gate refused to publish is reported as its own case, so
"the data did not change" is never silently mistaken for "nothing happened".

Every browser request is proxied same-origin through `/api/backend/*` to the
FastAPI backend (no CORS), so the frontend only needs `BACKEND_URL` to reach it.

Run it against a running backend (see the API sections above) and a tour whose
projections have been persisted with `fantasy-forecast --tour <id>`:

```bash
cd frontend
npm install
BACKEND_URL=http://127.0.0.1:8000 npm run dev   # http://127.0.0.1:3000
```

Checks (from `frontend/`):

```bash
npm run typecheck                 # tsc --noEmit
npm run build                     # production build (standalone output)
npm test                          # Vitest unit + component tests
npx playwright install chromium   # once
BACKEND_URL=http://127.0.0.1:8000 npm run e2e   # Playwright e2e (needs API up)
# Also run the real refresh e2e (imports a season; needs outbound network).
RUN_LIVE_REFRESH_E2E=1 BACKEND_URL=http://127.0.0.1:8000 npm run e2e
```

The whole stack (PostgreSQL, migrations, API, worker-free read API and the
frontend) also runs via Compose: `docker compose up -d --build` starts `postgres`,
`migrate`, `api` (`:8000`) and `frontend` (`:3000`). Import a season first so the
read endpoints have an active snapshot.

## Production deployment

The app is deployed to a dedicated OVH VPS via Dokploy (Docker Compose +
Traefik) with automatic redeploys on merge to `develop`. The production stack
lives in `compose.prod.yaml`, the CI/CD pipeline in
`.github/workflows/ci-cd.yml`, and the full setup guide (DNS, Dokploy,
secrets, first-time ingestion, smoke test) in
[`docs/deployment.md`](docs/deployment.md).

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
- [`docs/backtest-2025-2026.md`](docs/backtest-2025-2026.md) is a committed
  snapshot of a full-season backtest: model versus baselines, per-tour and
  per-position errors, squad results and the resulting decision.
- [`docs/forecast-improvement-plan.md`](docs/forecast-improvement-plan.md) is
  the prioritised list of what to improve in the forecast and the optimizer
  next, with the backtest as the acceptance gate for every item.
- [`docs/development-plan.md`](docs/development-plan.md) is the agent-oriented
  execution roadmap (planning, current statuses and key nuances) and must be
  updated after every completed step.
- [`docs/development-history.md`](docs/development-history.md) holds the detailed
  execution cards of completed steps and the status changelog.
- [`src/fantasy_analytics/db/models.py`](src/fantasy_analytics/db/models.py) is
  the authoritative SQLAlchemy definition of the PostgreSQL schema, materialized
  by the Alembic migrations in
  [`src/fantasy_analytics/migrations`](src/fantasy_analytics/migrations).

Sports.ru does not publish this GraphQL API as a stable developer interface.
The collector must preserve raw responses and use contract tests before the
model is promoted beyond the prototype.