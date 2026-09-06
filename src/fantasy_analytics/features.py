"""Analytical feature engineering (development-plan step 6).

This module turns the *active* snapshot published by the quality gate (step 4)
into a reproducible, leakage-free dataset that later steps use to predict the
fantasy points a player will score in an upcoming tour.

Design guarantees
-----------------

* **Reproducible.** Every dataset is keyed to a single ingestion run (the active
  snapshot of a season by default) and a feature-schema version, so rebuilding
  it from the same run yields identical rows.
* **Leakage-free.** Every feature is computed strictly from matches that kicked
  off *before* the target tour's transfer deadline (the ``cutoff``). Matches
  belonging to the target tour itself are excluded from the history set as a
  second line of defence, even if a tour's deadline is mis-dated in the source.
* **Explicit fills.** Missing values follow a documented strategy (see
  ``FEATURE_DICTIONARY`` and ``docs/feature-dictionary.md``) rather than leaking
  ``NaN`` into consumers.
* **Two seasons, one history.** Last season is blended into every tour rather
  than only backfilling the opening one, at a weight that halves every
  ``PRIOR_SEASON_HALF_LIFE`` matches of the new season. The current season is
  never discounted, so it takes over as soon as it has anything to say, and a
  league's best players do not spend August looking like they have never played.
* **A tour is a slice of the calendar, not a round.** Fantasy tours cannot
  overlap, so Sports.ru re-attaches a postponed match to whichever tour its new
  date falls in. A club can therefore play *twice* in one tour and not at all in
  another, and both cases are carried through: every one of a club's matches in
  the target tour is reported in ``tour_fixtures``.

The module only reads domain data; it never calls the Sports.ru API and never
writes to the database. The final ML algorithm is deliberately out of scope
(step 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import session_scope
from .db.models import (
    ClubMatchStats,
    Competition,
    FantasyPlayerSnapshot,
    FantasyTour,
    Match,
    Player,
    PlayerMatchStats,
    PlayerSeason,
    Season,
    SeasonClub,
)

# Bumped whenever the feature set or its computation changes so that datasets
# built by different code revisions never get silently mixed.
# 1.1.0 added the per-90 rates the event-based forecast (step 7) consumes.
# 1.2.0 added cross-season sourcing (step 14): when the target season has not
#   started yet, a player's history is drawn from the prior season by the shared
#   cross-season identity, every row carries a ``stat_source`` label, and
#   newcomers without any prior history get documented role priors.
# 1.3.0 replaced that all-or-nothing switch with a *blend*: last season is
#   always in the history and its weight halves every
#   ``PRIOR_SEASON_HALF_LIFE`` matches of the new one, so the current season
#   takes over as soon as it says anything. The same revision stopped counting a
#   0-minute matchday row as an appearance and estimates the appearance
#   probability from the whole sourced season rather than a five-match window.
# 1.4.0 reports every match a club plays in the target tour (``tour_fixtures``)
#   instead of only the earliest, because a fantasy tour is a slice of the
#   calendar and a postponement can leave a club playing twice inside one.
# 1.5.0 treats a red card as a one-match ban: the player is unavailable for the
#   target tour until his club has played a later match before the cutoff.
# 1.6.0 stops last season outvoting this one. The prior-season *rates* used to
#   enter the pool at ``weight x every match of last season``, so a 30-match
#   season still carried twice the evidence of six new matches half-way through
#   the autumn; now last season is worth at most ``RATE_PRIOR_WINDOW`` matches
#   before the weight is applied. Per-90 rates are additionally shrunk towards
#   the league's role average with ``RATE_SHRINK_MATCHES`` pseudo-matches, so a
#   newcomer's two goals in three games no longer read as a 1.0 goals-per-90
#   striker. The league role priors are pooled from both seasons, so a first
#   imported season also scores its newcomers from something. A *later* season
#   is never used as the prior of an earlier one.
# 1.7.0 (step 23) closes the events the forecast never saw and sharpens the
#   ones it did. ``ninety_share`` (full 90-minute matches) feeds the extra
#   point midfielders and forwards earn for a full match; ``reds_per90``,
#   ``own_goals_per90``, ``pen_missed_per90``, ``pen_saved_per90`` and
#   ``pen_conceded_per90`` price the rare events, shrunk hard towards the
#   role average. The current season's rates decay by ``CURRENT_RATE_DECAY``
#   per match so April weighs more than August, club strengths decay the same
#   way (``CLUB_RECENCY_DECAY``) and are pooled across venues with a league
#   home factor (``STRENGTH_VENUE_MODE``). A player who changed club within
#   the season has his appearance share judged on the matches he was actually
#   available for (``club_id`` on every appearance). A straight red card
#   discounts the *second* match after it as well as ruling out the first, a
#   ban whose end date the snapshot carries is honoured, and a "questionable"
#   status halves the appearance probability instead of zeroing it. Shrinkage
#   anchors can be the average of the player's *price bucket* within his role
#   (``PRICE_BUCKET_PRIORS``) and ownership can lift a low appearance share
#   (``OWNERSHIP_APPEARANCE_WEIGHT``).
# 1.8.0 (step 24) sources a European cup from the national leagues its clubs
#   are playing in at the same time. Sports.ru's stat identities are global,
#   so a Champions League player is the same ``players`` row as his La Liga
#   self; his league appearances (and his club's league results) enter the
#   history as *parallel layers* at ``PARALLEL_WEIGHT``, cut at the target
#   tour's deadline like everything else, with the league's own last season
#   behind them. Goals across leagues are made comparable by
#   ``LEAGUE_STRENGTH``. An injury known only to the league snapshot marks the
#   player out in the cup too; a red card stays inside the competition it was
#   shown in. ``stat_source`` gains ``parallel_league`` and every row lists
#   its ``sources``.
FEATURE_VERSION = "1.8.0"

ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")

# Rolling look-back windows (in appearances) required by the plan.
ROLLING_WINDOWS = (3, 5, 10)

# ``stat_source`` marks where a row's history came from so the frontend can
# visually separate last season's numbers from the ones collected this season.
# A row is labelled ``prior_season`` only while the player has *no* appearance
# in the target season; once he has played, the label follows the season that
# now dominates the blend even though last season is still contributing.
STAT_SOURCE_CURRENT = "current_season"
STAT_SOURCE_PRIOR = "prior_season"
# A player who has not played in the target competition yet but has played
# in his national league this season (step 24).
STAT_SOURCE_PARALLEL = "parallel_league"

# Parallel sourcing (step 24). A European cup is forecast from the national
# leagues its clubs play in at the same time; only the competitions listed
# here are *targets* of that sourcing, so a domestic league's forecast (and its
# backtests) is untouched by a cup being imported next to it.
PARALLEL_TARGET_SLUGS = frozenset({"champions-league", "europa-league"})

# Competitions that are never a parallel *source*: cups, whose own rows are
# the target, and national-team tournaments, whose "clubs" are countries.
# Everything else Sports.ru catalogues is a domestic league.
NON_LEAGUE_SLUGS = frozenset(
    {
        "champions-league",
        "europa-league",
        "world-cup",
        "club-world-cup",
        "european-championship",
        "copa-america",
        "africa-cup",
        "nations-league",
    }
)

# What one national-league observation is worth against one of the cup's own.
# The cup's matches are the thing being predicted, so they always count in
# full; the league is the same players against different opposition, and a
# big club rotates differently in the two. Provisional until the Champions
# League 2025/26 backtest fixes it.
PARALLEL_WEIGHT = 0.7

# How a league's goals translate into the cup's. A club scoring ``g`` per
# match in its league is expected to score ``g x LEAGUE_STRENGTH`` against
# the cup's field and to concede ``c / LEAGUE_STRENGTH``; the same factor
# scales a player's goals and assists (and his saves the other way). Every
# league is below one because the cup's field is stronger than the average
# domestic opponent; the spread between leagues follows the UEFA coefficient
# ranking. Provisional starting values, to be fitted on the 2025/26 backtest.
LEAGUE_STRENGTH: dict[str, float] = {
    "england": 0.92,
    "spain": 0.90,
    "italy": 0.88,
    "germany": 0.88,
    "france": 0.84,
    "portugal": 0.78,
    "netherlands": 0.76,
    "turkey": 0.72,
    "russia": 0.72,
    "belarus": 0.60,
    "kazakhstan": 0.58,
    "championship": 0.66,
    "eliteserien": 0.64,
    "allsvenskan": 0.64,
    "brazil": 0.80,
    "argentina": 0.78,
}
DEFAULT_LEAGUE_STRENGTH = 0.66

# Documented priors applied to a newcomer that has no history in either season
# (step 14). Offensive/defensive per-90 rates default to the league's role
# average, discounted because an unknown player is riskier than a proven one.
#
# The appearance probability is priced (1.6.0). Measured over the opening tour
# of four seasons (RPL and La Liga 2025/26 and 2026/27, 963 newcomers), a
# newcomer at his position's median price plays about one time in four, one
# priced a unit above the median plays more than one time in three, one priced
# a unit below plays one time in six — and only one keeper in fourteen plays at
# all. The old flat one-in-two over-predicted every one of them. Above the
# median the slope is steeper than below it, but a single slope, clamped, is
# close enough and easy to reason about.
NEWCOMER_P_APPEARANCE = 0.25
NEWCOMER_P_APPEARANCE_GOALKEEPER = 0.10
NEWCOMER_PRICE_SLOPE = 0.12
NEWCOMER_P_APPEARANCE_MIN = 0.03
NEWCOMER_P_APPEARANCE_MAX = 0.65
# A newcomer's assumed appearance probability is worth this many matches of
# evidence against his club's actual matches: after one match spent off the
# pitch it counts for less than half, after two for a quarter. The same four
# seasons say a newcomer who missed his club's first match plays the second
# one time in twenty, so the assumption has to give way fast.
NEWCOMER_PRIOR_MATCHES = 1.0
NEWCOMER_RATE_FACTOR = 0.7

# A player is credited with a "start" when they played at least this many
# minutes. Sports.ru does not import an explicit lineup flag, so start share is
# a documented approximation based on minutes played.
START_MINUTES_THRESHOLD = 60

# A "full match": the threshold at which midfielders and forwards earn an
# extra appearance point (scoring ``rpl-2025-2026.3``). Sports.ru caps the
# recorded minutes at 90, so a player who saw the final whistle is at 90.
NINETY_MINUTES_THRESHOLD = 90

# How fast last season stops mattering, in matches of the new one. Every
# ``PRIOR_SEASON_HALF_LIFE`` matches the weight of a prior-season observation
# halves, so last season is the whole story before a ball is kicked, half of it
# after three matches, a quarter after six and a tenth by the tenth. The
# current season is never discounted, which is what makes it win as soon as it
# has anything to say. Shortened from 5 to 3 in 1.6.0: on the 2025/26 RPL and
# La Liga seasons (backtested with 2024/25 as the prior) the shorter half-life
# lowered the whole-pool MAE in both leagues (1.048 -> 1.022 and 1.209 -> 1.190)
# and raised the realised squad points of the RPL; two matches was a step too
# far (squads lost points in both leagues).
PRIOR_SEASON_HALF_LIFE = 3.0

# Below this weight the prior season is not loaded at all: it can no longer move
# a rate by a measurable amount and loading it costs a full extra season scan.
# The weight is judged on how far the *season* has progressed (the median number
# of matches a club has played), not on any one player.
PRIOR_SEASON_MIN_WEIGHT = 0.01

# How many matches of evidence last season may bring to a player's *rates*
# (goals, assists, saves, recoveries, cards per 90). Without a cap the prior
# season enters the pool at its full length, so at ``PRIOR_SEASON_HALF_LIFE``
# matches of the new season — where its weight is one half — a 30-match season
# still outweighs the five fresh matches three to one, and a player whose role
# or club changed over the summer keeps last year's numbers well into the
# autumn. Capped at this many matches, the two seasons meet as equals once the
# new one is ``RATE_PRIOR_WINDOW x weight`` matches old, and the current season
# takes over from there. The window is applied *before* the half-life weight.
# Backtests of 2025/26 could not tell 4, 8 and 12 apart, so the cap is set
# where it makes the intent legible rather than where it scores best.
RATE_PRIOR_WINDOW = 8.0

# Pseudo-matches of the league's role average mixed into every player's per-90
# rates (empirical-Bayes shrinkage). Three games with two goals are not a
# 1.0-goals-per-90 striker; with five pseudo-matches of the average forward's
# rate in the pool they read as well under half that until the sample grows.
# The same pseudo-count tames a keeper's saves and a defender's recoveries
# after a single match, which is where the largest over-predictions of the
# first weeks come from. A player with a full season behind him is barely
# moved. Five beat two and three on every backtest metric in both leagues, and
# it is also what closed most of the gap between the optimizer's projected and
# realised squad points (La Liga 2025/26: +12% optimism down to +5%).
# Raised to eight in 1.7.0 together with the in-season decay below: over the
# four full seasons backtested (RPL and La Liga 2024/25 and 2025/26) eight
# beat five on the played-player MAE in every league and cut the gap between
# the forecast and the fact of the top-25 to within 2% (RPL) and 6% (La Liga),
# at squad points equal within noise; twelve gained nothing more.
RATE_SHRINK_MATCHES = 8.0

# Pseudo-matches for the rare events (red cards, own goals, penalties missed,
# saved and conceded). A red card is one match in a hundred; two of them in a
# season say almost nothing about a player, so his rate is held close to the
# role average for far longer than his goals are.
RARE_EVENT_SHRINK_MATCHES = 20.0

# Recency decay applied to the *current* season's matches when a player's
# per-90 rates are pooled: the most recent appearance weighs 1, the one before
# it ``CURRENT_RATE_DECAY``, and so on. At 1.0 every match of the season
# counts the same, which is what 1.6.0 did. Swept together with
# ``RATE_SHRINK_MATCHES`` on the four full seasons (step 23): 0.95 lowered
# the played-player MAE in both leagues against 1.0 (RPL 1.943 -> 1.938, La
# Liga 1.998 -> 1.995) and improved the rank correlation; 0.9 was no better
# and made the top-25 forecast pessimistic.
CURRENT_RATE_DECAY = 0.95

# The same decay for a club's own results in the match model: a 4-0 in August
# and a 4-0 last week should not describe the club equally in April. Neutral
# over a full season (0.95 and 0.9 moved no metric beyond noise in either
# direction, step 23), so it is left at 1.0.
CLUB_RECENCY_DECAY = 1.0

# How venue enters the club strengths. ``split`` keeps separate home and away
# strengths (each estimated from half the club's matches); ``pooled``
# estimates one attack and one defence per club from every match, normalised
# by the league's home/away factor, and multiplies the factor back in at the
# fixture venue — twice the sample for the same information. Pooled won the
# step-23 sweep: a higher rank correlation in every league and, on RPL
# 2024/25, 80 more realised squad points over the season.
STRENGTH_VENUE_MODE = "pooled"

# Shrinkage anchors by price bucket. With ``PRICE_BUCKET_PRIORS`` the per-90
# rates are shrunk towards the average of the player's price tercile within
# his role rather than the whole role: a 4.5 midfielder and a 10.0 midfielder
# are not drawn from the same population. A bucket needs at least
# ``PRICE_BUCKET_MIN_MINUTES`` of play to be used; otherwise the role average
# stands in. Prices in a backtest are the snapshot's (end-of-season) prices,
# so the effect measured there is an upper bound (see the step-23 card).
PRICE_BUCKET_PRIORS = False
PRICE_BUCKETS = 3
PRICE_BUCKET_MIN_MINUTES = 3000.0

# Ownership as evidence that a player features: a player picked by
# ``OWNERSHIP_FULL_PERCENT`` percent of managers is, in practice, a starter.
# The implied probability only ever *raises* the history-based share, and only
# at ``OWNERSHIP_APPEARANCE_WEIGHT``; at 0 ownership is ignored. Ownership is
# a point-in-time snapshot value, so it cannot be validated on a historical
# tour without looking into the future — the default is set from the current
# seasons only and kept small.
OWNERSHIP_APPEARANCE_WEIGHT = 0.0
OWNERSHIP_FULL_PERCENT = 15.0

# A straight red card is served in the next match for certain; roughly a third
# of them (the ones for violent conduct) run to a second match. Second yellows
# are one match. Measured over 2024/25 and 2025/26 in both leagues (step 23).
STRAIGHT_RED_SECOND_MATCH_SHARE = 0.35

# A player marked out whose stated return date has passed, or whose status is
# "questionable", plays at this share of his usual appearance probability.
QUESTIONABLE_APPEARANCE_FACTOR = 0.5

# The same two ideas for the *club* strengths the match model runs on. Last
# season's results are admitted for at most ``STRENGTH_PRIOR_WINDOW`` matches
# per club before the half-life weight (a full season at weight one half used
# to bring nineteen matches against three fresh ones, so a side whose defence
# fell apart over the summer kept last year's clean-sheet odds into October).
# Every venue strength is then shrunk towards the league's venue average with
# ``STRENGTH_SHRINK_MATCHES`` pseudo-matches: strengths are kept per venue, so
# three matches into a season a promoted club is described by one or two home
# results, and one 3-0 should not make it the league's best attack. Over a
# whole backtested season neither knob moves any metric (the opening weeks are
# too small a share of it), so they are set for the opening weeks, where the
# next-tour forecast is what people actually look at.
STRENGTH_PRIOR_WINDOW = 12.0
STRENGTH_SHRINK_MATCHES = 3.0

# How many matches of evidence each season may contribute when the two are
# blended into a *share* (appearance and start shares, and the appearance
# probability built from them). Shares cannot be pooled the way rates can: a
# finished season brings 38 matches and the new one brings two, so pooling by
# observation count would let last September outvote everything that has
# happened since. Capping both sides at the same number of effective matches
# makes the blend a straight tug-of-war between the two seasons' rates, decided
# by how much of the new season there is and by the prior weight above.
SHARE_BLEND_WINDOW = 5

# Recency decay inside the *current* season when estimating whether a player
# features: the club's most recent match counts 1, the one before it 0.85, and
# so on. An absence three matches ago says much more about the coming weekend
# than one in August. Last season gets no such decay (``1.0``): whether a player
# was dropped in April or in October is equally uninformative about a match
# three months after the season ended, and decaying it would hand the whole
# prior weight to the handful of dead rubbers a fit player is routinely rested
# for.
CURRENT_RECENCY_DECAY = 0.85
PRIOR_RECENCY_DECAY = 1.0

# Fantasy availability statuses that make a player unavailable for the tour.
# Everything else (``FIERY``, ``UNKNOWN`` and any not-yet-seen value) is treated
# as available, so a new status never silently zeroes a player out.
UNAVAILABLE_STATUSES = frozenset(
    {
        "INJURY",
        "INJURED",
        "DISQUALIFICATION",
        "DISQUALIFIED",
        "SUSPENDED",
        "SUSPENSION",
        "OUT",
        "LEFT",
    }
)

# Statuses that mean "may or may not play" rather than "out". Sports.ru's two
# imported leagues only use FIERY / INJURY / DISQUALIFICATION / UNKNOWN today,
# so this set is a documented hook for a status the source may add.
QUESTIONABLE_STATUSES = frozenset({"QUESTIONABLE", "DOUBTFUL", "FIFTY_FIFTY"})

# Bans (and only bans) end on the date the snapshot describes: a player whose
# disqualification has run out by the fixture is simply available. An injury
# with a past return date is treated as questionable.
BAN_STATUSES = frozenset({"DISQUALIFICATION", "DISQUALIFIED", "SUSPENDED", "SUSPENSION"})


class FeaturesError(RuntimeError):
    """Raised when a run, season or target tour cannot be resolved."""


@dataclass(frozen=True)
class Appearance:
    """One player-match observation used to build rolling features."""

    match_id: int
    scheduled_at: datetime
    minutes: int
    points: int
    goals: int
    assists: int
    saves: int = 0
    ball_recoveries: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    own_goals: int = 0
    penalties_missed: int = 0  # missed, hit the post or saved: all cost the same
    penalties_saved: int = 0
    penalty_conceded: int = 0
    # The club the player was registered with for this match; ``None`` when
    # the source did not say. A within-season transfer shows up as a change.
    club_id: int | None = None

    @property
    def played(self) -> bool:
        """True when the player was actually on the pitch.

        Sports.ru returns a row for every named matchday squad member, so an
        unused substitute arrives as a 0-minute, 0-point row. Counting those as
        appearances makes a permanent bench player look ever-present while
        halving his mean minutes, which is exactly backwards.
        """
        return self.minutes > 0


@dataclass(frozen=True)
class ClubMatch:
    """One finished club match used to derive strength and rest features."""

    match_id: int
    scheduled_at: datetime
    is_home: bool
    goals_scored: int
    goals_conceded: int


@dataclass(frozen=True)
class Fixture:
    """A single upcoming match a club plays in the target tour."""

    match_id: int
    scheduled_at: datetime
    club_id: int
    opponent_club_id: int
    is_home: bool


@dataclass(frozen=True)
class RolePrior:
    """Position-based per-90 priors used for newcomers (step 14)."""

    goals_per90: float
    assists_per90: float
    saves_per90: float
    recoveries_per90: float
    yellows_per90: float
    mean_minutes: float
    points_per90: float = 0.0
    reds_per90: float = 0.0
    own_goals_per90: float = 0.0
    pen_missed_per90: float = 0.0
    pen_saved_per90: float = 0.0
    pen_conceded_per90: float = 0.0
    # Share of the role's starts (>= 60 minutes) that ran the full 90.
    ninety_of_starts: float = 0.0

    def rate(self, key: str) -> float:
        """The per-90 rate for an event key of :func:`_event_totals`."""
        return float(getattr(self, f"{key}_per90", 0.0) or 0.0)


# Events whose rates are shrunk with the ordinary pseudo-count, and the rare
# ones that get the heavy one.
RATE_KEYS = ("points", "goals", "assists", "saves", "recoveries", "yellows")
RARE_RATE_KEYS = ("reds", "own_goals", "pen_missed", "pen_saved", "pen_conceded")


@dataclass(frozen=True)
class PriorPlayer:
    """A player's prior-season identity, resolved by the shared ``player_id``."""

    player_season_id: int
    club_id: int | None
    role: str


