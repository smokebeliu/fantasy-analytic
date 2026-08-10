# Feature dictionary (development-plan step 6)

The analytical feature dataset is produced by
[`src/fantasy_analytics/features.py`](../src/fantasy_analytics/features.py) and
the `fantasy-features` CLI. It turns the *active* snapshot published by the
quality gate (step 4) into a reproducible, leakage-free table with one row per
player whose club plays a target tour.

The current `feature_version` is `1.3.0`. Version `1.1.0` added the
`saves_per90`, `recoveries_per90` and `yellows_per90` rates that the step-7
event forecast consumes; version `1.2.0` added cross-season sourcing (step 14),
the `stat_source` / `is_newcomer` labels and newcomer priors; version `1.3.0`
turned that sourcing into a decaying **blend** of the two seasons, stopped
counting 0-minute matchday rows as appearances and estimates the appearance
probability from the whole sourced season instead of a five-match window.

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
| Fewer than *N* appearances for a rolling window | The window is topped up from last season; if there are still fewer, aggregate over what exists, means default to `0.0` and `appearances_{N}` records the true count |
| No appearances at all in either season | All rolling/per-90 features are `0.0` and `has_history` is `false` |
| No minutes played | Per-90 rates are `0.0` |
| Club has no matches in either season | `appearance_share` / `start_share` are `0.0`; strength falls back to the league mean |
| Club has no matches at the fixture venue | Venue attack/defence falls back to the league mean for that venue |
| No previous club match | `rest_days` is `null` |
| Player marked out (`availability_status` in the unavailable set) | `p_appearance` and `expected_minutes` are `0.0` |
| Missing snapshot fields (`price`, `selected_by`, `form`) | `null` |

Availability is unavailable when `availability_status` is one of
`INJURY`, `INJURED`, `DISQUALIFICATION`, `DISQUALIFIED`, `SUSPENDED`,
`SUSPENSION`, `OUT`, `LEFT`. Any other value (including `FIERY` and `UNKNOWN`)
is treated as available so a new status never silently zeroes a player.

## Cross-season blending

Last season is part of the history of **every** tour, not a fallback for the
opening one. A player is matched across seasons by the shared cross-season
identities (`players.stat_player_id`, `clubs.stat_team_id`), while the fixture,
venue and opponent always come from the active season.

- **How much it counts.** One prior-season observation is worth
  `0.5 ** (matches / PRIOR_SEASON_HALF_LIFE)` with a half-life of 5 matches: the
  whole story before a ball is kicked, the larger half after four matches, a
  fifth after ten, noise by the winter break. The current season is never
  discounted, so it wins as soon as it has anything to say. Below
  `PRIOR_SEASON_MIN_WEIGHT` (0.01) the prior season is not even loaded, which is
  why backtesting a fully played season is unaffected. `prior_season_weight`
  is reported per row and in the dataset metadata, next to `prior_run_id`.
- **Rates versus availability.** Per-90 rates and totals decay against the
  *player's* own matches, so a signing who has not played yet keeps last
  season's profile intact. The appearance and start shares decay against his
  *club's* matches, because eight matches spent on the bench are precisely the
  evidence that matters there. The two shares additionally cap each season at
  `SHARE_BLEND_WINDOW` (5) effective matches, so a finished 38-match season
  cannot outvote the one being played merely by being longer.
- **Rolling windows.** `points_avg_{3,5,10}` and friends run across the season
  boundary: while this season is shorter than the window it is topped up from
  last one, and each new appearance pushes one of last season's out.
- **Returning players.** A player registered in both seasons keeps the club he
  actually played for last season as the denominator of his prior shares, so a
  transfer carries its real track record rather than inheriting the new club's.
- **Departed players.** A player who is not registered in the active season has
  no `player_season` there and simply produces no row (and no optimizer
  candidate).
- **Newcomers.** A player with no appearance in *either* season is a newcomer:
  `is_newcomer` is `true`, `has_history` is `false`, and the event rates are
  filled from documented **role priors** — the prior season's per-90 role
  averages discounted by `NEWCOMER_RATE_FACTOR` (0.7), with a conservative
  `NEWCOMER_P_APPEARANCE` (0.5) play probability that itself fades as his club
  plays matches he does not. These priors are position-based; refining them by
  price/club is left to step 18.
- **Provenance label.** Every row carries `stat_source` (`current_season` or
  `prior_season`), so the frontend can visually separate last season's numbers
  from the ones collected this season (steps 12–13). It means "these numbers are
  last season's", so it flips to `current_season` on the player's first
  appearance however much last season still weighs. `rest_days` is `null` until
  the active club has played.

## Appearances are matches played

Sports.ru returns a per-match row for every **named matchday squad member**, so
an unused substitute arrives as a 0-minute, 0-point row. Only rows with minutes
count as appearances; counting the rest made a permanent reserve look
ever-present on half-length shifts.

The appearance probability is the recency-weighted share of the club's matches
the player was on the pitch for, over the whole sourced history rather than a
five-match window. Within the current season the club's latest match counts 1,
the one before it `CURRENT_RECENCY_DECAY` (0.85) and so on, because *when* a
player stopped featuring is the question. Last season is weighted flat: whether
he was rested in April or in October says nothing about a match three months
after the season ended, and decaying it would hand the entire prior weight to
the handful of dead rubbers the league's best players are routinely rested for —
which used to forecast them at exactly zero for the whole following season.

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
| `points_avg_{3,5,10}` | Mean fantasy points over the last *N* appearances, spilling into last season while this one is shorter. |
| `points_sum_{3,5,10}` | Total fantasy points over the last *N* appearances. |
| `goals_sum_{3,5,10}` | Goals over the last *N* appearances. |
| `assists_sum_{3,5,10}` | Assists over the last *N* appearances. |
| `minutes_avg_{3,5,10}` | Mean minutes over the last *N* appearances. |
| `appearances_{3,5,10}` | Appearances actually found in the last-*N* window. |
| `total_appearances`, `total_minutes`, `total_points` | Blended totals: this season's plus last season's at the prior weight, so they are fractional early in a season. |
| `current_appearances`, `current_minutes`, `current_points` | The target season's own totals before the cutoff, unweighted — what the backtest's leakage audit recomputes. |
| `points_per90`, `goals_per90`, `assists_per90` | Blended per-90 rates. |
| `saves_per90`, `recoveries_per90`, `yellows_per90` | Blended goalkeeper-save, ball-recovery and yellow-card per-90 rates (consumed by the step-7 event forecast). |
| `club_matches_before` | Target-season club matches before the cutoff (how far the prior weight has decayed). |
| `prior_club_matches` | Prior-season matches of the club the player played for last season. |
| `prior_season_weight` | What one prior-season appearance of this player is still worth (1.0 before the season starts, down to 0). |
| `appearance_share` | Blended, recency-weighted share of club matches the player was on the pitch for. |
| `start_share` | Blended share of club matches the player started (>= 60 minutes). |
| `p_appearance` | Probability of playing the fixture — the appearance share above; `0.0` when unavailable. |
| `expected_minutes` | `p_appearance` x blended mean minutes when appearing. |
| `club_attack`, `club_defense` | Club goals scored/conceded per match at the fixture venue, blended across seasons. |
| `opponent_attack`, `opponent_defense` | Opponent goals scored/conceded per match at their venue. |
| `has_history` | `true` when at least one appearance exists in either season. |
| `stat_source` | `current_season` once the player has played this season, otherwise `prior_season`. |
| `is_newcomer` | `true` when the player has no appearance in either season and is scored from role priors. |

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
