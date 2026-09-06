# Feature dictionary (development-plan step 6)

The analytical feature dataset is produced by
[`src/fantasy_analytics/features.py`](../src/fantasy_analytics/features.py) and
the `fantasy-features` CLI. It turns the *active* snapshot published by the
quality gate (step 4) into a reproducible, leakage-free table with one row per
player whose club plays a target tour.

The current `feature_version` is `1.8.0`. Version `1.1.0` added the
`saves_per90`, `recoveries_per90` and `yellows_per90` rates that the step-7
event forecast consumes; version `1.2.0` added cross-season sourcing (step 14),
the `stat_source` / `is_newcomer` labels and newcomer priors; version `1.3.0`
turned that sourcing into a decaying **blend** of the two seasons, stopped
counting 0-minute matchday rows as appearances and estimates the appearance
probability from the whole sourced season instead of a five-match window;
version `1.4.0` reports every match a club plays in the target tour rather than
only the earliest; version `1.5.0` marks a player unavailable for the target
tour when a red card from a previous match has not yet been served; version
`1.6.0` caps last season at a window of matches and shrinks every rate towards
the role average (step 22); version `1.7.0` (step 23) adds the full-match
share, the rare-event rates, recency decay inside the current season,
within-season transfers, the second match of a straight red, ban end dates and
the pooled venue strengths — see [Step 23](#step-23-what-the-forecast-never-saw);
version `1.8.0` (step 24) sources a European cup from the national leagues its
clubs play in at the same time — see
[Step 24](#step-24-a-european-cup-from-the-national-leagues).

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
| Unserved red card (`red_card_suspension`) | Same as marked out: `is_available` is `false`, `p_appearance` and `expected_minutes` are `0.0` |
| Missing snapshot fields (`price`, `selected_by`, `form`) | `null` |

Availability is unavailable when `availability_status` is one of
`INJURY`, `INJURED`, `DISQUALIFICATION`, `DISQUALIFIED`, `SUSPENDED`,
`SUSPENSION`, `OUT`, `LEFT`, **or** when the player received a red card and
his club has not played a later match before the cutoff (the one-match ban
that skips the next tour). Any other snapshot value (including `FIERY` and
`UNKNOWN`) is treated as available so a new status never silently zeroes a
player; the red-card ban is derived from `player_match_stats.red_cards`, not
from the snapshot, so it also applies when backtesting a historical tour.

## Cross-season blending

Last season is part of the history of **every** tour, not a fallback for the
opening one. A player is matched across seasons by the shared cross-season
identities (`players.stat_player_id`, `clubs.stat_team_id`), while the fixture,
venue and opponent always come from the active season.

- **How much it counts.** One prior-season observation is worth
  `0.5 ** (matches / PRIOR_SEASON_HALF_LIFE)` with a half-life of 3 matches
  (5 before 1.6.0): the whole story before a ball is kicked, half of it after
  three matches, a quarter after six, a tenth by the tenth. The current season is never
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
- **Last season is worth a window, not a season (1.6.0).** The per-90 rates
  used to pool last season at `weight x every match`, so with the weight at
  one half a 30-match season still brought three times the evidence of five
  new matches and a player whose role or club changed over the summer kept
  last year's numbers into the autumn. Now last season's totals are scaled to
  at most `RATE_PRIOR_WINDOW` matches *before* the weight is applied
  (`prior_season_scale = prior_season_weight x min(1, window / prior matches)`),
  so the two seasons meet as equals once the new one is a few matches old and
  the current one takes over from there.
- **Shrinkage towards the role average (1.6.0).** Every per-90 rate is
  pooled with `RATE_SHRINK_MATCHES` pseudo-matches of the league's average for
  the player's position (built from both seasons, cut at the deadline). Two
  goals in three games therefore read as roughly half a goal a game rather
  than a goal a game, a keeper's single nine-save match does not make him a
  nine-save keeper, and a player with a season behind him is barely moved.
  The same pooled role averages are what a newcomer starts from, so a first
  imported season scores its newcomers too.
- **Rolling windows.** `points_avg_{3,5,10}` and friends run across the season
  boundary: while this season is shorter than the window it is topped up from
  last one, and each new appearance pushes one of last season's out.
- **Returning players.** A player registered in both seasons keeps the club he
  actually played for last season as the denominator of his prior shares, so a
  transfer carries its real track record rather than inheriting the new club's.
- **Departed players.** A player who is not registered in the active season has
  no `player_season` there and simply produces no row (and no optimizer
  candidate).
- **Newcomers.** A player with no appearance in *either* season before the
  cutoff is a newcomer: `is_newcomer` is `true`, `has_history` is `false`, and
  the event rates are filled from documented **role priors** — the pooled
  per-90 role averages discounted by `NEWCOMER_RATE_FACTOR` (0.7). His play
  probability is **priced** (1.6.0): `NEWCOMER_P_APPEARANCE` (0.25) at his
  position's median price, `NEWCOMER_P_APPEARANCE_GOALKEEPER` (0.10) for a
  keeper, plus `NEWCOMER_PRICE_SLOPE` (0.12) per price unit above or below the
  median, clamped to `[0.03, 0.65]`. Over the opening tour of four seasons a
  median-priced newcomer played one time in four and a keeper one in fourteen,
  so the old flat one-in-two over-predicted every one of them. The assumption
  is worth `NEWCOMER_PRIOR_MATCHES` (1) match of evidence and fades against
  every club match he sits out — a newcomer who missed the opener plays the
  second match one time in twenty. Before 1.6.0 the newcomer test read the
  raw appearance list rather than the one cut at the deadline, so a backtest's
  opening tour treated everyone who would play later as a known player with an
  empty history and kept the newcomer prior for those who never play at all.
- **Provenance label.** Every row carries `stat_source` (`current_season` or
  `prior_season`), so the frontend can visually separate last season's numbers
  from the ones collected this season (steps 12–13). It means "these numbers are
  last season's", so it flips to `current_season` on the player's first
  appearance however much last season still weighs. `rest_days` is `null` until
  the active club has played.

## Step 23: what the forecast never saw

- **The full match (`ninety_share`).** Scoring `rpl-2025-2026.3` found that a
  midfielder or forward who plays all 90 minutes earns a third appearance
  point — one point in 14 % of every played row, the largest reward the
  model did not know about. `ninety_share` is the blended, recency-weighted
  share of club matches the player finished, built exactly like
  `start_share`, and the forecast reads `p_ninety` off it (never above the
  start probability).
- **Rare events.** `reds_per90`, `own_goals_per90`, `pen_missed_per90`,
  `pen_saved_per90` and `pen_conceded_per90` are pooled like every other rate
  but shrunk with `RARE_EVENT_SHRINK_MATCHES` (20) pseudo-matches: two red
  cards in a season say almost nothing about a player, so his rate stays
  close to the role average for far longer than his goals do. A missed
  penalty is any of Sports.ru's three flavours (off target, post, saved).
- **Recency inside the season (`CURRENT_RATE_DECAY`).** The current season's
  appearances enter the per-90 rates at `decay ** i` for the i-th most recent
  one, so a goal last week weighs more than one in August; the `current_*`
  totals the leakage audit recomputes stay unweighted. Last season keeps its
  capped, half-life-weighted share. Chosen with `RATE_SHRINK_MATCHES` on the
  2025/26 seasons of both leagues (see the step-23 card).
- **Club form and venue.** A club's own results decay by `CLUB_RECENCY_DECAY`
  per match the same way (neutral over a full season, kept at 1.0). With
  `STRENGTH_VENUE_MODE = "pooled"` a club has one attack and one defence
  estimated from every match, each goal count divided by the league's home
  or away factor and the factor multiplied back in at the fixture venue,
  which doubles the sample behind every strength; `"split"` keeps separate
  home and away estimates.
- **Within-season transfers (`club_matches_available`).** Every appearance
  carries the club the player was registered with for that match. A player
  who changed club is judged on his old club's matches up to his last
  appearance for it and on his new club's after that, so the new club's
  August, played without him, is not read as time on its bench.
- **Availability (`availability_factor`).** A second yellow is a one-match
  ban; a straight red is served in the next match for certain and, about a
  third of the time, in the one after (`STRAIGHT_RED_SECOND_MATCH_SHARE`,
  measured over 2024/25 and 2025/26 in both leagues), so the second match is
  discounted rather than ruled out. A disqualification whose end date the
  snapshot carries (`status_description`, e.g. `2026-09-01`) ends on that
  date; an injury with a past return date, or a "questionable" status
  (`QUESTIONABLE_STATUSES`), plays at `QUESTIONABLE_APPEARANCE_FACTOR` (0.5).
  The two imported leagues only ever carry the status name or a date in the
  description, so the vocabulary is a hook rather than a dictionary.
- **Crowd wisdom (off by default).** `PRICE_BUCKET_PRIORS` shrinks rates
  towards the average of the player's price tercile within his role instead
  of the whole role; `OWNERSHIP_APPEARANCE_WEIGHT` lets ownership lift a low
  appearance share (a player owned by `OWNERSHIP_FULL_PERCENT` percent of
  managers reads as a starter). Both use snapshot values that a backtest can
  only take from the end of the season, so what they measure there is an
  upper bound; both stayed off (see the step-23 card).

## Step 24: a European cup from the national leagues

A Champions League or Europa League season (`PARALLEL_TARGET_SLUGS`) has no
history of its own before its first tour and only eight matches per club in
its league phase, while its clubs are playing their national championships at
the same time. Version `1.8.0` reads those championships as *parallel
layers* of the same history:

- **Identity is free.** Sports.ru's stat slugs are global, so a cup player is
  the same `players` row as his league self and a cup club the same `clubs`
  row. No name matching is involved; a player the leagues do not know (a
  youth player, a club from a league Sports.ru has no fantasy for) stays a
  newcomer on the position priors.
- **Which leagues.** Every imported league (every catalogued competition
  outside `NON_LEAGUE_SLUGS`) whose active season overlaps the target one at
  the cutoff (`parallel_season_overlaps`): started by the cutoff, not ended
  before the target season began. Each league brings its own previous season
  as well, so on 8 September a player is not described by three matches.
- **Weights.** The league's current season counts at `PARALLEL_WEIGHT` (0.7)
  per observation against 1.0 for the cup's own; it also counts as "fresh"
  evidence that decays both last seasons (`prior_season_weight` is judged on
  own matches plus `0.7 x` league matches). The league's last season and the
  cup's last season share one prior window (`RATE_PRIOR_WINDOW`), so a full
  Bundesliga cannot outvote by being long.
- **Goals are translated.** A league's goals enter the cup's numbers through
  `LEAGUE_STRENGTH` (`league_factor` on the row): a player's goals and assists
  `x factor`, his saves `/ factor`, his club's goals scored `x factor` and
  conceded `/ factor`. Minutes, appearances, cards and recoveries are not
  scaled. The factors are provisional constants ordered by the UEFA
  coefficient ranking; a fixture with a stored 1x2 line is mostly corrected
  by the odds anyway.
- **Appearance shares** blend every layer by its capped match count
  (`SHARE_BLEND_WINDOW`) at the layer's weight; a league club's matches count
  only when the league registers the player with the club the cup does
  (`club_id`), so a player who left in the window does not inherit his old
  club's silence.
- **Availability** is lent: an out or doubtful status on a league snapshot
  marks the cup row out when the cup's own says nothing
  (`availability_source` names the league). Red cards are not: a ban is
  served in the competition it was earned in.
- **Provenance.** `stat_source` becomes `parallel_league` when the player's
  only play this season is in his league; `sources` lists every layer with
  the counts and weights it entered at; the dataset reports `parallel_runs`
  and counts `parallel_sourced` / `players_with_parallel` /
  `availability_lent`. The `current_*` totals remain the cup's own, so the
  backtest's leakage audit is unchanged. `fantasy-backtest --no-parallel`
  measures the cup on its own matches only.

## A tour is a slice of the calendar, not a round

Fantasy tours are time windows that cannot overlap, unlike league rounds, which
keep a postponed match no matter when it is eventually played. Sports.ru
therefore re-attaches a moved match to whichever tour its new date falls closest
to: it stays in its own tour (and moves the deadline) when the new date is still
nearer to it, joins the next tour when it is nearer to that one, and joins the
*previous* tour when it is brought forward past the midpoint — a Wednesday match
after a tour that ended on Monday belongs to that tour. Ties keep the original
calendar rather than creating a double.

Two consequences reach the feature builder, and both are carried through:

- **Double gameweeks.** A club can play **twice** in one tour. Every one of its
  matches is reported in `tour_fixtures` (`fixture_count` says how many), and
  the forecast is the sum over them: appearance points, goals, clean sheets and
  the concession penalty are all counted once per match, each against its own
  opponent and venue. The flat `match_id` / `opponent_name` / `is_home` /
  `club_attack` fields describe the *first* of them, so a reader that only ever
  expected one keeps working.
- **Blank gameweeks.** A club can play **none**, which is what the tour the
  match was moved out of looks like. Its players produce no row and no optimizer
  candidate for that tour, and are counted in `players_without_fixture`.

The `cutoff` is never later than the tour's own first kickoff, whatever the
recorded deadline says. A moved match can leave the deadline sitting after a
kickoff, and while the target tour's matches are excluded from a player's
history by id, the club-strength aggregates have no player to exclude them by —
so a tour would end up predicted partly from itself.

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
| `is_available` | `false` when the status marks the player out or a red card from a previous match has not yet been served. |
| `red_card_suspension` | `true` when the player received a red card and his club has not played a later match before the cutoff, so he misses the target tour. |
| `price`, `selected_by`, `form` | Fantasy snapshot values (`null` when absent). |
| `points_avg_{3,5,10}` | Mean fantasy points over the last *N* appearances, spilling into last season while this one is shorter. |
| `points_sum_{3,5,10}` | Total fantasy points over the last *N* appearances. |
| `goals_sum_{3,5,10}` | Goals over the last *N* appearances. |
| `assists_sum_{3,5,10}` | Assists over the last *N* appearances. |
| `minutes_avg_{3,5,10}` | Mean minutes over the last *N* appearances. |
| `appearances_{3,5,10}` | Appearances actually found in the last-*N* window. |
| `total_appearances`, `total_minutes`, `total_points` | Blended totals: this season's plus last season's at `prior_season_scale`, so they are fractional early in a season. |
| `current_appearances`, `current_minutes`, `current_points` | The target season's own totals before the cutoff, unweighted — what the backtest's leakage audit recomputes. |
| `points_per90`, `goals_per90`, `assists_per90` | Blended per-90 rates, shrunk towards the league's role average with `RATE_SHRINK_MATCHES` pseudo-matches. |
| `saves_per90`, `recoveries_per90`, `yellows_per90` | Blended goalkeeper-save, ball-recovery and yellow-card per-90 rates, shrunk the same way (consumed by the step-7 event forecast). |
| `club_matches_before` | Target-season club matches before the cutoff (how far the prior weight has decayed). |
| `prior_club_matches` | Prior-season matches of the club the player played for last season. |
| `prior_season_weight` | What one prior-season appearance of this player is still worth (1.0 before the season starts, down to 0). |
| `prior_season_scale` | The factor last season's totals were actually pooled at: `prior_season_weight` capped so last season brings at most `RATE_PRIOR_WINDOW` matches of evidence. |
| `appearance_share` | Blended, recency-weighted share of club matches the player was on the pitch for. |
| `start_share` | Blended share of club matches the player started (>= 60 minutes). |
| `ninety_share` | Blended share of club matches the player played in full (90 minutes) — the full-match bonus of scoring `.3` rides on it. |
| `reds_per90`, `own_goals_per90`, `pen_missed_per90`, `pen_saved_per90`, `pen_conceded_per90` | Blended rare-event rates, shrunk towards the role average with `RARE_EVENT_SHRINK_MATCHES` (20) pseudo-matches. |
| `availability_factor` | Multiplier on the appearance share: `0` when marked out or banned, `0.5` when questionable or just back from an injury with a stated return date, `1 - STRAIGHT_RED_SECOND_MATCH_SHARE` for the second match after a straight red, else `1`. |
| `club_matches_available` | Club matches the player was eligible for this season: his old club's up to a within-season transfer, his new club's after it. |
| `p_appearance` | Probability of playing the fixture — the appearance share above times `availability_factor`, lifted by ownership when `OWNERSHIP_APPEARANCE_WEIGHT` is set; `0.0` when unavailable. |
| `expected_minutes` | `p_appearance` x blended mean minutes when appearing. |
| `club_attack`, `club_defense` | Club goals scored/conceded per match at the fixture venue, blended across seasons. |
| `opponent_attack`, `opponent_defense` | Opponent goals scored/conceded per match at their venue. |
| `has_history` | `true` when at least one appearance exists in either season. |
| `stat_source` | `current_season` once the player has played this season; `parallel_league` when his only play this season is in his national league (cup target, step 24); otherwise `prior_season`. |
| `parallel_appearances` | Appearances this season in the parallel leagues before the cutoff, unweighted (0 outside a cup target). |
| `parallel_club_matches` | The player's league club's matches this season before the cutoff, unweighted. |
| `parallel_weight` | What one parallel-league observation is worth against one of the cup's own (`PARALLEL_WEIGHT`); 0 without a parallel layer. |
| `league_factor` | The `LEAGUE_STRENGTH` factor the league's goals were translated by; 1.0 without one. |
| `availability_source` | `null` when the status is the season's own; the slug of the league whose snapshot lent an out/doubtful status. |
| `sources` | Every layer of the history — the cup's current and prior season, then each league's — with appearances, club matches, weight and goal factor. |
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
