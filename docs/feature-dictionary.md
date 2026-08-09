# Feature dictionary (development-plan step 6)

The analytical feature dataset is produced by
[`src/fantasy_analytics/features.py`](../src/fantasy_analytics/features.py) and
the `fantasy-features` CLI. It turns the *active* snapshot published by the
quality gate (step 4) into a reproducible, leakage-free table with one row per
player whose club plays a target tour.

The current `feature_version` is `1.2.0`. Version `1.1.0` added the
`saves_per90`, `recoveries_per90` and `yellows_per90` rates that the step-7
event forecast consumes; version `1.2.0` added cross-season sourcing (step 14),
the `stat_source` / `is_newcomer` labels and newcomer priors.

## Reproducibility and leakage guarantees

- **Keyed to a snapshot.** Every dataset is built from a single ingestion run
  (the active snapshot by default, or `--run-id`) and stamped with
  `feature_version`, so rebuilding from the same run yields identical rows.
- **Cutoff.** The `cutoff` is the target tour's `transfers_deadline_at`
  (falling back to `starts_at`, then the earliest fixture kickoff). Only matches
  that kicked off **strictly before** the cutoff are used.
- **Second line of defence.** Matches that belong to the target tour are also
  removed from the history explicitly, so a mis-dated deadline can never leak a
  target-tour match into the features.
- **No network, no writes.** The builder only reads domain tables; it never
  calls the Sports.ru API and never mutates the database.

## Row identity

Each row carries `feature_version`, `tour_cutoff`, `player_season_id` /
`fantasy_player_id` and the target `tour` metadata, satisfying the "player,
tour, cutoff time and feature version" requirement.

## Missing-value strategy

| Situation | Fill |
| --- | --- |
| Fewer than *N* appearances for a rolling window | Aggregate over the appearances that exist; means default to `0.0` and `appearances_{N}` records the true count |
| No appearances at all before cutoff | All rolling/per-90 features are `0.0` and `has_history` is `false` |
| No minutes played | Per-90 rates are `0.0` |
| Club has no matches before cutoff | `appearance_share` / `start_share` are `0.0`; strength falls back to the league mean |
| Club has no matches at the fixture venue | Venue attack/defence falls back to the league mean for that venue |
| No previous club match | `rest_days` is `null` |
| Player marked out (`availability_status` in the unavailable set) | `p_appearance` and `expected_minutes` are `0.0` |
| Missing snapshot fields (`price`, `selected_by`, `form`) | `null` |

Availability is unavailable when `availability_status` is one of
`INJURY`, `INJURED`, `DISQUALIFICATION`, `DISQUALIFIED`, `SUSPENDED`,
`SUSPENSION`, `OUT`, `LEFT`. Any other value (including `FIERY` and `UNKNOWN`)
is treated as available so a new status never silently zeroes a player.

## Cross-season sourcing (step 14)

Before the target season has played a single match, a pure current-season
dataset would be all zeros (there is no history yet). To forecast the first tour
of a new tournament, the builder falls back to the **prior season** by the
shared cross-season identities (`players.stat_player_id`,
`clubs.stat_team_id`), while the fixture, venue and opponent still come from the
active season.

- **When it activates.** Cross-season sourcing turns on only when the target
  season has no club match before the cutoff *and* a prior season of the same
  competition has its own published (active) snapshot. As soon as the season
  produces a played match, the builder switches back to the pure current-season
  path, so backtesting a finished season is never affected. `cross_season` and
  `prior_run_id` are reported in the dataset metadata.
- **Returning players.** A player registered in both seasons is sourced from the
  prior season by the shared `player_id`. Their appearances and appearance/start
  shares use the club they actually played for last season, so a transfer keeps
  its real track record, while the venue and opponent come from the active club.
- **Departed players.** A player who is not registered in the active season has
  no `player_season` there and simply produces no row (and no optimizer
  candidate).
