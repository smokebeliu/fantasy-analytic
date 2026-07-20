"""GraphQL operations used by the discovery prototype."""

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
