# Extended match-statistics coverage

Reproduce with `fantasy-match-stats --season-name 2025/2026 --sample-size 40`
(after `fantasy-migrate upgrade` and `fantasy-ingest`). Raw fixtures are written
to `data/match-stats/raw/` and this table is `data/match-stats/field-coverage.md`.

Generated: 2026-07-21T10:15:03.065055+00:00

Operation: `statQueries.football.match` (source: `None`).

Sample: 40 matches, 16 clubs, 30 tours.

## Match-level availability

| Flag | Matches |
| --- | --- |
| `has_detail_stat` | 40/40 |
| `has_lineups` | 40/40 |
| `has_events` | 40/40 |
| `has_person_stat` | 40/40 |
| `has_xg` | 10/40 |
| `score_matches_catalog` | 40/40 |
| `eleven_home_starters` | 40/40 |
| `eleven_away_starters` | 40/40 |

## Field coverage

| GraphQL path | Type | Fill | Non-null/observed | Decision |
| --- | --- | --- | --- | --- |
| `attendance` | int | 100% | 40/40 | ADOPT |
| `events[].id` | string | 100% | 860/860 | ADOPT |
| `events[].outcome` | string | 100% | 860/860 | ADOPT |
| `events[].team` | string | 77% | 660/860 | CONDITIONAL |
| `events[].time` | string | 100% | 860/860 | ADOPT |
| `events[].type` | string | 100% | 860/860 | ADOPT |
| `events[].unix_time` | int | 100% | 860/860 | ADOPT |
| `hasDetailStat` | boolean | 100% | 40/40 | ADOPT |
| `hasEvents` | boolean | 100% | 40/40 | ADOPT |
| `hasLineups` | boolean | 100% | 40/40 | ADOPT |
| `hasPersonStat` | boolean | 100% | 40/40 | ADOPT |
| `hasXG` | boolean | 100% | 40/40 | ADOPT |
| `id` | string | 100% | 40/40 | ADOPT |
| `matchStatus` | string | 100% | 40/40 | ADOPT |
| `scheduledAt` | string | 100% | 40/40 | ADOPT |
| `side.formation.code` | string | 100% | 80/80 | ADOPT |
| `side.lineup[].isCaptain` | boolean | 100% | 1791/1791 | ADOPT |
| `side.lineup[].jerseyNumber` | string | 100% | 1791/1791 | ADOPT |
| `side.lineup[].lineupCurrent` | boolean | 100% | 1791/1791 | ADOPT |
| `side.lineup[].lineupOrder` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].lineupStarting` | boolean | 100% | 1791/1791 | ADOPT |
| `side.lineup[].mark` | int | 62% | 1105/1791 | CONDITIONAL |
| `side.lineup[].player.id` | string | 100% | 1791/1791 | ADOPT |
| `side.lineup[].player.name` | string | 100% | 1791/1791 | ADOPT |
| `side.lineup[].position` | string | 49% | 880/1791 | SPARSE |
| `side.lineup[].stat.actionSuccessPercent` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.actionSuccessful` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.actions` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.assists` | int | 4% | 74/1791 | SPARSE |
| `side.lineup[].stat.badBallControl` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.ballRecovery` | int | 56% | 1005/1791 | CONDITIONAL |
| `side.lineup[].stat.chancesCreated` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].stat.crossesSuccessful` | int | 13% | 227/1791 | SPARSE |
| `side.lineup[].stat.crossesTotal` | int | 28% | 509/1791 | SPARSE |
| `side.lineup[].stat.duelsHeaderSuccessful` | int | 36% | 650/1791 | SPARSE |
| `side.lineup[].stat.duelsHeaderTotal` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.duelsSprintSuccessful` | int | 19% | 349/1791 | SPARSE |
| `side.lineup[].stat.duelsSprintTotal` | int | 32% | 567/1791 | SPARSE |
| `side.lineup[].stat.duelsTackleSuccessful` | int | 28% | 497/1791 | SPARSE |
| `side.lineup[].stat.duelsTackleTotal` | int | 36% | 640/1791 | SPARSE |
| `side.lineup[].stat.fantasyAssists` | int | 1% | 21/1791 | SPARSE |
| `side.lineup[].stat.foulsCommitted` | int | 36% | 650/1791 | SPARSE |
| `side.lineup[].stat.goalAttempts` | int | 30% | 544/1791 | SPARSE |
| `side.lineup[].stat.goalLineClearances` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.goalMistakes` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.goalsByHead` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.goalsByPenalty` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.goalsConceded` | int | 40% | 717/1791 | SPARSE |
| `side.lineup[].stat.goalsScored` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].stat.interceptions` | int | 25% | 454/1791 | SPARSE |
| `side.lineup[].stat.minutesPlayed` | int | 68% | 1220/1791 | CONDITIONAL |
| `side.lineup[].stat.mistakes` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.offsides` | int | 7% | 118/1791 | SPARSE |
| `side.lineup[].stat.ownGoals` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].stat.passesBack` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesBackAccurate` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesForward` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesForwardAccurate` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesLongSuccessful` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesLongTotal` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesMediumSuccessful` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesMediumTotal` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesShortSuccessful` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.passesShortTotal` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.penaltiesFaced` | int | 1% | 14/1791 | SPARSE |
| `side.lineup[].stat.penaltiesMissed` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.penaltiesSaved` | int | 0% | 3/1791 | SPARSE |
| `side.lineup[].stat.penaltyConceded` | int | 1% | 14/1791 | SPARSE |
| `side.lineup[].stat.penaltyWon` | int | 1% | 12/1791 | SPARSE |
| `side.lineup[].stat.performanceScore` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.possLostInOwnHalf` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.redCards` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].stat.shotsBlocked` | int | 12% | 215/1791 | SPARSE |
| `side.lineup[].stat.shotsFacedSaved` | int | 4% | 73/1791 | SPARSE |
| `side.lineup[].stat.shotsFacedTotal` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.shotsOffGoal` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.shotsOnGoal` | int | 15% | 264/1791 | SPARSE |
| `side.lineup[].stat.wasFouled` | int | 34% | 604/1791 | SPARSE |
| `side.lineup[].stat.xA` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.xG` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.xGPS` | null | 0% | 0/1791 | EXCLUDE |
| `side.lineup[].stat.yellowCards` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].stat.yellowRedCards` | int | 100% | 1791/1791 | ADOPT |
| `side.lineup[].type` | string | 100% | 1791/1791 | ADOPT |
| `side.manager.id` | string | 100% | 80/80 | ADOPT |
| `side.manager.name` | string | 100% | 80/80 | ADOPT |
| `side.score` | int | 100% | 80/80 | ADOPT |
| `side.stat.ballPossession` | int | 100% | 80/80 | ADOPT |
| `side.stat.cornerKicks` | int | 99% | 79/80 | ADOPT |
| `side.stat.fouls` | int | 100% | 80/80 | ADOPT |
| `side.stat.freeKicks` | int | 100% | 80/80 | ADOPT |
| `side.stat.goalKicks` | int | 100% | 80/80 | ADOPT |
| `side.stat.injuries` | null | 0% | 0/80 | EXCLUDE |
| `side.stat.offsides` | int | 81% | 65/80 | CONDITIONAL |
| `side.stat.ownGoals` | int | 2% | 2/80 | SPARSE |
| `side.stat.penaltiesMissed` | int | 5% | 4/80 | SPARSE |
| `side.stat.penaltyScored` | int | 100% | 80/80 | ADOPT |
| `side.stat.shotsBlocked` | int | 94% | 75/80 | ADOPT |
| `side.stat.shotsOffTarget` | int | 96% | 77/80 | ADOPT |
| `side.stat.shotsOnTarget` | int | 98% | 78/80 | ADOPT |
| `side.stat.shotsSaved` | int | 91% | 73/80 | ADOPT |
| `side.stat.shotsTotal` | int | 100% | 80/80 | ADOPT |
| `side.stat.substitutions` | int | 100% | 80/80 | ADOPT |
| `side.stat.throwIns` | int | 100% | 80/80 | ADOPT |
| `side.stat.totalRedCards` | int | 10% | 8/80 | SPARSE |
| `side.stat.yellowCards` | int | 89% | 71/80 | CONDITIONAL |
| `side.stat.yellowRedCards` | int | 5% | 4/80 | SPARSE |
| `side.team.abbreviation` | string | 100% | 80/80 | ADOPT |
| `side.team.id` | string | 100% | 80/80 | ADOPT |
| `side.team.name` | string | 100% | 80/80 | ADOPT |
| `side.xG` | float | 25% | 20/80 | SPARSE |
| `venue.id` | string | 100% | 40/40 | ADOPT |
| `venue.name` | string | 100% | 40/40 | ADOPT |

## Findings

- statQueries.football.match(id) returns a statMatch by stat_match_id without needing a source argument.
- xG is only partially available (10/40 matches expose hasXG); treat team/player xG as optional, never required.
- 50 of 113 measured fields are reliably populated (>= 90% fill) and safe to adopt.
- 57 measured fields are missing or too sparse to depend on.
- Identifier linkage: side.team.id is the stat team slug (clubs.stat_team_id) and side.lineup[].player.id is the stat player slug (players.stat_player_id); both join the extended stats back to the imported catalog.
- Reliable team metrics: shotsTotal, shotsOnTarget, shotsOffTarget, ballPossession, cornerKicks, fouls and substitutions. Reliable player metrics are limited to goalsScored, cards, chancesCreated and ownGoals; passing, duels and xG breakdowns are too sparse.