- **Newcomers.** A player registered in the active season with no prior-season
  history is a newcomer: `is_newcomer` is `true`, `has_history` is `false`, and
  the event rates are filled from documented **role priors** — the prior
  season's per-90 role averages discounted by `NEWCOMER_RATE_FACTOR` (0.7),
  with a conservative `NEWCOMER_P_APPEARANCE` (0.5) play probability. These
  priors are position-based; refining them by price/club is left to step 18.
- **Provenance label.** Every row carries `stat_source` (`current_season` or
  `prior_season`), so the frontend can visually separate last season's numbers
  from the ones collected this season (steps 12–13). `rest_days` is `null` in
  cross-season mode because the active club has not played yet.

## Fields

| Field | Description |
| --- | --- |
| `feature_version` | Feature-schema version stamped on every row. |
| `player_season_id` | Internal player-season surrogate key. |
| `fantasy_player_id` | External Sports.ru fantasy player id. |
| `player_name` | Canonical player name. |
| `role` | `GOALKEEPER` / `DEFENDER` / `MIDFIELDER` / `FORWARD`. |
| `club_id`, `club_name` | Club the player belongs to at cutoff. |
| `tour_cutoff` | ISO 8601 cutoff timestamp (also the dataset `cutoff`). |
| `is_home` | `true` when the club hosts the target-tour fixture. |
| `opponent_club_id`, `opponent_name` | Target-tour opponent. |
| `match_id`, `match_scheduled_at` | Target-tour fixture identity and kickoff. |
| `rest_days` | Days between the club's last pre-cutoff match and the fixture; `null` when none. |
| `availability_status`, `status_description` | From the active snapshot. |
| `is_available` | `false` when the status marks the player out. |
| `price`, `selected_by`, `form` | Fantasy snapshot values (`null` when absent). |
| `points_avg_{3,5,10}` | Mean fantasy points over the last *N* appearances. |
| `points_sum_{3,5,10}` | Total fantasy points over the last *N* appearances. |
| `goals_sum_{3,5,10}` | Goals over the last *N* appearances. |
| `assists_sum_{3,5,10}` | Assists over the last *N* appearances. |
| `minutes_avg_{3,5,10}` | Mean minutes over the last *N* appearances. |
| `appearances_{3,5,10}` | Appearances actually found in the last-*N* window. |
| `total_appearances`, `total_minutes`, `total_points` | Season-to-date totals before cutoff. |
| `points_per90`, `goals_per90`, `assists_per90` | Season-to-date per-90 rates. |
| `saves_per90`, `recoveries_per90`, `yellows_per90` | Season-to-date goalkeeper-save, ball-recovery and yellow-card per-90 rates (consumed by the step-7 event forecast). |
| `club_matches_before` | Club matches played before cutoff (share denominator). |
| `appearance_share` | Share of club matches the player appeared in. |
| `start_share` | Share of club matches the player started (>= 60 minutes). |
| `p_appearance` | Estimated probability of playing the fixture (last 5 club matches). |
| `expected_minutes` | `p_appearance` x recent mean minutes when appearing. |
| `club_attack`, `club_defense` | Club goals scored/conceded per match at the fixture venue. |
| `opponent_attack`, `opponent_defense` | Opponent goals scored/conceded per match at their venue. |
| `has_history` | `true` when at least one appearance exists in the sourced history. |
| `stat_source` | `current_season` or `prior_season` (cross-season backfill while the target season has not started). |
| `is_newcomer` | `true` when the player has no prior-season history and is scored from role priors. |

## Command

```bash
export DATABASE_URL=postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy
PYTHONPATH=src python3 -m fantasy_analytics.features_cli \
  --tour 1786 \
  --output data/features
```

Without `--tour` the next non-finished tour is used; a fully finished season
(useful for backtesting in step 12) requires an explicit tour. Outputs land in
the chosen directory: `features.json` (metadata, feature dictionary and rows),
`features.csv` (rows only) and `feature-dictionary.json`.