@dataclass(frozen=True)
class PriorContext:
    """Everything needed to source a target-tour row from the prior season."""

    run_id: int
    season_id: int
    appearances: dict[int, list[Appearance]]
    club_matches: dict[int, list[ClubMatch]]
    by_player_id: dict[int, PriorPlayer]
    role_priors: dict[str, RolePrior]
    season_name: str = ""


@dataclass(frozen=True)
class HistoryLayer:
    """One extra slice of a player's (or club's) history from another competition.

    A layer is what one national-league season says about a cup row: the
    player's appearances there, his club's results there, how much each
    observation counts (``weight``), how the league's goals translate into the
    cup's (``factor``) and whether it is the league's current season or the one
    before it (``is_prior``, which then decays like the cup's own last season).
    """

    source: str
    season_name: str
    appearances: list[Appearance]
    club_matches: list[ClubMatch]
    weight: float
    factor: float
    is_prior: bool


@dataclass(frozen=True)
class ParallelContext:
    """A national league running alongside the target cup season (step 24)."""

    run_id: int
    season_id: int
    competition_slug: str
    competition_name: str
    season_name: str
    factor: float
    appearances: dict[int, list[Appearance]]
    club_matches: dict[int, list[ClubMatch]]
    by_player_id: dict[int, PriorPlayer]
    snapshots: dict[int, dict[str, Any]]
    prior: PriorContext | None


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in isolation).
# ---------------------------------------------------------------------------


def league_strength(slug: str | None) -> float:
    """The goal-translation factor of a league (see :data:`LEAGUE_STRENGTH`)."""
    if slug is None:
        return DEFAULT_LEAGUE_STRENGTH
    return float(LEAGUE_STRENGTH.get(slug, DEFAULT_LEAGUE_STRENGTH))


def scale_club_match(match: ClubMatch, factor: float) -> ClubMatch:
    """A league result expressed in the cup's goals: scored x factor, conceded / factor."""
    if factor == 1.0 or factor <= 0:
        return match
    return ClubMatch(
        match_id=match.match_id,
        scheduled_at=match.scheduled_at,
        is_home=match.is_home,
        goals_scored=round(match.goals_scored * factor, 4),
        goals_conceded=round(match.goals_conceded / factor, 4),
    )


def parallel_season_overlaps(
    starts_at: datetime | None,
    ends_at: datetime | None,
    *,
    target_starts_at: datetime | None,
    cutoff: datetime,
) -> bool:
    """True when a league season runs alongside the target one at the cutoff.

    A season that has not started by the cutoff is the future; one that ended
    before the target season began is last year's, which the league's own
    prior-season mechanism covers. Unknown dates are taken as overlapping.
    """
    if starts_at is not None and starts_at > cutoff:
        return False
    if ends_at is not None and target_starts_at is not None and ends_at < target_starts_at:
        return False
    return True


def _informative_status(snapshot: dict[str, Any] | None) -> bool:
    """Whether a snapshot status says anything (out or doubtful) about playing."""
    if not snapshot:
        return False
    key = (snapshot.get("availability_status") or "").upper()
    return key in UNAVAILABLE_STATUSES or key in QUESTIONABLE_STATUSES


