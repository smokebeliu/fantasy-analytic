"""GraphQL operations used by the discovery prototype."""

# Every fantasy tournament Sports.ru offers, with the seasons each one exposes.
# This is the only operation that answers "which leagues can we import?" — the
# tournament endpoint itself has to be addressed by a slug that is already known.
TOURNAMENTS_QUERY = """
query DiscoverTournaments {
  fantasyQueries {
    tournamentsList {
      id
      name
      webName
      seasons {
        id
        isActive
        statObject {
          id
          name
        }
      }
      currentSeason {
        id
        isActive
        statObject {
          id
          name
        }
        currentTour {
          id
          name
          status
        }
      }
    }
  }
}
"""

TOURNAMENT_QUERY = """
query DiscoverTournament($id: ID!) {
  fantasyQueries {
    tournament(source: HRU, id: $id) {
      id
      name
      webName
      seasons {
        id
        isActive
        statObject {
          id
          name
        }
      }
      currentSeason {
        id
        isActive
        statObject {
          id
          name
        }
        currentTour {
          id
          name
          status
        }
      }
    }
  }
}
"""

SEASON_QUERY = """
query DiscoverSeason($seasonID: ID!) {
  fantasyQueries {
    season(input: {seasonID: $seasonID}) {
      id
      isActive
      rules
      statObject {
        id
        name
      }
      info {
        playerPrices
        constraints {
          totalBalance
          totalPlayersCount
          activePlayersCount
          fullRoster {
            role
            minCount
            maxCount
          }
          startingRoster {
            role
            minCount
            maxCount
          }
        }
        teams {
          id
          name
          statObject {
            id
            name
          }
        }
      }
      tours {
        id
        name
        status
        startedAt
        finishedAt
        transfersStartedAt
        transfersFinishedAt
        constraints {
          totalTransfers
          maxSameTeamPlayers
        }
        matches {
          id
          scheduledAt
          matchStatus
          home {
            score
            team {
              id
              name
            }
          }
          away {
            score
            team {
              id
              name
            }
          }
        }
      }
    }
  }
}
"""

PLAYERS_QUERY = """
query DiscoverPlayers(
  $seasonID: ID!
  $pageNum: Int!
  $pageSize: Int!
) {
  fantasyQueries {
    players(
      input: {
        seasonID: $seasonID
        pageNum: $pageNum
        pageSize: $pageSize
        sortType: BY_POINTS
        sortOrder: DESC
      }
    ) {
      pageInfo {
        currentPage
        firstPage
        lastPage
        totalCount
        hasNextPage
      }
      list {
        id
        name
        price
        role
        statObject {
          id
          name
        }
        team {
          id
          name
          statObject {
            id
            name
          }
        }
        status {
          status
          description
          selectedBy
          form
        }
        seasonScoreInfo {
          place
          score
          averageScore
          scoreForLastTour
          topPercent
        }
        gameStat {
          points
          goals
          assists
          saves
          penaltiesMissed
          penaltiesPost
          penaltiesTarget
          penaltiesSaved
          fieldMinutes
          yellowCards
          redCards
          goalsConceded
          penaltyGoalsConceded
          penaltiesFaced
          penaltyConceded
          ownGoals
          ballRecovery
        }
      }
    }
  }
}
"""

PLAYER_HISTORY_QUERY = """
query DiscoverPlayerHistory(
  $seasonID: ID!
  $playerID: ID!
  $pageNum: Int!
  $pageSize: Int!
) {
  fantasyQueries {
    season(
      input: {
        seasonID: $seasonID
        paginationPlayers: {
          playerID: $playerID
          pageNum: 1
          pageSize: 1
        }
      }
    ) {
      players {
        list {
          player {
            id
            name
            price
            role
          }
          matches(
            input: {
              pageNum: $pageNum
              pageSize: $pageSize
              isFinished: true
              sortOrder: ASC
            }
          ) {
            pageInfo {
              currentPage
              lastPage
              totalCount
              hasNextPage
            }
            matches {
              match {
                id
                scheduledAt
              }
              team {
                id
                name
                statObject {
                  id
                  name
                }
              }
              tour {
                id
                name
                status
              }
              playerMatchInfo {
                points
                goals
                assists
                saves
                penaltiesMissed
                penaltiesPost
                penaltiesTarget
                penaltiesSaved
                fieldMinutes
                yellowCards
                redCards
                goalsConceded
                penaltyGoalsConceded
                penaltiesFaced
                penaltyConceded
                ownGoals
                ballRecovery
              }
              statDetails {
                score
                reason
              }
            }
          }
        }
      }
    }
  }
}
"""