def merge_availability(
    own: dict[str, Any] | None,
    parallel: Sequence[tuple[str, dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """Let a league snapshot mark a player out when the cup's does not.

    The cup snapshot says ``UNKNOWN`` about everyone before its first tour;
    the league snapshot, refreshed nightly, knows who is injured. An
    informative status on the cup's own snapshot always wins; otherwise the
    first league that reports the player out or doubtful lends its status (and
    its description, which may carry the return date). Price, ownership and
    form always stay the cup's own.
    """
    if _informative_status(own):
        return own
    for slug, snapshot in parallel:
        if _informative_status(snapshot):
            merged = dict(
                own
                or {
                    "availability_status": None,
                    "status_description": None,
                    "price": None,
                    "selected_by": None,
                    "form": None,
                }
            )
            merged["availability_status"] = snapshot["availability_status"]
            merged["status_description"] = snapshot.get("status_description")
            merged["availability_source"] = slug
            return merged
    return own


def recent_before_cutoff(
    appearances: list[Appearance],
    cutoff: datetime,
    *,
    exclude_match_ids: frozenset[int] = frozenset(),
) -> list[Appearance]:
    """Return appearances strictly before ``cutoff``, most recent first.

    ``exclude_match_ids`` drops matches that belong to the target tour so they
    can never leak into the history even when a tour deadline is mis-dated.
    """
    kept = [
        appearance
        for appearance in appearances
        if appearance.scheduled_at < cutoff
        and appearance.match_id not in exclude_match_ids
    ]
    kept.sort(key=lambda a: a.scheduled_at, reverse=True)
    return kept


def _mean(values: list[float | int]) -> float:
    return sum(values) / len(values) if values else 0.0


def played_before_cutoff(
    appearances: list[Appearance],
    cutoff: datetime,
    *,
    exclude_match_ids: frozenset[int] = frozenset(),
) -> list[Appearance]:
    """:func:`recent_before_cutoff` restricted to matches actually played."""
    return [
        appearance
        for appearance in recent_before_cutoff(
            appearances, cutoff, exclude_match_ids=exclude_match_ids
        )
        if appearance.played
    ]


def pending_red_card_suspension(
    appearances: Sequence[Appearance],
    club_matches: Sequence[ClubMatch],
) -> bool:
    """True when a red card has not yet been served by a later club match.

    A red card costs the next match, which for a one-match fantasy tour is the
    next tour. If the club already played after the sending-off — a double
    gameweek, or any later fixture before the cutoff — the ban is served and
    the player is available again. The snapshot ``DISQUALIFICATION`` status is
    not used here: it is a point-in-time flag from the latest import, so it
    cannot tell a historical tour whether the player was banned *then*.
    """
    reds = [item for item in appearances if item.red_cards > 0]
    if not reds:
        return False
    last_red = max(reds, key=lambda item: (item.scheduled_at, item.match_id))
    return not any(
        match.scheduled_at > last_red.scheduled_at
        or (
            match.scheduled_at == last_red.scheduled_at
            and match.match_id != last_red.match_id
        )
        for match in club_matches
    )


def red_card_ban(
    appearances: Sequence[Appearance],
    club_matches: Sequence[ClubMatch],
) -> tuple[int, bool] | None:
    """Describe the most recent red card: ``(club matches since, straight)``.

    ``None`` when there is no red card. A second yellow (a red on top of a
    yellow in the same match) is a one-match ban; a straight red is served in
    the next match for certain and, about a third of the time, in the one
    after as well (:data:`STRAIGHT_RED_SECOND_MATCH_SHARE`). The club matches
    are the ones played before the cutoff, so "matches since" is how much of
    the ban has already been served.
    """
    reds = [item for item in appearances if item.red_cards > 0]
    if not reds:
        return None
    last_red = max(reds, key=lambda item: (item.scheduled_at, item.match_id))
    since = sum(
        1
        for match in club_matches
        if match.scheduled_at > last_red.scheduled_at
        or (
            match.scheduled_at == last_red.scheduled_at
            and match.match_id != last_red.match_id
        )
    )
    return since, last_red.yellow_cards == 0


def red_card_availability_factor(
    appearances: Sequence[Appearance],
    club_matches: Sequence[ClubMatch],
) -> float:
    """What a red card leaves of the appearance probability for the next match.

    0 while the ban is certainly still running (the next match after the
    sending-off), ``1 - STRAIGHT_RED_SECOND_MATCH_SHARE`` for the second match
    after a straight red, 1 otherwise.
    """
    ban = red_card_ban(appearances, club_matches)
    if ban is None:
        return 1.0
    since, straight = ban
    if since == 0:
        return 0.0
    if straight and since == 1:
        return round(1.0 - STRAIGHT_RED_SECOND_MATCH_SHARE, 4)
    return 1.0


def parse_status_date(description: str | None) -> datetime | None:
    """Read an ISO date (``2026-09-01``) out of a snapshot status description.

    Sports.ru writes the end of a disqualification as a bare date; anything
    else (the status echoed back, free text, nothing) yields ``None``.
    """
    if not description:
        return None
    text = str(description).strip()
    for length in (10, 19, 20, 25):
        candidate = text[:length]
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def status_availability(
    status: str | None,
    description: str | None,
    fixture_at: datetime | None,
) -> tuple[bool, float]:
    """Turn the snapshot status into ``(is_available, appearance factor)``.

    An unavailable status keeps the player out — unless the description dates
    the end of a *ban* before the fixture, in which case he is back at full
    strength, or dates the end of an injury before the fixture, in which case
    he is back but questionable. A questionable status is available at
    :data:`QUESTIONABLE_APPEARANCE_FACTOR`. Everything else is available at 1.
    """
    key = (status or "").upper()
    if key in QUESTIONABLE_STATUSES:
        return True, QUESTIONABLE_APPEARANCE_FACTOR
    if key not in UNAVAILABLE_STATUSES:
        return True, 1.0
    until = parse_status_date(description)
    if until is None or fixture_at is None or until > fixture_at:
        return False, 0.0
    if key in BAN_STATUSES:
        return True, 1.0
    return True, QUESTIONABLE_APPEARANCE_FACTOR


def ownership_appearance(selected_by: float | None) -> float | None:
    """Appearance probability implied by ownership, or ``None`` without it."""
    if selected_by is None:
        return None
    return round(min(1.0, max(0.0, float(selected_by) / OWNERSHIP_FULL_PERCENT)), 4)


def participation_matches(
    active_club_id: int | None,
    appearances: Sequence[Appearance],
    club_matches_by_club: dict[int, list[ClubMatch]],
) -> list[ClubMatch]:
    """The club matches a player could have featured in this season.

    Normally his current club's matches. A player who changed club within
    the season is judged on his *old* club's matches up to his last
    appearance for it and on his new club's matches after that: the new
    club's August, played without him, is not evidence that he sits on its
    bench. Returned most recent first, like every club-match list.
    """
    active = list(club_matches_by_club.get(active_club_id, [])) if active_club_id is not None else []
    elsewhere = [
        item
        for item in appearances
        if item.played and item.club_id is not None and item.club_id != active_club_id
    ]
    if not elsewhere:
        return sorted(active, key=lambda m: m.scheduled_at, reverse=True)
    last_other = max(elsewhere, key=lambda item: (item.scheduled_at, item.match_id))
    old = [
        match
        for match in club_matches_by_club.get(last_other.club_id, [])
        if match.scheduled_at <= last_other.scheduled_at
    ]
    new = [match for match in active if match.scheduled_at > last_other.scheduled_at]
    merged = [*old, *new]
    merged.sort(key=lambda m: m.scheduled_at, reverse=True)
    return merged


def per90(total: float, minutes: float) -> float:
    """Per-90 rate; zero when no minutes were played (documented fill)."""
    if minutes <= 0:
        return 0.0
    return round(total / minutes * 90, 4)


def prior_season_weight(
    elapsed_matches: int, *, half_life: float | None = None
) -> float:
    """How much one prior-season observation still counts.

    ``elapsed_matches`` is how much of the *new* season the player (or his club)
    has already produced. The weight starts at 1 — before the season kicks off,
    last season is all there is — and halves every ``half_life`` matches, so the
    fresh evidence never has to fight last season for room: it simply outgrows
    it. Current-season observations always weigh 1.
    """
    if elapsed_matches <= 0:
        return 1.0
    if half_life is None:
        half_life = PRIOR_SEASON_HALF_LIFE
    if half_life <= 0:
        return 0.0
    return round(0.5 ** (elapsed_matches / half_life), 6)


def rate_prior_scale(
    prior_matches: int, weight: float, *, window: float | None = None
) -> float:
    """What each of last season's totals is multiplied by before pooling.

    Last season is admitted at ``weight`` per match but for at most ``window``
    matches' worth of evidence, so a full season is scaled down to the window
    first and then discounted by the half-life weight. A short prior season
    (fewer matches than the window) is taken as it is.
    """
    if prior_matches <= 0 or weight <= 0:
        return 0.0
    cap = RATE_PRIOR_WINDOW if window is None else window
    if cap <= 0:
        return 0.0
    return round(weight * min(1.0, cap / prior_matches), 6)


def shrunk_per90(
    events: float,
    minutes: float,
    prior_rate: float,
    *,
    pseudo_matches: float | None = None,
) -> float:
    """Per-90 rate shrunk towards ``prior_rate`` with a pseudo-count of minutes.

    The prior contributes ``pseudo_matches x 90`` minutes at ``prior_rate``, so
    it decides the rate when the player has no minutes and fades as real
    minutes accumulate: after one full match it still holds three quarters of
    the vote (at the default three pseudo-matches), after a dozen a fifth.
    """
    count = RATE_SHRINK_MATCHES if pseudo_matches is None else pseudo_matches
    pseudo_minutes = max(0.0, float(count)) * 90.0
    total_minutes = max(0.0, float(minutes)) + pseudo_minutes
    if total_minutes <= 0:
        return 0.0
    pooled = max(0.0, float(events)) + max(0.0, float(prior_rate)) * pseudo_minutes / 90.0
    return round(pooled / total_minutes * 90.0, 4)


def newcomer_appearance_prior(
    role: str, price: float | None, median_price: float | None
) -> float:
    """Assumed probability that a newcomer features, from his price.

    The price is the one signal the snapshot carries about a player nobody has
    seen play: a club does not pay for a bench warmer. The prior is the role's
    base at the role's median price, rising and falling with the price by
    ``NEWCOMER_PRICE_SLOPE`` per unit, clamped to a sensible range. Without a
    price (or a median to compare it with) the base applies as it is.
    """
    base = (
        NEWCOMER_P_APPEARANCE_GOALKEEPER
        if role == "GOALKEEPER"
        else NEWCOMER_P_APPEARANCE
    )
    if price is None or median_price is None:
        return base
    value = base + NEWCOMER_PRICE_SLOPE * (float(price) - float(median_price))
    return round(min(NEWCOMER_P_APPEARANCE_MAX, max(NEWCOMER_P_APPEARANCE_MIN, value)), 4)


def share_blend_weights(
    current_matches: int,
    prior_weight: float,
    *,
    window: int = SHARE_BLEND_WINDOW,
) -> tuple[float, float]:
    """Weights for blending a current-season share with a prior-season one.

    Both sides are measured in the same units — effective matches, capped at
    ``window`` — so a full season behind us cannot outvote the one being played
    just by being longer. See :data:`SHARE_BLEND_WINDOW`.
    """
    current = float(min(max(current_matches, 0), window))
    prior = max(0.0, prior_weight) * window
    return current, prior


def blend(
    current_value: float,
    current_weight: float,
    prior_value: float,
    prior_weight: float,
) -> float:
    """Weighted average of a current-season and a prior-season quantity."""
    total = current_weight + prior_weight
    if total <= 0:
        return 0.0
    return (current_value * current_weight + prior_value * prior_weight) / total


def decayed_mean(values: list[float], decay: float) -> float:
    """Recency-weighted mean; index 0 is the most recent value."""
    total = 0.0
    weight = 1.0
    weighted = 0.0
    for value in values:
        weighted += value * weight
        total += weight
        weight *= decay
    return weighted / total if total else 0.0


def decayed_share(
    flags: list[bool], decay: float
) -> tuple[float, float]:
    """Return ``(matched weight, total weight)`` for recency-decayed flags.

    ``flags`` is ordered most recent first and the i-th entry weighs
    ``decay ** i``. A decay of 1 makes every entry count the same.
    """
    matched = 0.0
    total = 0.0
    weight = 1.0
    for flag in flags:
        total += weight
        if flag:
            matched += weight
        weight *= decay
    return matched, total


def _window_stats(window: list[Appearance]) -> dict[str, Any]:
    return {
        "appearances": len(window),
        "points_avg": round(_mean([a.points for a in window]), 4),
        "points_sum": sum(a.points for a in window),
        "goals_sum": sum(a.goals for a in window),
        "assists_sum": sum(a.assists for a in window),
        "minutes_avg": round(_mean([a.minutes for a in window]), 2),
    }


# ---------------------------------------------------------------------------
# Feature dictionary (also mirrored in docs/feature-dictionary.md).
# ---------------------------------------------------------------------------

FEATURE_DICTIONARY: tuple[dict[str, str], ...] = (
    {"name": "player_season_id", "description": "Internal player-season surrogate key."},
    {"name": "fantasy_player_id", "description": "External Sports.ru fantasy player id."},
    {"name": "player_name", "description": "Canonical player name."},
    {"name": "role", "description": "GOALKEEPER / DEFENDER / MIDFIELDER / FORWARD."},
    {"name": "club_id", "description": "Internal club id the player belongs to at cutoff."},
    {"name": "club_name", "description": "Club display name."},
    {"name": "is_home", "description": "True when the club hosts the target-tour fixture."},
    {"name": "opponent_club_id", "description": "Internal club id of the target-tour opponent."},
    {"name": "opponent_name", "description": "Opponent club display name."},
    {"name": "match_scheduled_at", "description": "Kickoff of the target-tour fixture (ISO 8601)."},
    {"name": "tour_fixtures", "description": "Every match the club plays in the target tour, in kickoff order, each with its opponent and venue strengths. Usually one; two when a postponement doubles the club up."},
    {"name": "fixture_count", "description": "How many matches the club plays in the target tour."},
    {"name": "rest_days", "description": "Days between the club's last match before cutoff and the fixture; null when the club has no prior match."},
    {"name": "availability_status", "description": "Fantasy availability status from the active snapshot."},
    {"name": "is_available", "description": "False when availability_status marks the player out (see UNAVAILABLE_STATUSES) or a red card from a previous match has not yet been served."},
    {"name": "red_card_suspension", "description": "True when the player received a red card and his club has not played a later match before the cutoff, so he misses the target tour."},
    {"name": "price", "description": "Fantasy price from the active snapshot; null when absent."},
    {"name": "selected_by", "description": "Ownership percent from the active snapshot; null when absent."},
    {"name": "form", "description": "Fantasy form value from the active snapshot; null when absent."},
    {"name": "points_avg_{N}", "description": "Mean fantasy points over the last N appearances (N in 3/5/10), spilling into last season while this one is shorter than N; 0.0 when none."},
    {"name": "points_sum_{N}", "description": "Total fantasy points over the last N appearances."},
    {"name": "goals_sum_{N}", "description": "Goals over the last N appearances."},
    {"name": "assists_sum_{N}", "description": "Assists over the last N appearances."},
    {"name": "minutes_avg_{N}", "description": "Mean minutes over the last N appearances; 0.0 when none."},
    {"name": "appearances_{N}", "description": "Number of appearances actually found in the last-N window."},
    {"name": "total_appearances", "description": "Blended appearance count: this season's plus last season's at the prior weight, so it is fractional early in a season."},
    {"name": "total_minutes", "description": "Blended minutes, weighed the same way as total_appearances."},
    {"name": "total_points", "description": "Blended fantasy points, weighed the same way as total_appearances."},
    {"name": "current_appearances", "description": "Appearances in the target season before the cutoff, unweighted (what the leakage audit recomputes)."},
    {"name": "current_minutes", "description": "Minutes in the target season before the cutoff, unweighted."},
    {"name": "current_points", "description": "Fantasy points in the target season before the cutoff, unweighted."},
    {"name": "points_per90", "description": "Blended points per 90 minutes, shrunk towards the league's role average with RATE_SHRINK_MATCHES pseudo-matches."},
    {"name": "goals_per90", "description": "Blended goals per 90 minutes, shrunk towards the role average the same way."},
    {"name": "assists_per90", "description": "Blended assists per 90 minutes, shrunk towards the role average."},
    {"name": "saves_per90", "description": "Blended goalkeeper saves per 90 minutes, shrunk towards the role average."},
    {"name": "recoveries_per90", "description": "Blended ball recoveries per 90 minutes, shrunk towards the role average."},
    {"name": "yellows_per90", "description": "Blended yellow cards per 90 minutes, shrunk towards the role average."},
    {"name": "club_matches_before", "description": "Target-season club matches played before the cutoff (how far the prior weight has decayed)."},
    {"name": "prior_club_matches", "description": "Prior-season matches of the club the player played for last season."},
    {"name": "prior_season_weight", "description": "What one prior-season appearance of this player is still worth, 1.0 before the season starts down to 0 as it progresses."},
    {"name": "prior_season_scale", "description": "The factor last season's totals were actually pooled at: prior_season_weight capped so last season brings at most RATE_PRIOR_WINDOW matches of evidence."},
    {"name": "appearance_share", "description": "Blended share of the club's matches the player was on the pitch for, recency-weighted within the current season; 0.0 when neither season has a match."},
    {"name": "start_share", "description": "Blended share of the club's matches the player started (>= 60 minutes); 0.0 when none."},
    {"name": "ninety_share", "description": "Blended share of the club's matches the player played in full (90 minutes); 0.0 when none."},
    {"name": "reds_per90", "description": "Blended red cards per 90 minutes, shrunk hard (RARE_EVENT_SHRINK_MATCHES) towards the role average."},
    {"name": "own_goals_per90", "description": "Blended own goals per 90 minutes, shrunk the same way."},
    {"name": "pen_missed_per90", "description": "Blended penalties missed (off target, post or saved) per 90 minutes, shrunk the same way."},
    {"name": "pen_saved_per90", "description": "Blended penalties saved per 90 minutes (goalkeepers), shrunk the same way."},
    {"name": "pen_conceded_per90", "description": "Blended penalties conceded per 90 minutes, shrunk the same way."},
    {"name": "availability_factor", "description": "Multiplier applied to the appearance share: 0 when marked out or banned, 0.5 when questionable or just back, 0.65 for the second match after a straight red, else 1."},
    {"name": "club_matches_available", "description": "Club matches the player was eligible for this season (his old club's up to a transfer, his new club's after it)."},
    {"name": "p_appearance", "description": "Probability of playing the fixture — the appearance share above, times availability_factor, lifted by ownership when OWNERSHIP_APPEARANCE_WEIGHT is set; 0.0 when unavailable."},
    {"name": "expected_minutes", "description": "Expected minutes: p_appearance x blended mean minutes when appearing."},
    {"name": "club_attack", "description": "Club goals scored per match at the fixture venue (home/away), blended across seasons; league mean when no venue matches at all."},
    {"name": "club_defense", "description": "Club goals conceded per match at the fixture venue; league mean when no venue matches at all."},
    {"name": "opponent_attack", "description": "Opponent goals scored per match at their fixture venue; league mean fallback."},
    {"name": "opponent_defense", "description": "Opponent goals conceded per match at their fixture venue; league mean fallback."},
    {"name": "has_history", "description": "True when the player has at least one appearance in either season, in the target competition or a parallel league."},
    {"name": "stat_source", "description": "'current_season' once the player has played in the target season; 'parallel_league' when his only play this season is in his national league (a cup target, step 24); otherwise 'prior_season'."},
    {"name": "is_newcomer", "description": "True when the player has no appearance in any season or source and is scored from documented role priors."},
    {"name": "parallel_appearances", "description": "Appearances this season in the parallel leagues before the cutoff, unweighted (0 outside a cup target)."},
    {"name": "parallel_club_matches", "description": "The player's league club's matches this season before the cutoff, unweighted."},
    {"name": "parallel_weight", "description": "What one parallel-league observation is worth against one of the target competition's own (PARALLEL_WEIGHT); 0 when there is no parallel layer."},
    {"name": "league_factor", "description": "The LEAGUE_STRENGTH factor the parallel league's goals were translated by; 1.0 when there is none."},
    {"name": "availability_source", "description": "null when availability_status is the target season's own; the slug of the parallel league whose snapshot lent an out/doubtful status."},
    {"name": "sources", "description": "Every layer of the row's history: the target competition's current and prior season, then each parallel league's, with appearances, club matches, the weight they entered at and the goal factor."},
)


# ---------------------------------------------------------------------------
# Loading and resolution.
# ---------------------------------------------------------------------------


def resolve_run(
    session,
    run_id: int | None,
    season_ref: str | None,
    competition_ref: str | None = None,
):
    """Resolve the ingestion run whose snapshot the features are built from.

    With an explicit ``run_id`` that run is used. Otherwise the active run
    (published by the quality gate) is selected, narrowed by ``competition_ref``
    (a tournament slug or fantasy tournament id) and/or a season referenced by its
    fantasy/stat id or name.

    Once more than one league is imported there is no single active snapshot, so a
    caller that names neither gets the most recently published one — whichever
    league that happens to belong to. Both the API and the UI therefore always
    name the season they mean; the fallback only keeps single-league setups and
    ad-hoc CLI runs working.
    """
    from .db.models import Competition, IngestionRun

    if run_id is not None:
        run = session.get(IngestionRun, run_id)
        if run is None:
            raise FeaturesError(f"Ingestion run {run_id} does not exist")
        if run.season_id is None:
            raise FeaturesError(
                f"Ingestion run {run_id} has no season; run the quality gate first"
            )
        return run

    query = (
        select(IngestionRun)
        .where(IngestionRun.is_active.is_(True))
        .order_by(IngestionRun.id.desc())
    )
    if season_ref is not None or competition_ref is not None:
        query = query.join(Season, IngestionRun.season_id == Season.id)
    if season_ref is not None:
        query = query.where(
            (Season.fantasy_season_id == season_ref)
            | (Season.stat_season_id == season_ref)
            | (Season.name == season_ref)
        )
    if competition_ref is not None:
        query = query.join(
            Competition, Season.competition_id == Competition.id
        ).where(
            (Competition.slug == competition_ref)
            | (Competition.fantasy_tournament_id == competition_ref)
        )
    run = session.execute(query.limit(1)).scalar_one_or_none()
    if run is None:
        scope = ", ".join(
            filter(
                None,
                [
                    f"competition '{competition_ref}'" if competition_ref else "",
                    f"season '{season_ref}'" if season_ref else "",
                ],
            )
        )
        raise FeaturesError(
            "No active snapshot found; run 'fantasy-ingest' then 'fantasy-quality'"
            + (f" for {scope}" if scope else "")
        )
    return run


def resolve_target_tour(
    session, season_id: int, tour_ref: str | None
) -> FantasyTour:
    """Resolve the tour to build features for.

    A ``tour_ref`` matches a tour by its fantasy id or name. Without one, the
    earliest tour that is not ``FINISHED`` is chosen (the natural "next tour");
    a fully finished season therefore requires an explicit tour, which is what
    backtesting (step 12) needs.
    """
    tours = list(
        session.execute(
            select(FantasyTour)
            .where(FantasyTour.season_id == season_id)
            .order_by(FantasyTour.starts_at.is_(None), FantasyTour.starts_at)
        ).scalars()
    )
    if not tours:
        raise FeaturesError(f"Season {season_id} has no tours")

    if tour_ref is not None:
        for tour in tours:
            if tour.fantasy_tour_id == tour_ref or tour.name == tour_ref:
                return tour
        raise FeaturesError(
            f"Tour '{tour_ref}' not found in season {season_id}"
        )

    for tour in tours:
        if (tour.status or "").upper() != "FINISHED":
            return tour
    raise FeaturesError(
        "Every tour in the season is FINISHED; pass an explicit --tour to build "
        "features for a historical tour"
    )


def _tour_cutoff(tour: FantasyTour, fixtures: list[Fixture]) -> datetime:
    """Cutoff before which data may be used: deadline, else start, else kickoff.

    Never later than the tour's own first kickoff. A fantasy tour is a slice of
    the calendar, not a round, so a postponed match is re-attached to whichever
    tour it now falls in and the deadline moves with it. The source does not
    always move the deadline back far enough, and a deadline sitting after a
    kickoff would let matches of the tour being predicted into the history that
    predicts it — through the club-strength aggregates, which have no player to
    exclude them by.
    """
    candidates = [
        moment
        for moment in (
            tour.transfers_deadline_at,
            tour.starts_at,
            min((fixture.scheduled_at for fixture in fixtures), default=None),
        )
        if moment is not None
    ]
    if not candidates:
        raise FeaturesError(
            f"Tour {tour.fantasy_tour_id} has no deadline, start or scheduled match"
        )
    return min(candidates)


def _load_fixtures(session, tour_id: int) -> list[Fixture]:
    rows = session.execute(
        select(
            Match.id,
            Match.scheduled_at,
            Match.home_club_id,
            Match.away_club_id,
        ).where(Match.tour_id == tour_id)
    ).all()
    fixtures: list[Fixture] = []
    for match_id, scheduled_at, home_club_id, away_club_id in rows:
        fixtures.append(
            Fixture(
                match_id=match_id,
                scheduled_at=scheduled_at,
                club_id=home_club_id,
                opponent_club_id=away_club_id,
                is_home=True,
            )
        )
        fixtures.append(
            Fixture(
                match_id=match_id,
                scheduled_at=scheduled_at,
                club_id=away_club_id,
                opponent_club_id=home_club_id,
                is_home=False,
            )
        )
    return fixtures


def _load_club_matches(
    session,
    season_id: int,
    run_id: int,
    cutoff: datetime,
    exclude_match_ids: frozenset[int] = frozenset(),
) -> dict[int, list[ClubMatch]]:
    """Club results before the cutoff, excluding the tour being predicted.

    The exclusion is the same second line of defence the player history gets:
    a fantasy tour is a slice of the calendar, so a moved match can land inside
    a tour whose deadline has already passed, and a club strength computed from
    it would be describing the very fixtures it is used to predict.
    """
    rows = session.execute(
        select(
            ClubMatchStats.club_id,
            Match.id,
            Match.scheduled_at,
            ClubMatchStats.is_home,
            ClubMatchStats.goals_scored,
            ClubMatchStats.goals_conceded,
        )
        .join(Match, ClubMatchStats.match_id == Match.id)
        .where(
            Match.season_id == season_id,
            ClubMatchStats.ingestion_run_id == run_id,
            Match.scheduled_at < cutoff,
            ClubMatchStats.goals_scored.isnot(None),
            ClubMatchStats.goals_conceded.isnot(None),
        )
    ).all()
    by_club: dict[int, list[ClubMatch]] = {}
    for club_id, match_id, scheduled_at, is_home, gf, ga in rows:
        if match_id in exclude_match_ids:
            continue
        by_club.setdefault(club_id, []).append(
            ClubMatch(
                match_id=match_id,
                scheduled_at=scheduled_at,
                is_home=bool(is_home),
                goals_scored=gf,
                goals_conceded=ga,
            )
        )
    for matches in by_club.values():
        matches.sort(key=lambda m: m.scheduled_at, reverse=True)
    return by_club


def load_appearances(
    session, season_id: int, run_id: int
) -> dict[int, list[Appearance]]:
    rows = session.execute(
        select(
            PlayerMatchStats.player_season_id,
            PlayerMatchStats.match_id,
            Match.scheduled_at,
            PlayerMatchStats.field_minutes,
            PlayerMatchStats.points,
            PlayerMatchStats.goals,
            PlayerMatchStats.assists,
            PlayerMatchStats.saves,
            PlayerMatchStats.ball_recoveries,
            PlayerMatchStats.yellow_cards,
            PlayerMatchStats.red_cards,
            PlayerMatchStats.own_goals,
            PlayerMatchStats.penalties_missed,
            PlayerMatchStats.penalties_post,
            PlayerMatchStats.penalties_target,
            PlayerMatchStats.penalties_saved,
            PlayerMatchStats.penalty_conceded,
            SeasonClub.club_id,
        )
        .join(Match, PlayerMatchStats.match_id == Match.id)
        .join(PlayerSeason, PlayerMatchStats.player_season_id == PlayerSeason.id)
        .outerjoin(SeasonClub, PlayerMatchStats.season_club_id == SeasonClub.id)
        .where(
            PlayerSeason.season_id == season_id,
            PlayerMatchStats.ingestion_run_id == run_id,
        )
    ).all()
    by_player: dict[int, list[Appearance]] = {}
    for row in rows:
        by_player.setdefault(row.player_season_id, []).append(
            Appearance(
                match_id=row.match_id,
                scheduled_at=row.scheduled_at,
                minutes=row.field_minutes,
                points=row.points,
                goals=row.goals,
                assists=row.assists,
                saves=row.saves,
                ball_recoveries=row.ball_recoveries,
                yellow_cards=row.yellow_cards,
                red_cards=row.red_cards,
                own_goals=row.own_goals or 0,
                penalties_missed=(
                    (row.penalties_missed or 0)
                    + (row.penalties_post or 0)
                    + (row.penalties_target or 0)
                ),
                penalties_saved=row.penalties_saved or 0,
                penalty_conceded=row.penalty_conceded or 0,
                club_id=row.club_id,
            )
        )
    return by_player


def _load_snapshots(session, run_id: int) -> dict[int, dict[str, Any]]:
    rows = session.execute(
        select(
            FantasyPlayerSnapshot.player_season_id,
            FantasyPlayerSnapshot.availability_status,
            FantasyPlayerSnapshot.status_description,
            FantasyPlayerSnapshot.price,
            FantasyPlayerSnapshot.selected_by,
            FantasyPlayerSnapshot.form,
        ).where(FantasyPlayerSnapshot.ingestion_run_id == run_id)
    ).all()
    snapshots: dict[int, dict[str, Any]] = {}
    for player_season_id, status, description, price, selected_by, form in rows:
        snapshots[player_season_id] = {
            "availability_status": status,
            "status_description": description,
            "price": float(price) if price is not None else None,
            "selected_by": float(selected_by) if selected_by is not None else None,
            "form": form,
        }
    return snapshots


def _load_players(session, season_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            PlayerSeason.id,
            PlayerSeason.player_id,
            PlayerSeason.fantasy_player_id,
            PlayerSeason.role,
            PlayerSeason.current_season_club_id,
            Player.canonical_name,
        )
        .join(Player, PlayerSeason.player_id == Player.id)
        .where(PlayerSeason.season_id == season_id)
    ).all()
    return [
        {
            "player_season_id": pid,
            "player_id": player_id,
            "fantasy_player_id": fantasy_id,
            "role": role,
            "current_season_club_id": season_club_id,
            "player_name": name,
        }
        for pid, player_id, fantasy_id, role, season_club_id, name in rows
    ]


def _load_season_clubs(session, season_id: int) -> dict[int, dict[str, Any]]:
    rows = session.execute(
        select(
            SeasonClub.id,
            SeasonClub.club_id,
            SeasonClub.display_name,
        ).where(SeasonClub.season_id == season_id)
    ).all()
    return {
        season_club_id: {"club_id": club_id, "display_name": name}
        for season_club_id, club_id, name in rows
    }


# ---------------------------------------------------------------------------
# Cross-season sourcing (step 14).
# ---------------------------------------------------------------------------


def _role_totals(
    appearances_by_ps: dict[int, list[Appearance]],
    role_by_ps: dict[int, str],
    totals: dict[str, dict[str, int]] | None = None,
) -> dict[str, dict[str, int]]:
    """Sum every played appearance of a season into per-role event totals.

    Passing ``totals`` accumulates into it, which is how the two seasons are
    pooled into one league-wide prior.
    """
    if totals is None:
        totals = {}
    for ps_id, appearances in appearances_by_ps.items():
        role = role_by_ps.get(ps_id)
        if role is None:
            continue
        agg = totals.setdefault(role, _empty_totals())
        _accumulate_totals(agg, appearances)
    return totals


def _empty_totals() -> dict[str, int]:
    return {
        "minutes": 0,
        "count": 0,
        "starts": 0,
        "ninety": 0,
        "points": 0,
        "goals": 0,
        "assists": 0,
        "saves": 0,
        "recoveries": 0,
        "yellows": 0,
        "reds": 0,
        "own_goals": 0,
        "pen_missed": 0,
        "pen_saved": 0,
        "pen_conceded": 0,
    }


def _accumulate_totals(agg: dict[str, int], appearances: Sequence[Appearance]) -> None:
    for appearance in appearances:
        if not appearance.played:
            continue
        agg["minutes"] += appearance.minutes
        agg["count"] += 1
        agg["starts"] += appearance.minutes >= START_MINUTES_THRESHOLD
        agg["ninety"] += appearance.minutes >= NINETY_MINUTES_THRESHOLD
        agg["points"] += appearance.points
        agg["goals"] += appearance.goals
        agg["assists"] += appearance.assists
        agg["saves"] += appearance.saves
        agg["recoveries"] += appearance.ball_recoveries
        agg["yellows"] += appearance.yellow_cards
        agg["reds"] += appearance.red_cards
        agg["own_goals"] += appearance.own_goals
        agg["pen_missed"] += appearance.penalties_missed
        agg["pen_saved"] += appearance.penalties_saved
        agg["pen_conceded"] += appearance.penalty_conceded


def _prior_from_totals(agg: dict[str, int]) -> RolePrior:
    minutes = agg["minutes"]
    return RolePrior(
        goals_per90=per90(agg["goals"], minutes),
        assists_per90=per90(agg["assists"], minutes),
        saves_per90=per90(agg["saves"], minutes),
        recoveries_per90=per90(agg["recoveries"], minutes),
        yellows_per90=per90(agg["yellows"], minutes),
        mean_minutes=round(minutes / agg["count"], 2) if agg["count"] else 0.0,
        points_per90=per90(agg["points"], minutes),
        reds_per90=per90(agg["reds"], minutes),
        own_goals_per90=per90(agg["own_goals"], minutes),
        pen_missed_per90=per90(agg["pen_missed"], minutes),
        pen_saved_per90=per90(agg["pen_saved"], minutes),
        pen_conceded_per90=per90(agg["pen_conceded"], minutes),
        ninety_of_starts=(
            round(agg["ninety"] / agg["starts"], 4) if agg["starts"] else 0.0
        ),
    )


def _priors_from_totals(totals: dict[str, dict[str, int]]) -> dict[str, RolePrior]:
    return {role: _prior_from_totals(agg) for role, agg in totals.items()}


def price_bucket(price: float | None, edges: Sequence[float]) -> int | None:
    """Index of the price bucket a price falls in, given ascending inner edges."""
    if price is None:
        return None
    bucket = 0
    for edge in edges:
        if float(price) > edge:
            bucket += 1
    return bucket


def price_bucket_edges(prices: Sequence[float], buckets: int = PRICE_BUCKETS) -> list[float]:
    """Inner quantile edges splitting ``prices`` into ``buckets`` groups."""
    ordered = sorted(float(p) for p in prices)
    if len(ordered) < buckets or buckets < 2:
        return []
    return [ordered[len(ordered) * k // buckets] for k in range(1, buckets)]


def _price_bucket_priors(
    current_appearances: dict[int, list[Appearance]],
    current_roles: dict[int, str],
    current_prices: dict[int, float],
    prior: PriorContext | None,
    prior_prices: dict[int, float],
    *,
    cutoff: datetime,
    exclude_match_ids: frozenset[int],
) -> tuple[dict[str, list[float]], dict[tuple[str, int], RolePrior]]:
    """Per-(role, price bucket) priors, pooled from both seasons.

    Buckets are terciles of the *current* snapshot's prices within each role.
    Last season's players enter the bucket of the price they carry in the
    current snapshot (through the shared identity), so a player without one
    contributes to the role average only. A bucket with fewer than
    :data:`PRICE_BUCKET_MIN_MINUTES` of play is dropped, and the role prior
    stands in for it.
    """
    by_role: dict[str, list[float]] = {}
    for ps_id, price in current_prices.items():
        role = current_roles.get(ps_id)
        if role is not None:
            by_role.setdefault(role, []).append(price)
    edges = {role: price_bucket_edges(prices) for role, prices in by_role.items()}

    totals: dict[tuple[str, int], dict[str, int]] = {}

    def _add(role: str | None, price: float | None, items: Sequence[Appearance]) -> None:
        if role is None or price is None or role not in edges or not edges[role]:
            return
        bucket = price_bucket(price, edges[role])
        if bucket is None:
            return
        _accumulate_totals(totals.setdefault((role, bucket), _empty_totals()), items)

    for ps_id, items in current_appearances.items():
        _add(
            current_roles.get(ps_id),
            current_prices.get(ps_id),
            played_before_cutoff(items, cutoff, exclude_match_ids=exclude_match_ids),
        )
    if prior is not None:
        for player in prior.by_player_id.values():
            _add(
                player.role,
                prior_prices.get(player.player_season_id),
                prior.appearances.get(player.player_season_id, []),
            )
    priors = {
        key: _prior_from_totals(agg)
        for key, agg in totals.items()
        if agg["minutes"] >= PRICE_BUCKET_MIN_MINUTES
    }
    return edges, priors


def _role_priors(
    appearances_by_ps: dict[int, list[Appearance]],
    role_by_ps: dict[int, str],
) -> dict[str, RolePrior]:
    """Aggregate a season into per-role per-90 priors.

    Rates are pooled across every appearance of a role (so they are minute-
    weighted), and ``mean_minutes`` is the average minutes of a single
    appearance. A role without any appearance yields no prior. The result is
    what newcomers are scored from (step 14) and what every player's own rates
    are shrunk towards (:func:`shrunk_per90`).
    """
    return _priors_from_totals(_role_totals(appearances_by_ps, role_by_ps))


def _league_role_priors(
    current_appearances: dict[int, list[Appearance]],
    current_roles: dict[int, str],
    prior: PriorContext | None,
    *,
    cutoff: datetime,
    exclude_match_ids: frozenset[int],
    parallel: Sequence[ParallelContext] = (),
) -> dict[str, RolePrior]:
    """Role priors pooled from both seasons, leakage-free.

    The current season's appearances are cut at the tour deadline and the
    target tour's matches are dropped by id, exactly as a player's own history
    is, so the prior can never carry the tour being predicted. Both seasons are
    pooled unweighted: a league average built from thousands of minutes is
    stable, and the point of it is to be a stable anchor.

    The parallel leagues of a cup (step 24) are pooled in the same way, cut at
    the same cutoff: before its first tour a cup has no appearances of its own
    to anchor anything to, and the leagues its clubs come from are the closest
    population there is. Their goals are not translated by the league factor
    here — an anchor built from seven leagues is a broad average already.
    """
    filtered = {
        ps_id: played_before_cutoff(
            items, cutoff, exclude_match_ids=exclude_match_ids
        )
        for ps_id, items in current_appearances.items()
    }
    totals = _role_totals(filtered, current_roles)
    if prior is not None:
        prior_roles = {
            player.player_season_id: player.role
            for player in prior.by_player_id.values()
        }
        totals = _role_totals(prior.appearances, prior_roles, totals)
    for context in parallel:
        roles = {
            player.player_season_id: player.role
            for player in context.by_player_id.values()
        }
        pooled = {
            ps_id: played_before_cutoff(
                items, cutoff, exclude_match_ids=exclude_match_ids
            )
            for ps_id, items in context.appearances.items()
        }
        totals = _role_totals(pooled, roles, totals)
        if context.prior is not None:
            prior_roles = {
                player.player_season_id: player.role
                for player in context.prior.by_player_id.values()
            }
            totals = _role_totals(context.prior.appearances, prior_roles, totals)
    return _priors_from_totals(totals)


def resolve_prior_run(session, season: Season, *, require_earlier: bool = False):
    """Return the active run of the season preceding ``season``, if any.

    A "prior season" is another season of the same competition with its own
    published (active) snapshot; the immediately-preceding one (the latest
    starting before the target) is chosen. Returns ``None`` when the target
    season is the only one imported.

    The two callers want different things when *nothing* starts earlier.
    Cross-season forecasting (the default) falls back to the latest other
    season: it only runs when the target season has no played match, so any
    other season's history beats forecasting from nothing. Presenting a player's
    "previous season" has no such excuse — labelling a *later* season as last
    season would simply be wrong — so it passes ``require_earlier``.
    """
    from .db.models import IngestionRun

    rows = session.execute(
        select(IngestionRun, Season.starts_at)
        .join(Season, IngestionRun.season_id == Season.id)
        .where(
            IngestionRun.is_active.is_(True),
            Season.competition_id == season.competition_id,
            Season.id != season.id,
        )
    ).all()
    if not rows:
        return None

    target_start = season.starts_at
    earlier = [
        item
        for item in rows
        if item[1] is not None
        and target_start is not None
        and item[1] < target_start
    ]
    if require_earlier:
        pool = earlier
    elif earlier:
        pool = earlier
    else:
        # Nothing is known to start earlier. Only when the *dates* are unknown
        # is another season a reasonable stand-in; a season that is known to
        # start later is the future, and forecasting a 2024/25 tour from
        # 2025/26 would be leakage in a backtest and nonsense in production.
        pool = [
            item
            for item in rows
            if item[1] is None or target_start is None
        ]
    if not pool:
        return None

    def _key(item):
        run, starts_at = item
        return (starts_at is not None, starts_at, run.id)

    return max(pool, key=_key)[0]


def _load_prior_context(session, prior_run, cutoff: datetime) -> PriorContext:
    """Load the prior season's history keyed by the cross-season identities."""
    season_id = prior_run.season_id
    run_id = prior_run.id
    appearances = load_appearances(session, season_id, run_id)
    club_matches = _load_club_matches(session, season_id, run_id, cutoff)
    players = _load_players(session, season_id)
    season_clubs = _load_season_clubs(session, season_id)

    role_by_ps = {p["player_season_id"]: p["role"] for p in players}
    by_player_id: dict[int, PriorPlayer] = {}
    for player in players:
        season_club_id = player["current_season_club_id"]
        club_info = season_clubs.get(season_club_id) if season_club_id else None
        by_player_id[player["player_id"]] = PriorPlayer(
            player_season_id=player["player_season_id"],
            club_id=club_info["club_id"] if club_info else None,
            role=player["role"],
        )
    role_priors = _role_priors(appearances, role_by_ps)
    prior_season = session.get(Season, season_id)
    return PriorContext(
        run_id=run_id,
        season_id=season_id,
        appearances=appearances,
        club_matches=club_matches,
        by_player_id=by_player_id,
        role_priors=role_priors,
        season_name=prior_season.name if prior_season is not None else "",
    )


def _resolve_history_source(
    player: dict[str, Any],
    active_club_id: int | None,
    *,
    appearances: dict[int, list[Appearance]],
    current_club_matches: dict[int, list[ClubMatch]],
    prior: PriorContext | None,
) -> dict[str, Any]:
    """Collect both halves of one player's history, current and prior.

    The current-season half is the player's own play for the club he is
    registered with now. The prior-season half is resolved through the shared
    ``player_id``, and is taken against the club he played for *last* season, so
    a transfer keeps his real track record instead of inheriting his new club's.

    Both halves are returned; how much each one counts is decided later by
    :func:`prior_season_weight`, which is what lets last season keep informing a
    forecast well past the opening tour while never outweighing what has already
    happened this one. A player with no history on either side is a newcomer and
    is scored from the position priors.
    """
    active_ps_id = player["player_season_id"]
    current_appearances = appearances.get(active_ps_id, [])
    current_matches = (
        current_club_matches.get(active_club_id, [])
        if active_club_id is not None
        else []
    )

    prior_appearances: list[Appearance] = []
    prior_matches: list[ClubMatch] = []
    if prior is not None:
        prior_player = prior.by_player_id.get(player["player_id"])
        if prior_player is not None:
            prior_appearances = prior.appearances.get(
                prior_player.player_season_id, []
            )
            if prior_player.club_id is not None:
                prior_matches = prior.club_matches.get(prior_player.club_id, [])
    if not prior_appearances:
        prior_matches = []

    is_newcomer = not current_appearances and not prior_appearances
    return {
        "appearances": current_appearances,
        "club_matches": current_matches,
        "prior_appearances": prior_appearances,
        "prior_club_matches": prior_matches,
        "is_newcomer": is_newcomer,
        "newcomer_prior": (
            prior.role_priors.get(player["role"])
            if is_newcomer and prior is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Parallel sourcing (step 24): the national leagues behind a European cup.
# ---------------------------------------------------------------------------


def resolve_parallel_runs(session, season: Season, competition, cutoff: datetime):
    """The active runs of the leagues running alongside a cup season.

    Returns ``(run, season, competition)`` triples, one per league, ordered as
    the catalogue orders the leagues. Empty unless the target competition is
    one of :data:`PARALLEL_TARGET_SLUGS`, so a domestic league never sources
    from anything but itself. A league qualifies through
    :func:`parallel_season_overlaps`; when two of its seasons both do (the
    dates are unknown, say) the later-starting one is taken.
    """
    from .db.models import Competition, IngestionRun

    if competition is None or competition.slug not in PARALLEL_TARGET_SLUGS:
        return []
    rows = session.execute(
        select(IngestionRun, Season, Competition)
        .join(Season, IngestionRun.season_id == Season.id)
        .join(Competition, Season.competition_id == Competition.id)
        .where(
            IngestionRun.is_active.is_(True),
            Season.competition_id != season.competition_id,
            Competition.slug.not_in(NON_LEAGUE_SLUGS),
        )
    ).all()
    chosen: dict[int, tuple[Any, Season, Any]] = {}
    for run, other, league in rows:
        if not parallel_season_overlaps(
            other.starts_at,
            other.ends_at,
            target_starts_at=season.starts_at,
            cutoff=cutoff,
        ):
            continue
        best = chosen.get(league.id)
        if best is None or (other.starts_at or datetime.min.replace(tzinfo=UTC)) > (
            best[1].starts_at or datetime.min.replace(tzinfo=UTC)
        ):
            chosen[league.id] = (run, other, league)
    return sorted(chosen.values(), key=lambda item: (item[2].sort_order, item[2].slug))


def _load_parallel_contexts(
    session, season: Season | None, competition, cutoff: datetime
) -> list[ParallelContext]:
    """Load every parallel league of the target season, with its own last season.

    Everything is keyed by the cross-competition identities (``player_id``,
    ``club_id``) so a cup row can find its league half. Club results are cut
    at the cutoff on load; appearances are cut per row, like the cup's own.
    """
    if season is None:
        return []
    contexts: list[ParallelContext] = []
    for run, other, league in resolve_parallel_runs(session, season, competition, cutoff):
        players = _load_players(session, other.id)
        season_clubs = _load_season_clubs(session, other.id)
        by_player_id: dict[int, PriorPlayer] = {}
        for player in players:
            season_club_id = player["current_season_club_id"]
            club_info = season_clubs.get(season_club_id) if season_club_id else None
            by_player_id[player["player_id"]] = PriorPlayer(
                player_season_id=player["player_season_id"],
                club_id=club_info["club_id"] if club_info else None,
                role=player["role"],
            )
        prior_run = resolve_prior_run(session, other, require_earlier=True)
        contexts.append(
            ParallelContext(
                run_id=run.id,
                season_id=other.id,
                competition_slug=league.slug,
                competition_name=league.name,
                season_name=other.name,
                factor=league_strength(league.slug),
                appearances=load_appearances(session, other.id, run.id),
                club_matches=_load_club_matches(session, other.id, run.id, cutoff),
                by_player_id=by_player_id,
                snapshots=_load_snapshots(session, run.id),
                prior=(
                    _load_prior_context(session, prior_run, cutoff)
                    if prior_run is not None
                    else None
                ),
            )
        )
    return contexts


def parallel_layers_for_player(
    player_id: int,
    contexts: Sequence[ParallelContext],
    *,
    club_id: int | None = None,
) -> list[HistoryLayer]:
    """A player's league history, this season and last, as layers.

    The current-season layer is kept even when the player has not played in
    the league yet: his club's matches without him are evidence, exactly as the
    cup's own club matches are — but only when the league registers him with
    the same club (``club_id``) the cup does. A player the league still lists
    at the club he left in the window keeps his own appearances and loses
    that club's matches, which are no longer evidence about him. The
    prior-season layer needs appearances to mean anything, as the cup's own
    prior does.
    """
    layers: list[HistoryLayer] = []
    for context in contexts:
        current = context.by_player_id.get(player_id)
        if current is not None:
            appearances = context.appearances.get(current.player_season_id, [])
            same_club = club_id is None or current.club_id == club_id
            club_matches = (
                context.club_matches.get(current.club_id, [])
                if current.club_id is not None and same_club
                else []
            )
            if appearances or club_matches:
                layers.append(
                    HistoryLayer(
                        source=context.competition_slug,
                        season_name=context.season_name,
                        appearances=appearances,
                        club_matches=club_matches,
                        weight=PARALLEL_WEIGHT,
                        factor=context.factor,
                        is_prior=False,
                    )
                )
        if context.prior is not None:
            prior_player = context.prior.by_player_id.get(player_id)
            if prior_player is None:
                continue
            appearances = context.prior.appearances.get(prior_player.player_season_id, [])
            if not appearances:
                continue
            layers.append(
                HistoryLayer(
                    source=context.competition_slug,
                    season_name=_season_name_of(context.prior),
                    appearances=appearances,
                    club_matches=(
                        context.prior.club_matches.get(prior_player.club_id, [])
                        if prior_player.club_id is not None
                        else []
                    ),
                    weight=PARALLEL_WEIGHT,
                    factor=context.factor,
                    is_prior=True,
                )
            )
    return layers


def _season_name_of(prior: PriorContext) -> str:
    return prior.season_name or f"season {prior.season_id}"


def parallel_snapshots_for_player(
    player_id: int, contexts: Sequence[ParallelContext]
) -> list[tuple[str, dict[str, Any] | None]]:
    """The league snapshots of a player, for :func:`merge_availability`."""
    found: list[tuple[str, dict[str, Any] | None]] = []
    for context in contexts:
        current = context.by_player_id.get(player_id)
        if current is not None:
            found.append(
                (context.competition_slug, context.snapshots.get(current.player_season_id))
            )
    return found


def parallel_club_layers(
    contexts: Sequence[ParallelContext], club_ids: Sequence[int]
) -> dict[int, list[HistoryLayer]]:
    """The league results of the cup's clubs, this season and last, as layers.

    Only the clubs asked for are returned: the pool the strengths are shrunk
    towards should be the cup's field, not every club of every league.
    """
    layers: dict[int, list[HistoryLayer]] = {}
    for context in contexts:
        for club_id in club_ids:
            matches = context.club_matches.get(club_id)
            if matches:
                layers.setdefault(club_id, []).append(
                    HistoryLayer(
                        source=context.competition_slug,
                        season_name=context.season_name,
                        appearances=[],
                        club_matches=matches,
                        weight=PARALLEL_WEIGHT,
                        factor=context.factor,
                        is_prior=False,
                    )
                )
            if context.prior is None:
                continue
            prior_matches = context.prior.club_matches.get(club_id)
            if prior_matches:
                layers.setdefault(club_id, []).append(
                    HistoryLayer(
                        source=context.competition_slug,
                        season_name=_season_name_of(context.prior),
                        appearances=[],
                        club_matches=prior_matches,
                        weight=PARALLEL_WEIGHT,
                        factor=context.factor,
                        is_prior=True,
                    )
                )
    return layers


def _scaled_totals(totals: dict[str, float], layer: HistoryLayer) -> dict[str, float]:
    """A layer's event totals at its weight, with its goals in the cup's units."""
    scaled = {key: float(value) * layer.weight for key, value in totals.items()}
    scaled["goals"] *= layer.factor
    scaled["assists"] *= layer.factor
    if layer.factor > 0:
        scaled["saves"] /= layer.factor
    return scaled


# ---------------------------------------------------------------------------
# Strength aggregation.
# ---------------------------------------------------------------------------


def _weighted_mean(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """Return ``(mean, total weight)`` for ``(value, weight)`` pairs."""
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return 0.0, 0.0
    return sum(value * weight for value, weight in pairs) / total, total


def _club_strengths(
    club_matches: dict[int, list[tuple[ClubMatch, float]]],
    *,
    shrink_matches: float | None = None,
    venue_mode: str | None = None,
) -> tuple[dict[int, dict[str, float]], dict[str, float]]:
    """Per-club home/away attack & defence, plus league averages for fills.

    Every match carries the weight of the season it belongs to, so a club whose
    new season is two matches old is still described mostly by last season
    rather than by a single 4-0 that happened to be its opener. Each venue
    strength is additionally shrunk towards the league's venue average with
    ``shrink_matches`` pseudo-matches (default :data:`STRENGTH_SHRINK_MATCHES`),
    so a club with one home result is mostly the league average at home and a
    club with none is exactly that.

    In ``pooled`` venue mode (default :data:`STRENGTH_VENUE_MODE`) a club has
    one attack and one defence estimated from *every* match, each goal count
    divided by the league's factor for the venue it was scored at, and the
    factor is multiplied back in for the fixture venue. Twice the sample for
    the same information; the league averages reported are unchanged.
    """
    pseudo = STRENGTH_SHRINK_MATCHES if shrink_matches is None else shrink_matches
    pseudo = max(0.0, float(pseudo))
    mode = STRENGTH_VENUE_MODE if venue_mode is None else venue_mode
    league_pairs: dict[str, list[tuple[float, float]]] = {
        "home_attack": [],
        "home_defense": [],
        "away_attack": [],
        "away_defense": [],
    }
    for matches in club_matches.values():
        for match, weight in matches:
            venue = "home" if match.is_home else "away"
            league_pairs[f"{venue}_attack"].append((match.goals_scored, weight))
            league_pairs[f"{venue}_defense"].append((match.goals_conceded, weight))

    league = {
        key: round(_weighted_mean(pairs)[0], 4)
        for key, pairs in league_pairs.items()
    }

    strengths: dict[int, dict[str, float]] = {}
    if mode == "pooled":
        overall = {
            key: _weighted_mean(league_pairs[f"home_{key}"] + league_pairs[f"away_{key}"])[0]
            for key in ("attack", "defense")
        }
        # A venue nobody has scored at (a two-match synthetic season, or the
        # very first weekend) has no factor to speak of; 1.0 keeps the pooled
        # strength usable instead of dividing by zero.
        factors = {
            (venue, key): (
                league[f"{venue}_{key}"] / overall[key]
                if overall[key] > 0 and league[f"{venue}_{key}"] > 0
                else 1.0
            )
            for venue in ("home", "away")
            for key in ("attack", "defense")
        }
        for club_id, matches in club_matches.items():
            entry: dict[str, float] = {}
            for key, getter in (
                ("attack", lambda m: m.goals_scored),
                ("defense", lambda m: m.goals_conceded),
            ):
                pairs = [
                    (getter(m) / factors[("home" if m.is_home else "away", key)], w)
                    for m, w in matches
                ]
                value, weight = _weighted_mean(pairs)
                anchor = overall[key]
                pooled = (
                    (value * weight + anchor * pseudo) / (weight + pseudo)
                    if weight > 0
                    else anchor
                )
                for venue in ("home", "away"):
                    entry[f"{venue}_{key}"] = round(pooled * factors[(venue, key)], 4)
            entry["matches_home"] = sum(1 for m, _ in matches if m.is_home)
            entry["matches_away"] = sum(1 for m, _ in matches if not m.is_home)
            strengths[club_id] = entry
        return strengths, league

    for club_id, matches in club_matches.items():
        entry = {}
        for venue, is_home in (("home", True), ("away", False)):
            side = [(m, w) for m, w in matches if m.is_home is is_home]
            attack, weight = _weighted_mean([(m.goals_scored, w) for m, w in side])
            defense, _ = _weighted_mean([(m.goals_conceded, w) for m, w in side])
            for key, value in (("attack", attack), ("defense", defense)):
                anchor = league[f"{venue}_{key}"]
                if weight > 0:
                    pooled = (value * weight + anchor * pseudo) / (weight + pseudo)
                    entry[f"{venue}_{key}"] = round(pooled, 4)
                else:
                    entry[f"{venue}_{key}"] = anchor
            entry[f"matches_{venue}"] = len(side)
        strengths[club_id] = entry
    return strengths, league


def _season_progress(club_matches: dict[int, list[ClubMatch]]) -> int:
    """How many matches into the season the league is: the median club's count."""
    counts = sorted(len(matches) for matches in club_matches.values())
    if not counts:
        return 0
    return counts[len(counts) // 2]


def _blended_club_matches(
    current: dict[int, list[ClubMatch]],
    prior: dict[int, list[ClubMatch]],
    parallel: dict[int, list[HistoryLayer]] | None = None,
) -> dict[int, list[tuple[ClubMatch, float]]]:
    """Weigh each club's matches by the season they belong to.

    A club's own new-season matches are the yardstick for how far last season's
    still count, so a side that has played once is still described mostly by
    last season while a side twenty matches in barely is. Last season is
    admitted for at most :data:`STRENGTH_PRIOR_WINDOW` matches' worth of
    evidence (:func:`rate_prior_scale`), so a finished season cannot outvote
    the new one merely by being longer.

    ``parallel`` (step 24) adds a cup club's league results: the league's
    current season counts as fresh evidence at the layer's weight, expressed in
    the cup's goals through the layer's factor, and pushes the priors down the
    same way the cup's own matches do; the league's last season is one more
    prior, capped and decayed like the cup's own.
    """
    parallel = parallel or {}
    blended: dict[int, list[tuple[ClubMatch, float]]] = {}
    fresh: dict[int, float] = {
        club_id: float(len(matches)) for club_id, matches in current.items()
    }
    for club_id, matches in current.items():
        ordered = sorted(matches, key=lambda m: m.scheduled_at, reverse=True)
        blended[club_id] = [
            (match, round(CLUB_RECENCY_DECAY**index, 6))
            for index, match in enumerate(ordered)
        ]
    for club_id, layers in parallel.items():
        for layer in layers:
            if layer.is_prior:
                continue
            ordered = sorted(layer.club_matches, key=lambda m: m.scheduled_at, reverse=True)
            fresh[club_id] = fresh.get(club_id, 0.0) + layer.weight * len(ordered)
            blended.setdefault(club_id, []).extend(
                (
                    scale_club_match(match, layer.factor),
                    round(layer.weight * CLUB_RECENCY_DECAY**index, 6),
                )
                for index, match in enumerate(ordered)
            )
    for club_id, matches in prior.items():
        weight = rate_prior_scale(
            len(matches),
            prior_season_weight(fresh.get(club_id, 0.0)),
            window=STRENGTH_PRIOR_WINDOW,
        )
        if weight <= 0:
            continue
        blended.setdefault(club_id, []).extend(
            (match, weight) for match in matches
        )
    for club_id, layers in parallel.items():
        for layer in layers:
            if not layer.is_prior:
                continue
            weight = layer.weight * rate_prior_scale(
                len(layer.club_matches),
                prior_season_weight(fresh.get(club_id, 0.0)),
                window=STRENGTH_PRIOR_WINDOW,
            )
            if weight <= 0:
                continue
            blended.setdefault(club_id, []).extend(
                (scale_club_match(match, layer.factor), round(weight, 6))
                for match in layer.club_matches
            )
    return blended


def _venue_strength(
    club_id: int,
    is_home: bool,
    strengths: dict[int, dict[str, float]],
    league: dict[str, float],
) -> tuple[float, float]:
    """Return (attack, defence) for a club playing home or away."""
    stats = strengths.get(club_id)
    if is_home:
        if stats is None:
            return league["home_attack"], league["home_defense"]
        return stats["home_attack"], stats["home_defense"]
    if stats is None:
        return league["away_attack"], league["away_defense"]
    return stats["away_attack"], stats["away_defense"]


# ---------------------------------------------------------------------------
# Row builder.
# ---------------------------------------------------------------------------


def _event_totals(history: list[Appearance], decay: float = 1.0) -> dict[str, float]:
    """Sum every counted event over a list of appearances, most recent first.

    With ``decay`` below 1 the i-th most recent appearance weighs ``decay**i``,
    so the sums are recency-weighted evidence rather than season totals; at
    1 they are the plain totals the leakage audit recomputes.
    """
    totals: dict[str, float] = {
        "appearances": 0.0,
        "minutes": 0.0,
        "points": 0.0,
        "goals": 0.0,
        "assists": 0.0,
        "saves": 0.0,
        "recoveries": 0.0,
        "yellows": 0.0,
        "reds": 0.0,
        "own_goals": 0.0,
        "pen_missed": 0.0,
        "pen_saved": 0.0,
        "pen_conceded": 0.0,
    }
    weight = 1.0
    for item in history:
        totals["appearances"] += weight
        totals["minutes"] += weight * item.minutes
        totals["points"] += weight * item.points
        totals["goals"] += weight * item.goals
        totals["assists"] += weight * item.assists
        totals["saves"] += weight * item.saves
        totals["recoveries"] += weight * item.ball_recoveries
        totals["yellows"] += weight * item.yellow_cards
        totals["reds"] += weight * item.red_cards
        totals["own_goals"] += weight * item.own_goals
        totals["pen_missed"] += weight * item.penalties_missed
        totals["pen_saved"] += weight * item.penalties_saved
        totals["pen_conceded"] += weight * item.penalty_conceded
        weight *= decay
    if decay >= 1.0:
        # Plain totals stay integers, which is what the audit compares.
        return {key: int(value) for key, value in totals.items()}
    return totals


def _club_participation(
    club_matches: list[ClubMatch], minutes_by_match: dict[int, int], decay: float
) -> dict[str, float]:
    """Recency-weighted shares of a club's matches a player played, started, finished."""
    played_flags = [
        minutes_by_match.get(match.match_id, 0) > 0 for match in club_matches
    ]
    start_flags = [
        minutes_by_match.get(match.match_id, 0) >= START_MINUTES_THRESHOLD
        for match in club_matches
    ]
    ninety_flags = [
        minutes_by_match.get(match.match_id, 0) >= NINETY_MINUTES_THRESHOLD
        for match in club_matches
    ]
    played, total = decayed_share(played_flags, decay)
    started, _ = decayed_share(start_flags, decay)
    ninety, _ = decayed_share(ninety_flags, decay)
    return {
        "matches": len(club_matches),
        "appearance_share": played / total if total else 0.0,
        "start_share": started / total if total else 0.0,
        "ninety_share": ninety / total if total else 0.0,
    }


def _fixture_context(
    fixture: Fixture,
    *,
    opponent_name: str,
    strengths: dict[int, dict[str, float]],
    league: dict[str, float],
) -> dict[str, Any]:
    """One match of the target tour, with the venue strengths it is played at."""
    club_attack, club_defense = _venue_strength(
        fixture.club_id, fixture.is_home, strengths, league
    )
    opponent_attack, opponent_defense = _venue_strength(
        fixture.opponent_club_id, not fixture.is_home, strengths, league
    )
    return {
        "match_id": fixture.match_id,
        "match_scheduled_at": fixture.scheduled_at.isoformat(),
        "is_home": fixture.is_home,
        "opponent_club_id": fixture.opponent_club_id,
        "opponent_name": opponent_name,
        "club_attack": club_attack,
        "club_defense": club_defense,
        "opponent_attack": opponent_attack,
        "opponent_defense": opponent_defense,
    }


def _build_row(
    *,
    player: dict[str, Any],
    fixtures: list[Fixture],
    club_name: str,
    opponent_names: dict[int, str],
    cutoff: datetime,
    target_match_ids: frozenset[int],
    appearances: list[Appearance],
    club_matches: list[ClubMatch],
    prior_appearances: list[Appearance] | None = None,
    prior_club_matches: list[ClubMatch] | None = None,
    snapshot: dict[str, Any] | None,
    strengths: dict[int, dict[str, float]],
    league: dict[str, float],
    is_newcomer: bool = False,
    newcomer_prior: RolePrior | None = None,
    role_prior: RolePrior | None = None,
    role_median_price: float | None = None,
    prior_available: bool = False,
    club_matches_by_club: dict[int, list[ClubMatch]] | None = None,
    bucket_prior: RolePrior | None = None,
    parallel_layers: Sequence[HistoryLayer] = (),
) -> dict[str, Any]:
    """Build one player's feature row from both seasons of his history.

    ``parallel_layers`` (step 24) are the player's national-league history
    when the target is a European cup: the league's current season counts as
    fresh evidence at the layer's weight, the league's last season joins the
    cup's own last season as the prior, and both are cut at the cutoff like
    everything else. Red cards are not read from them: a ban is served in the
    competition it was earned in.

    The two seasons are kept apart until the very end and then blended, because
    they answer the same questions with different authority: what happened this
    season counts in full, what happened last season counts less with every
    match played (:func:`prior_season_weight`) and for at most
    :data:`RATE_PRIOR_WINDOW` matches' worth (:func:`rate_prior_scale`). Only
    matches actually played count as appearances, so a permanent substitute
    never looks ever-present.

    ``role_prior`` is the league's average for the player's position; every
    per-90 rate is shrunk towards it with :data:`RATE_SHRINK_MATCHES`
    pseudo-matches (:func:`shrunk_per90`), so a rate built from one or two
    matches is read with the scepticism it deserves.

    ``fixtures`` is every match the club plays in the target tour, in kickoff
    order — normally one, but a fantasy tour is a slice of the calendar rather
    than a round, so a postponed match can leave a club with two of them (and
    the tour it was moved out of with none). The whole list is reported; the
    flat fixture fields describe the first one.
    """
    primary = fixtures[0]
    fixture_contexts = [
        _fixture_context(
            fixture,
            opponent_name=opponent_names.get(fixture.opponent_club_id, ""),
            strengths=strengths,
            league=league,
        )
        for fixture in fixtures
    ]
    current = played_before_cutoff(
        appearances, cutoff, exclude_match_ids=target_match_ids
    )
    prior = played_before_cutoff(
        prior_appearances or [], cutoff, exclude_match_ids=target_match_ids
    )
    # The matches the player could have featured in: his club's, or across a
    # within-season transfer the old club's until he left and the new club's
    # after (``participation_matches``). Rest days stay on the current club.
    current_clubs = (
        participation_matches(primary.club_id, current, club_matches_by_club)
        if club_matches_by_club is not None
        else club_matches
    )
    prior_clubs = prior_club_matches or []

    # The parallel leagues (step 24), each cut at the cutoff: the league's
    # current season is fresh evidence, its last season is one more prior.
    par_current: list[tuple[HistoryLayer, list[Appearance], list[ClubMatch]]] = []
    par_prior: list[tuple[HistoryLayer, list[Appearance], list[ClubMatch]]] = []
    for layer in parallel_layers:
        played = played_before_cutoff(
            layer.appearances, cutoff, exclude_match_ids=target_match_ids
        )
        clubs = sorted(
            (
                match
                for match in layer.club_matches
                if match.scheduled_at < cutoff
                and match.match_id not in target_match_ids
            ),
            key=lambda m: m.scheduled_at,
            reverse=True,
        )
        (par_prior if layer.is_prior else par_current).append((layer, played, clubs))
    par_current_played = [item for _, played, _ in par_current for item in played]
    par_prior_played = [item for _, played, _ in par_prior for item in played]

    # A newcomer is a player with no appearance in either season *before the
    # cutoff*. Judging it on the raw lists instead made a backtest's opening
    # tour treat every player who would play later in the season as a known
    # quantity with an empty history, and reserved the newcomer prior for the
    # handful who never play at all — which is exactly backwards.
    if not current and not prior and not par_current_played and not par_prior_played:
        is_newcomer = True
        newcomer_prior = newcomer_prior or role_prior

    # How much last season still counts. Rates are judged on the player's own
    # matches — a signing who has not played yet keeps last season's profile
    # intact — while whether he features is judged on his club's, because eight
    # matches spent on the bench are exactly the evidence that matters there.
    # A league match counts towards "this season" at the layer's weight.
    fresh_matches = len(current) + sum(
        layer.weight * len(played) for layer, played, _ in par_current
    )
    fresh_club_matches = len(current_clubs) + sum(
        layer.weight * len(clubs) for layer, _, clubs in par_current
    )
    rate_prior_weight = prior_season_weight(fresh_matches)
    share_prior_weight = prior_season_weight(fresh_club_matches)

    if current:
        stat_source = STAT_SOURCE_CURRENT
    elif par_current_played:
        stat_source = STAT_SOURCE_PARALLEL
    elif prior or par_prior_played or (is_newcomer and prior_available):
        stat_source = STAT_SOURCE_PRIOR
    else:
        stat_source = STAT_SOURCE_CURRENT

    row: dict[str, Any] = {
        "feature_version": FEATURE_VERSION,
        "player_season_id": player["player_season_id"],
        "fantasy_player_id": player["fantasy_player_id"],
        "player_name": player["player_name"],
        "role": player["role"],
        "club_id": primary.club_id,
        "club_name": club_name,
        "tour_cutoff": cutoff.isoformat(),
        # The label is what the frontend separates the two seasons by, so it
        # means "these numbers are last season's", not "last season is in the
        # blend". Once the player has kicked a ball this season it is his own
        # season being reported, however much last season still weighs. A
        # newcomer scored from the priors carries the label too, but only
        # when there is a last season for the priors to have come from. A
        # player whose only play this season is in his national league is
        # labelled by that league.
        "stat_source": stat_source,
        "prior_season_weight": rate_prior_weight if (prior or par_prior_played) else 0.0,
        "is_newcomer": is_newcomer,
        # Every match of the tour, plus the first one flattened onto the row so
        # a consumer that only ever expected one keeps working.
        "tour_fixtures": fixture_contexts,
        "fixture_count": len(fixture_contexts),
        "is_home": primary.is_home,
        "opponent_club_id": primary.opponent_club_id,
        "opponent_name": fixture_contexts[0]["opponent_name"],
        "match_id": primary.match_id,
        "match_scheduled_at": primary.scheduled_at.isoformat(),
    }

    # Rolling form windows run across the season boundary: while this season is
    # short the window is topped up from last one, and every new appearance
    # pushes one of last season's out, so the old numbers fade on their own.
    # The parallel leagues' matches take their place in the windows by date
    # among the cup's own; last seasons come after, cup first.
    fresh_history = (
        sorted(
            [*current, *par_current_played],
            key=lambda a: (a.scheduled_at, a.match_id),
            reverse=True,
        )
        if par_current_played
        else current
    )
    blended_history = [*fresh_history, *prior, *par_prior_played]
    for window_size in ROLLING_WINDOWS:
        stats = _window_stats(blended_history[:window_size])
        row[f"appearances_{window_size}"] = stats["appearances"]
        row[f"points_avg_{window_size}"] = stats["points_avg"]
        row[f"points_sum_{window_size}"] = stats["points_sum"]
        row[f"goals_sum_{window_size}"] = stats["goals_sum"]
        row[f"assists_sum_{window_size}"] = stats["assists_sum"]
        row[f"minutes_avg_{window_size}"] = stats["minutes_avg"]

    # Totals and per-90 rates: last season's contribution enters at its weight
    # and for at most a window of matches, which is why the totals are
    # fractional early in a season.
    now_totals = _event_totals(current)
    was_totals = _event_totals(prior)
    # The league layers at their weight, their goals in the cup's units. The
    # prior window caps the cup's and the leagues' last seasons together.
    par_now_totals = [
        _scaled_totals(_event_totals(played), layer) for layer, played, _ in par_current
    ]
    par_was_totals = [
        _scaled_totals(_event_totals(played), layer) for layer, played, _ in par_prior
    ]
    prior_pool_appearances = was_totals["appearances"] + sum(
        totals["appearances"] for totals in par_was_totals
    )
    prior_scale = rate_prior_scale(prior_pool_appearances, rate_prior_weight)
    row["prior_season_scale"] = prior_scale

    def _layered(
        key: str, own: dict[str, float], parallel: list[dict[str, float]]
    ) -> float:
        return (
            own[key]
            + sum(totals[key] for totals in parallel)
            + prior_scale * (was_totals[key] + sum(totals[key] for totals in par_was_totals))
        )

    def _blended(key: str) -> float:
        return _layered(key, now_totals, par_now_totals)

    total_minutes = _blended("minutes")
    total_points = _blended("points")
    row["current_appearances"] = now_totals["appearances"]
    row["current_minutes"] = now_totals["minutes"]
    row["current_points"] = now_totals["points"]
    row["total_appearances"] = round(_blended("appearances"), 4)
    row["total_minutes"] = round(total_minutes, 4)
    row["total_points"] = round(total_points, 4)
    # The rates are pooled from the *recency-weighted* current season (a goal
    # last week is worth more than one in August) plus last season's capped
    # share, and shrunk towards the anchor: the player's price bucket within
    # his role when one is known, else the league's role average; without
    # either (a role nobody has played yet) the target is zero, which is the
    # pre-1.6.0 behaviour of trusting the sample as it stands.
    recent_totals = _event_totals(current, CURRENT_RATE_DECAY)
    par_recent_totals = [
        _scaled_totals(_event_totals(played, CURRENT_RATE_DECAY), layer)
        for layer, played, _ in par_current
    ]
    rate_minutes = _layered("minutes", recent_totals, par_recent_totals)
    anchor = bucket_prior or role_prior or RolePrior(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    rare_anchor = role_prior or anchor
    for key in RATE_KEYS:
        row[f"{key}_per90"] = shrunk_per90(
            _layered(key, recent_totals, par_recent_totals),
            rate_minutes,
            anchor.rate(key),
        )
    for key in RARE_RATE_KEYS:
        row[f"{key}_per90"] = shrunk_per90(
            _layered(key, recent_totals, par_recent_totals),
            rate_minutes,
            rare_anchor.rate(key),
            pseudo_matches=RARE_EVENT_SHRINK_MATCHES,
        )
    row["has_history"] = bool(blended_history)

    # Appearance and start shares, blended between the two seasons. Each side is
    # capped at the same number of effective matches so a whole finished season
    # cannot outvote the one being played merely by being longer.
    now_share = _club_participation(
        current_clubs, {a.match_id: a.minutes for a in current}, CURRENT_RECENCY_DECAY
    )
    was_share = _club_participation(
        prior_clubs, {a.match_id: a.minutes for a in prior}, PRIOR_RECENCY_DECAY
    )
    # ``carry_weight`` is what any last-season assumption is still worth; the
    # prior *shares* additionally require last season's club matches to exist,
    # since without them there is no share to carry over.
    share_now_weight, carry_weight = share_blend_weights(
        len(current_clubs), share_prior_weight
    )
    # The league layers vote the same way: each current-season layer with its
    # own capped match count at the layer's weight, the last seasons pooled
    # into one prior vote by how much each of them saw.
    par_now_shares = [
        (
            layer,
            _club_participation(
                clubs, {a.match_id: a.minutes for a in played}, CURRENT_RECENCY_DECAY
            ),
        )
        for layer, played, clubs in par_current
    ]
    par_was_shares = [
        (
            layer,
            _club_participation(
                clubs, {a.match_id: a.minutes for a in played}, PRIOR_RECENCY_DECAY
            ),
        )
        for layer, played, clubs in par_prior
    ]
    any_prior_clubs = bool(prior_clubs) or any(
        share["matches"] for _, share in par_was_shares
    )
    share_prior_weight_scaled = carry_weight if any_prior_clubs else 0.0
    row["club_matches_before"] = now_share["matches"]
    row["prior_club_matches"] = was_share["matches"]

    def _share(key: str) -> float:
        votes = [(now_share[key], share_now_weight)]
        votes += [
            (share[key], float(min(share["matches"], SHARE_BLEND_WINDOW)) * layer.weight)
            for layer, share in par_now_shares
        ]
        prior_value, _ = _weighted_mean(
            [(was_share[key], float(was_share["matches"]))]
            + [
                (share[key], float(share["matches"]) * layer.weight)
                for layer, share in par_was_shares
            ]
        )
        votes.append((prior_value, share_prior_weight_scaled))
        value, _ = _weighted_mean(votes)
        return value

    appearance_share = _share("appearance_share")
    start_share = _share("start_share")
    ninety_share = _share("ninety_share")
    row["appearance_share"] = round(appearance_share, 4)
    row["start_share"] = round(start_share, 4)
    row["ninety_share"] = round(ninety_share, 4)
    row["club_matches_available"] = len(current_clubs)
    row["parallel_appearances"] = len(par_current_played)
    row["parallel_club_matches"] = sum(len(clubs) for _, _, clubs in par_current)
    row["parallel_weight"] = (
        max(layer.weight for layer in parallel_layers) if parallel_layers else 0.0
    )
    row["league_factor"] = parallel_layers[0].factor if parallel_layers else 1.0
    row["sources"] = [
        {
            "competition": None,
            "season": None,
            "kind": "own",
            "prior": False,
            "appearances": len(current),
            "club_matches": len(current_clubs),
            "weight": 1.0,
            "factor": 1.0,
        },
        {
            "competition": None,
            "season": None,
            "kind": "own",
            "prior": True,
            "appearances": len(prior),
            "club_matches": len(prior_clubs),
            "weight": round(prior_scale, 6),
            "factor": 1.0,
        },
        *[
            {
                "competition": layer.source,
                "season": layer.season_name,
                "kind": "parallel",
                "prior": layer.is_prior,
                "appearances": len(played),
                "club_matches": len(clubs),
                "weight": round(layer.weight * (prior_scale if layer.is_prior else 1.0), 6),
                "factor": layer.factor,
            }
            for layer, played, clubs in [*par_current, *par_prior]
        ],
    ]

    # Availability: the snapshot status covers injuries and published bans
    # (honouring an end date it may carry, and a "questionable" status as a
    # half chance), and a red card on the last match before the cutoff covers
    # the suspension that status may not yet (or, for a historical tour, may
    # no longer) reflect — the next match for certain, the one after at a
    # discount when the card was a straight red.
    status = snapshot["availability_status"] if snapshot else None
    description = snapshot["status_description"] if snapshot else None
    status_ok, status_factor = status_availability(
        status, description, primary.scheduled_at
    )
    card_history = recent_before_cutoff(
        [*appearances, *(prior_appearances or [])],
        cutoff,
        exclude_match_ids=target_match_ids,
    )
    red_factor = red_card_availability_factor(
        card_history,
        [*current_clubs, *prior_clubs],
    )
    red_card_suspension = red_factor <= 0.0
    availability_factor = round(status_factor * red_factor, 4)
    is_available = status_ok and not red_card_suspension
    row["availability_status"] = status
    row["status_description"] = description
    # Which snapshot the status came from: ``None`` for the season's own, a
    # league slug when a parallel league's snapshot lent it (step 24).
    row["availability_source"] = snapshot.get("availability_source") if snapshot else None
    row["price"] = snapshot["price"] if snapshot else None
    row["selected_by"] = snapshot["selected_by"] if snapshot else None
    row["form"] = snapshot["form"] if snapshot else None
    row["is_available"] = is_available
    row["red_card_suspension"] = red_card_suspension
    row["availability_factor"] = availability_factor if is_available else 0.0

    # Appearance probability and expected minutes. The probability is the share
    # above: a whole season of evidence, recency-weighted, rather than the last
    # five matches. A five-match window is why a player who misses the run-in —
    # which is most of the league's stars, rested or injured once the table is
    # settled — used to be forecast at exactly zero for the whole next season.
    # Ownership, when it is allowed to speak, can only lift the share.
    p_share = appearance_share
    owned = ownership_appearance(row["selected_by"])
    if OWNERSHIP_APPEARANCE_WEIGHT > 0 and owned is not None:
        p_share = blend(
            appearance_share,
            1.0 - OWNERSHIP_APPEARANCE_WEIGHT,
            max(appearance_share, owned),
            OWNERSHIP_APPEARANCE_WEIGHT,
        )
    p_appearance = round(p_share * availability_factor, 4) if is_available else 0.0
    row["p_appearance"] = p_appearance
    # Minutes when he does play are a *current* fact — a squad player promoted
    # to the eleven in October plays 90 minutes now whatever he averaged in
    # August — so they are recency-weighted the same way the share is.
    minute_votes = [
        (
            decayed_mean([a.minutes for a in current], CURRENT_RECENCY_DECAY),
            share_now_weight if current else 0.0,
        )
    ]
    minute_votes += [
        (
            decayed_mean([a.minutes for a in played], CURRENT_RECENCY_DECAY),
            float(min(len(clubs), SHARE_BLEND_WINDOW)) * layer.weight if played else 0.0,
        )
        for layer, played, clubs in par_current
    ]
    prior_minutes, _ = _weighted_mean(
        [(_mean([a.minutes for a in prior]), float(len(prior)))]
        + [
            (_mean([a.minutes for a in played]), float(len(played)) * layer.weight)
            for layer, played, _ in par_prior
        ]
    )
    minute_votes.append(
        (prior_minutes, carry_weight if (prior or par_prior_played) else 0.0)
    )
    mean_minutes, _ = _weighted_mean(minute_votes)
    row["expected_minutes"] = round(p_appearance * mean_minutes, 2)

    # Rest days since the club's previous match of *this* season. Before the
    # season starts there is no such match, and reporting the several-month gap
    # since last May would be meaningless, so it stays null.
    if current_clubs:
        last_match = max(current_clubs, key=lambda m: m.scheduled_at)
        row["rest_days"] = (primary.scheduled_at - last_match.scheduled_at).days
    else:
        row["rest_days"] = None

    # Club and opponent strength at the venue of the first fixture; the rest are
    # in ``tour_fixtures``, each with the venue it is actually played at.
    for key in ("club_attack", "club_defense", "opponent_attack", "opponent_defense"):
        row[key] = fixture_contexts[0][key]

    if is_newcomer and newcomer_prior is not None:
        _apply_newcomer_prior(
            row,
            newcomer_prior,
            is_available=is_available,
            # The assumption is worth NEWCOMER_PRIOR_MATCHES matches, decayed
            # like every other prior-season quantity, against every match the
            # club has actually played without him.
            prior_share=share_prior_weight * NEWCOMER_PRIOR_MATCHES,
            current_share=float(fresh_club_matches),
            p_base=newcomer_appearance_prior(
                player["role"], row["price"], role_median_price
            )
            * (availability_factor if is_available else 0.0),
        )

    return row


def _apply_newcomer_prior(
    row: dict[str, Any],
    prior: RolePrior,
    *,
    is_available: bool,
    prior_share: float = 1.0,
    current_share: float = 0.0,
    p_base: float | None = None,
) -> None:
    """Overwrite the empty history-derived features with role priors in place.

    A newcomer has no appearances at all, so every rolling / per-90 feature is
    zero. The event forecast (step 7) reads the per-90 rates, the appearance
    probability, the expected minutes and the full/sub split, so those are set
    from the position prior while the totals stay zero and ``has_history`` stays
    ``False`` (the row is explicitly flagged as a newcomer).

    The assumed appearance probability (``p_base``, by default the flat
    :data:`NEWCOMER_P_APPEARANCE`; :func:`newcomer_appearance_prior` prices it)
    is blended down the same way every other share is: an unknown signing is
    given some benefit of the doubt before a ball is kicked, but once his club
    has played matches he has not, that silence is evidence and the prior fades
    against it.
    """
    weight = (
        blend(0.0, current_share, 1.0, prior_share)
        if (current_share + prior_share) > 0
        else 1.0
    )
    base = NEWCOMER_P_APPEARANCE if p_base is None else float(p_base)
    p_appearance = round(base * weight if is_available else 0.0, 4)
    mean_minutes = max(0.0, prior.mean_minutes)
    full_ratio = min(max(mean_minutes / 90.0, 0.0), 1.0)

    row["p_appearance"] = p_appearance
    row["expected_minutes"] = round(p_appearance * mean_minutes, 2)
    row["appearance_share"] = p_appearance
    row["start_share"] = round(p_appearance * full_ratio, 4)
    row["ninety_share"] = round(row["start_share"] * prior.ninety_of_starts, 4)
    row["goals_per90"] = round(prior.goals_per90 * NEWCOMER_RATE_FACTOR, 4)
    row["assists_per90"] = round(prior.assists_per90 * NEWCOMER_RATE_FACTOR, 4)
    row["saves_per90"] = round(prior.saves_per90 * NEWCOMER_RATE_FACTOR, 4)
    row["recoveries_per90"] = round(prior.recoveries_per90 * NEWCOMER_RATE_FACTOR, 4)
    # Penalties (cards, own goals, missed penalties, conceded penalties) are
    # not discounted (staying cautious); a penalty save is a reward and is.
    row["yellows_per90"] = round(prior.yellows_per90, 4)
    row["reds_per90"] = round(prior.reds_per90, 4)
    row["own_goals_per90"] = round(prior.own_goals_per90, 4)
    row["pen_missed_per90"] = round(prior.pen_missed_per90, 4)
    row["pen_conceded_per90"] = round(prior.pen_conceded_per90, 4)
    row["pen_saved_per90"] = round(prior.pen_saved_per90 * NEWCOMER_RATE_FACTOR, 4)


def build_feature_dataset(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    competition_ref: str | None = None,
    now: datetime | None = None,
    feature_version: str = FEATURE_VERSION,
    cutoff_override: datetime | None = None,
    parallel_sources: bool = True,
) -> dict[str, Any]:
    """Build the leakage-free feature dataset for a target tour.

    Returns a JSON-serialisable report with metadata, the feature dictionary
    and one row per player whose club plays the target tour. The dataset is
    reproducible from ``(run_id, tour, feature_version)``.

    ``cutoff_override`` pulls the cutoff *earlier* than the tour's own (never
    later): it is how a tour two weeks ahead is forecast from today's
    knowledge, for a transfer plan or a backtest with a horizon, without the
    tours in between leaking into its history.

    ``parallel_sources`` switches the national leagues off for a cup target
    (step 24), which is how a backtest measures what they are worth.
    """
    generated_at = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        run = resolve_run(session, run_id, season_ref, competition_ref)
        season_id = run.season_id
        season = session.get(Season, season_id)

        tour = resolve_target_tour(session, season_id, tour_ref)
        fixtures = _load_fixtures(session, tour.id)
        cutoff = _tour_cutoff(tour, fixtures)
        if cutoff_override is not None and cutoff_override < cutoff:
            cutoff = cutoff_override
        target_match_ids = frozenset(f.match_id for f in fixtures)

        club_matches = _load_club_matches(
            session, season_id, run.id, cutoff, exclude_match_ids=target_match_ids
        )
        appearances = load_appearances(session, season_id, run.id)
        snapshots = _load_snapshots(session, run.id)
        players = _load_players(session, season_id)
        season_clubs = _load_season_clubs(session, season_id)

        # Cross-season sourcing. Last season is not a fallback for the opening
        # tour but a permanent part of the history, weighed down by how much of
        # the new season has been played (:func:`prior_season_weight`). It stops
        # being loaded once that weight can no longer move a number, which for a
        # fully played season means backtesting is unaffected. The season's
        # progress is the median club's match count, so one club's postponed
        # games neither hold the prior open nor shut it early.
        prior_run = resolve_prior_run(session, season) if season else None
        season_weight = prior_season_weight(_season_progress(club_matches))
        prior = (
            _load_prior_context(session, prior_run, cutoff)
            if prior_run is not None and season_weight >= PRIOR_SEASON_MIN_WEIGHT
            else None
        )
        cross_season = prior is not None and not club_matches

        # Parallel sourcing (step 24): when the target is a European cup, the
        # national leagues its clubs play in at the same time, each with its
        # own last season, keyed by the cross-competition identities.
        competition = (
            session.get(Competition, season.competition_id)
            if season is not None and season.competition_id is not None
            else None
        )
        parallel = (
            _load_parallel_contexts(session, season, competition, cutoff)
            if parallel_sources
            else []
        )
        season_club_ids = sorted({info["club_id"] for info in season_clubs.values()})

        strengths, league = _club_strengths(
            _blended_club_matches(
                club_matches,
                prior.club_matches if prior else {},
                parallel_club_layers(parallel, season_club_ids) if parallel else None,
            )
        )

        # League-wide role averages from both seasons: the anchor every
        # player's rates are shrunk towards, and the prior a newcomer starts
        # from when last season knows nothing about him.
        role_priors = _league_role_priors(
            appearances,
            {p["player_season_id"]: p["role"] for p in players},
            prior,
            cutoff=cutoff,
            exclude_match_ids=target_match_ids,
            parallel=parallel,
        )

        # The median price per position, the yardstick a newcomer's assumed
        # appearance probability is read against.
        prices_by_role: dict[str, list[float]] = {}
        for player in players:
            snapshot = snapshots.get(player["player_season_id"])
            if snapshot and snapshot["price"] is not None:
                prices_by_role.setdefault(player["role"], []).append(snapshot["price"])
        median_price_by_role = {
            role: sorted(prices)[len(prices) // 2]
            for role, prices in prices_by_role.items()
        }

        # Shrinkage anchors by price bucket within the role (optional): a
        # last-season player is bucketed by the price he carries *now*.
        bucket_edges: dict[str, list[float]] = {}
        bucket_priors: dict[tuple[str, int], RolePrior] = {}
        if PRICE_BUCKET_PRIORS:
            current_prices = {
                p["player_season_id"]: snapshots[p["player_season_id"]]["price"]
                for p in players
                if p["player_season_id"] in snapshots
                and snapshots[p["player_season_id"]]["price"] is not None
            }
            prior_prices: dict[int, float] = {}
            if prior is not None:
                for p in players:
                    prior_player = prior.by_player_id.get(p["player_id"])
                    price = current_prices.get(p["player_season_id"])
                    if prior_player is not None and price is not None:
                        prior_prices[prior_player.player_season_id] = price
            bucket_edges, bucket_priors = _price_bucket_priors(
                appearances,
                {p["player_season_id"]: p["role"] for p in players},
                current_prices,
                prior,
                prior_prices,
                cutoff=cutoff,
                exclude_match_ids=target_match_ids,
            )

        # Map a club id to its display name via any of its season-club rows.
        club_names: dict[int, str] = {}
        for info in season_clubs.values():
            club_names.setdefault(info["club_id"], info["display_name"])

        # Index fixtures by the club that plays in them, in kickoff order. A
        # club normally has exactly one, but a fantasy tour is a slice of the
        # calendar rather than a round: a postponed match is re-attached to
        # whichever tour it now falls in, which leaves that tour with two
        # matches for the club and the tour it came from with none.
        fixtures_by_club: dict[int, list[Fixture]] = {}
        for fixture in sorted(fixtures, key=lambda f: (f.scheduled_at, f.match_id)):
            fixtures_by_club.setdefault(fixture.club_id, []).append(fixture)
        double_fixture_clubs = sum(
            1 for club in fixtures_by_club.values() if len(club) > 1
        )

        rows: list[dict[str, Any]] = []
        players_without_fixture = 0
        newcomers = 0
        prior_sourced = 0
        parallel_sourced = 0
        players_with_parallel = 0
        availability_lent = 0
        double_fixture_rows = 0
        for player in players:
            season_club_id = player["current_season_club_id"]
            club_info = season_clubs.get(season_club_id) if season_club_id else None
            club_id = club_info["club_id"] if club_info else None
            club_fixtures = (
                fixtures_by_club.get(club_id) if club_id is not None else None
            )
            if not club_fixtures:
                players_without_fixture += 1
                continue
            if len(club_fixtures) > 1:
                double_fixture_rows += 1
            source = _resolve_history_source(
                player,
                club_id,
                appearances=appearances,
                current_club_matches=club_matches,
                prior=prior,
            )
            # The player's league half (step 24): his league appearances and
            # club results, this season and last, and the league snapshot's
            # word on whether he is fit.
            layers = (
                parallel_layers_for_player(
                    player["player_id"], parallel, club_id=club_id
                )
                if parallel
                else []
            )
            if layers:
                players_with_parallel += 1
            is_newcomer = source["is_newcomer"] and not any(
                layer.appearances for layer in layers
            )
            if is_newcomer:
                newcomers += 1
            # A newcomer is scored from the league's pooled role prior; the
            # prior-season-only one is the fallback for a role that nobody has
            # played yet this season.
            newcomer_prior = (
                role_priors.get(player["role"]) or source["newcomer_prior"]
                if is_newcomer
                else None
            )
            snapshot = snapshots.get(player["player_season_id"])
            if parallel:
                merged = merge_availability(
                    snapshot, parallel_snapshots_for_player(player["player_id"], parallel)
                )
                if merged is not snapshot:
                    availability_lent += 1
                snapshot = merged
            bucket_prior = None
            if bucket_priors and snapshot and snapshot["price"] is not None:
                bucket = price_bucket(snapshot["price"], bucket_edges.get(player["role"], []))
                if bucket is not None:
                    bucket_prior = bucket_priors.get((player["role"], bucket))
            row = _build_row(
                player=player,
                fixtures=club_fixtures,
                club_name=club_names.get(club_id, ""),
                opponent_names=club_names,
                cutoff=cutoff,
                target_match_ids=target_match_ids,
                appearances=source["appearances"],
                club_matches=source["club_matches"],
                prior_appearances=source["prior_appearances"],
                prior_club_matches=source["prior_club_matches"],
                snapshot=snapshot,
                strengths=strengths,
                league=league,
                is_newcomer=is_newcomer,
                newcomer_prior=newcomer_prior,
                role_prior=role_priors.get(player["role"]),
                role_median_price=median_price_by_role.get(player["role"]),
                prior_available=prior is not None or bool(layers),
                club_matches_by_club=club_matches,
                bucket_prior=bucket_prior,
                parallel_layers=layers,
            )
            if row["stat_source"] == STAT_SOURCE_PRIOR:
                prior_sourced += 1
            elif row["stat_source"] == STAT_SOURCE_PARALLEL:
                parallel_sourced += 1
            if row["is_newcomer"] and not is_newcomer:
                newcomers += 1
            rows.append(row)

        rows.sort(key=lambda r: (r["club_name"], -r["points_sum_5"], r["player_name"]))

        return {
            "feature_version": feature_version,
            "generated_at": generated_at.isoformat(),
            "run_id": run.id,
            "season_id": season_id,
            "season": {
                "fantasy_id": season.fantasy_season_id if season else None,
                "name": season.name if season else None,
            },
            "tour": {
                "tour_id": tour.id,
                "fantasy_tour_id": tour.fantasy_tour_id,
                "name": tour.name,
                "status": tour.status,
            },
            "cutoff": cutoff.isoformat(),
            "cross_season": cross_season,
            "prior_run_id": prior.run_id if prior else None,
            "prior_season_weight": season_weight if prior else 0.0,
            "parallel_runs": [
                {
                    "run_id": context.run_id,
                    "season_id": context.season_id,
                    "competition": context.competition_slug,
                    "competition_name": context.competition_name,
                    "season": context.season_name,
                    "factor": context.factor,
                    "weight": PARALLEL_WEIGHT,
                    "prior_run_id": context.prior.run_id if context.prior else None,
                    "prior_season": context.prior.season_name if context.prior else None,
                }
                for context in parallel
            ],
            "counts": {
                "rows": len(rows),
                "fixtures": len(target_match_ids),
                "players_without_fixture": players_without_fixture,
                "clubs_with_history": len(strengths),
                "prior_sourced": prior_sourced,
                "parallel_sourced": parallel_sourced,
                "players_with_parallel": players_with_parallel,
                "availability_lent": availability_lent,
                "newcomers": newcomers,
                "clubs_with_fixture": len(fixtures_by_club),
                "double_fixture_clubs": double_fixture_clubs,
                "double_fixture_rows": double_fixture_rows,
            },
            "feature_dictionary": list(FEATURE_DICTIONARY),
            "rows": rows,
        }


__all__ = [
    "FEATURE_VERSION",
    "ROLES",
    "ROLLING_WINDOWS",
    "START_MINUTES_THRESHOLD",
    "CURRENT_RECENCY_DECAY",
    "PRIOR_RECENCY_DECAY",
    "PRIOR_SEASON_HALF_LIFE",
    "PRIOR_SEASON_MIN_WEIGHT",
    "RATE_PRIOR_WINDOW",
    "RATE_SHRINK_MATCHES",
    "STRENGTH_PRIOR_WINDOW",
    "STRENGTH_SHRINK_MATCHES",
    "STRENGTH_VENUE_MODE",
    "SHARE_BLEND_WINDOW",
    "CURRENT_RATE_DECAY",
    "CLUB_RECENCY_DECAY",
    "RARE_EVENT_SHRINK_MATCHES",
    "PRICE_BUCKET_PRIORS",
    "OWNERSHIP_APPEARANCE_WEIGHT",
    "STRAIGHT_RED_SECOND_MATCH_SHARE",
    "QUESTIONABLE_APPEARANCE_FACTOR",
    "QUESTIONABLE_STATUSES",
    "NINETY_MINUTES_THRESHOLD",
    "RATE_KEYS",
    "RARE_RATE_KEYS",
    "red_card_ban",
    "red_card_availability_factor",
    "parse_status_date",
    "status_availability",
    "ownership_appearance",
    "participation_matches",
    "price_bucket",
    "price_bucket_edges",
    "UNAVAILABLE_STATUSES",
    "STAT_SOURCE_CURRENT",
    "STAT_SOURCE_PRIOR",
    "STAT_SOURCE_PARALLEL",
    "PARALLEL_TARGET_SLUGS",
    "NON_LEAGUE_SLUGS",
    "PARALLEL_WEIGHT",
    "LEAGUE_STRENGTH",
    "DEFAULT_LEAGUE_STRENGTH",
    "HistoryLayer",
    "ParallelContext",
    "league_strength",
    "scale_club_match",
    "parallel_season_overlaps",
    "merge_availability",
    "resolve_parallel_runs",
    "parallel_layers_for_player",
    "parallel_snapshots_for_player",
    "parallel_club_layers",
    "NEWCOMER_P_APPEARANCE",
    "NEWCOMER_P_APPEARANCE_GOALKEEPER",
    "NEWCOMER_PRICE_SLOPE",
    "NEWCOMER_PRIOR_MATCHES",
    "NEWCOMER_RATE_FACTOR",
    "newcomer_appearance_prior",
    "FEATURE_DICTIONARY",
    "FeaturesError",
    "Appearance",
    "ClubMatch",
    "Fixture",
    "RolePrior",
    "PriorPlayer",
    "PriorContext",
    "blend",
    "decayed_mean",
    "decayed_share",
    "prior_season_weight",
    "rate_prior_scale",
    "shrunk_per90",
    "share_blend_weights",
    "recent_before_cutoff",
    "played_before_cutoff",
    "pending_red_card_suspension",
    "per90",
    "load_appearances",
    "resolve_prior_run",
    "resolve_run",
    "resolve_target_tour",
    "build_feature_dataset",
]