# Candidate team-level match stat fields exposed by `statTeamMatchStat`.
# The discovery spike measures how reliably each one is populated for RPL.
MATCH_TEAM_STAT_FIELDS = (
    "shotsTotal",
    "shotsOnTarget",
    "shotsOffTarget",
    "shotsBlocked",
    "shotsSaved",
    "ballPossession",
    "cornerKicks",
    "offsides",
    "fouls",
    "freeKicks",
    "goalKicks",
    "throwIns",
    "yellowCards",
    "yellowRedCards",
    "totalRedCards",
    "ownGoals",
    "penaltyScored",
    "penaltiesMissed",
    "substitutions",
    "injuries",
)

# Candidate per-player match stat fields exposed by `statPlayerMatchStat`.
MATCH_PLAYER_STAT_FIELDS = (
    "minutesPlayed",
    "performanceScore",
    "goalsScored",
    "goalsByHead",
    "goalsByPenalty",
    "assists",
    "fantasyAssists",
    "ownGoals",
    "shotsOnGoal",
    "shotsOffGoal",
    "shotsBlocked",
    "goalAttempts",
    "chancesCreated",
    "crossesTotal",
    "crossesSuccessful",
    "passesShortTotal",
    "passesShortSuccessful",
    "passesMediumTotal",
    "passesMediumSuccessful",
    "passesLongTotal",
    "passesLongSuccessful",
    "passesForward",
    "passesForwardAccurate",
    "passesBack",
    "passesBackAccurate",
    "actions",
    "actionSuccessful",
    "actionSuccessPercent",
    "duelsHeaderTotal",
    "duelsHeaderSuccessful",
    "duelsTackleTotal",
    "duelsTackleSuccessful",
    "duelsSprintTotal",
    "duelsSprintSuccessful",
    "interceptions",
    "ballRecovery",
    "goalLineClearances",
    "foulsCommitted",
    "wasFouled",
    "offsides",
    "yellowCards",
    "yellowRedCards",
    "redCards",
    "goalsConceded",
    "shotsFacedTotal",
    "shotsFacedSaved",
    "penaltiesFaced",
    "penaltiesSaved",
    "penaltiesMissed",
    "penaltyWon",
    "penaltyConceded",
    "xG",
    "xA",
    "xGPS",
    "possLostInOwnHalf",
    "badBallControl",
    "mistakes",
    "goalMistakes",
)


def _indent(fields: tuple[str, ...], spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{field}" for field in fields)


def build_match_stats_query() -> str:
    """Build the extended match-statistics discovery query.

    It resolves a single match through `statQueries.football.match` and
    requests the availability flags, team-level stats, per-player lineup
    stats and the event timeline so field coverage can be measured.
    """
    team_stat = _indent(MATCH_TEAM_STAT_FIELDS, 12)
    player_stat = _indent(MATCH_PLAYER_STAT_FIELDS, 16)
    side = f"""{{
      team {{ id name abbreviation }}
      score
      xG
      formation {{ code }}
      manager {{ id name }}
      stat {{
{team_stat}
      }}
      lineup(skipPreview: false) {{
        player {{ id name }}
        jerseyNumber
        position
        lineupOrder
        lineupStarting
        lineupCurrent
        isCaptain
        type
        mark
        stat {{
{player_stat}
        }}
      }}
    }}"""
    return f"""
query DiscoverMatchStats($id: ID!, $source: statSourceList) {{
  statQueries {{
    football {{
      match(id: $id, source: $source) {{
        id
        matchStatus
        scheduledAt
        attendance
        hasDetailStat
        hasLineups
        hasEvents
        hasPersonStat
        hasXG
        venue {{ id name }}
        home {side}
        away {side}
        events {{
          id
          time
          unix_time
          type
          outcome
          team
        }}
      }}
    }}
  }}
}}
"""


MATCH_STATS_QUERY = build_match_stats_query()


TEAM_STAT_FIELDS = """
MatchesPlayed
MatchesWon
MatchesDrawn
MatchesLost
GoalsScored
GoalsConceded
CupRank
GroupPosition
GroupName
YellowCards
RedCards
"""


def build_team_stats_query(team_count: int) -> str:
    """Build one stat query with a typed variable and alias per team."""
    if team_count < 1:
        raise ValueError("At least one team is required")

    variables = ", ".join(
        ["$seasonID: [String!]!"]
        + [f"$team{index}: ID!" for index in range(team_count)]
    )
    fields = "\n".join(
        (
            f"team{index}: stats(id: $team{index}, source: SPORTS_HUB) {{"
            f"\n{TEAM_STAT_FIELDS}\n}}"
        )
        for index in range(team_count)
    )
    return f"""
query DiscoverTeamStats({variables}) {{
  stat_season(id: $seasonID, source: SPORTS_HUB) {{
    id
    name
    startedAt
    endedAt
    {fields}
  }}
}}
"""
