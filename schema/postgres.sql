-- Prototype PostgreSQL schema derived from live Sports.ru GraphQL responses.

CREATE TABLE ingestion_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trigger_type text NOT NULL DEFAULT 'manual',
    tournament_slug text NOT NULL,
    requested_season_id text,
    status text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error_message text,
    report jsonb,
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed'))
);

CREATE TABLE raw_api_responses (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id bigint NOT NULL REFERENCES ingestion_runs(id),
    operation_name text NOT NULL,
    variables jsonb NOT NULL,
    response jsonb NOT NULL,
    response_hash text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ingestion_run_id, operation_name, response_hash)
);

CREATE TABLE competitions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fantasy_tournament_id text NOT NULL UNIQUE,
    slug text NOT NULL UNIQUE,
    name text NOT NULL
);

CREATE TABLE seasons (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    competition_id bigint NOT NULL REFERENCES competitions(id),
    fantasy_season_id text NOT NULL UNIQUE,
    stat_season_id text NOT NULL UNIQUE,
    name text NOT NULL,
    is_active boolean NOT NULL,
    starts_at timestamptz,
    ends_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE season_rules (
    season_id bigint PRIMARY KEY REFERENCES seasons(id) ON DELETE CASCADE,
    rules_html text NOT NULL,
    total_budget numeric(8, 2) NOT NULL,
    total_players integer NOT NULL,
    starting_players integer NOT NULL,
    full_roster_constraints jsonb NOT NULL,
    starting_roster_constraints jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE clubs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stat_team_id text NOT NULL UNIQUE,
    canonical_name text NOT NULL
);

CREATE TABLE season_clubs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    season_id bigint NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    club_id bigint NOT NULL REFERENCES clubs(id),
    fantasy_team_id text NOT NULL,
    display_name text NOT NULL,
    UNIQUE (season_id, club_id),
    UNIQUE (season_id, fantasy_team_id)
);

CREATE TABLE players (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stat_player_id text UNIQUE,
    canonical_name text NOT NULL
);

CREATE TABLE player_seasons (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    season_id bigint NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players(id),
    fantasy_player_id text NOT NULL,
    role text NOT NULL,
    current_season_club_id bigint REFERENCES season_clubs(id),
    UNIQUE (season_id, fantasy_player_id),
    UNIQUE (season_id, player_id),
    CHECK (role IN ('GOALKEEPER', 'DEFENDER', 'MIDFIELDER', 'FORWARD'))
);

CREATE TABLE fantasy_tours (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    season_id bigint NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    fantasy_tour_id text NOT NULL,
    name text NOT NULL,
    status text NOT NULL,
    starts_at timestamptz,
    finishes_at timestamptz,
    transfers_start_at timestamptz,
    transfers_deadline_at timestamptz,
    total_transfers integer,
    max_same_team_players integer,
    UNIQUE (season_id, fantasy_tour_id)
);

CREATE TABLE matches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    season_id bigint NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    tour_id bigint REFERENCES fantasy_tours(id),
    stat_match_id text NOT NULL UNIQUE,
    scheduled_at timestamptz NOT NULL,
    home_club_id bigint NOT NULL REFERENCES clubs(id),
    away_club_id bigint NOT NULL REFERENCES clubs(id),
    home_score integer,
    away_score integer,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX matches_season_scheduled_idx
    ON matches (season_id, scheduled_at);

CREATE TABLE fantasy_player_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_season_id bigint NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    ingestion_run_id bigint NOT NULL REFERENCES ingestion_runs(id),
    season_club_id bigint REFERENCES season_clubs(id),
    captured_at timestamptz NOT NULL DEFAULT now(),
    price numeric(6, 2) NOT NULL,
    availability_status text NOT NULL,
    status_description text NOT NULL DEFAULT '',
    selected_by numeric(10, 6),
    form integer,
    rank integer,
    season_score integer,
    average_score numeric(10, 4),
    last_tour_score integer,
    top_percent numeric(10, 6),
    UNIQUE (player_season_id, ingestion_run_id)
);

CREATE INDEX fantasy_player_snapshots_latest_idx
    ON fantasy_player_snapshots (player_season_id, captured_at DESC);

CREATE TABLE player_season_stats (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_season_id bigint NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    ingestion_run_id bigint NOT NULL REFERENCES ingestion_runs(id),
    captured_at timestamptz NOT NULL DEFAULT now(),
    points integer NOT NULL,
    goals integer NOT NULL,
    assists integer NOT NULL,
    saves integer NOT NULL,
    penalties_missed integer NOT NULL,
    penalties_post integer NOT NULL,
    penalties_target integer NOT NULL,
    penalties_saved integer NOT NULL,
    field_minutes integer NOT NULL,
    yellow_cards integer NOT NULL,
    red_cards integer NOT NULL,
    goals_conceded integer NOT NULL,
    penalty_goals_conceded integer NOT NULL,
    penalties_faced integer NOT NULL,
    penalty_conceded integer NOT NULL,
    own_goals integer NOT NULL,
    ball_recoveries integer NOT NULL,
    UNIQUE (player_season_id, ingestion_run_id)
);

CREATE TABLE player_match_stats (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_season_id bigint NOT NULL REFERENCES player_seasons(id) ON DELETE CASCADE,
    match_id bigint NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    tour_id bigint NOT NULL REFERENCES fantasy_tours(id),
    season_club_id bigint REFERENCES season_clubs(id),
    ingestion_run_id bigint NOT NULL REFERENCES ingestion_runs(id),
    points integer NOT NULL,
    goals integer NOT NULL,
    assists integer NOT NULL,
    saves integer NOT NULL,
    penalties_missed integer NOT NULL,
    penalties_post integer NOT NULL,
    penalties_target integer NOT NULL,
    penalties_saved integer NOT NULL,
    field_minutes integer NOT NULL,
    yellow_cards integer NOT NULL,
    red_cards integer NOT NULL,
    goals_conceded integer NOT NULL,
    penalty_goals_conceded integer NOT NULL,
    penalties_faced integer NOT NULL,
    penalty_conceded integer NOT NULL,
    own_goals integer NOT NULL,
    ball_recoveries integer NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (player_season_id, match_id)
);

CREATE INDEX player_match_stats_player_idx
    ON player_match_stats (player_season_id, match_id);

CREATE TABLE fantasy_point_details (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_match_stat_id bigint NOT NULL
        REFERENCES player_match_stats(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    reason text NOT NULL,
    score integer NOT NULL,
    UNIQUE (player_match_stat_id, ordinal)
);

CREATE TABLE club_season_stats (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    season_club_id bigint NOT NULL REFERENCES season_clubs(id) ON DELETE CASCADE,
    ingestion_run_id bigint NOT NULL REFERENCES ingestion_runs(id),
    captured_at timestamptz NOT NULL DEFAULT now(),
    matches_played integer NOT NULL,
    matches_won integer NOT NULL,
    matches_drawn integer NOT NULL,
    matches_lost integer NOT NULL,
    goals_scored integer NOT NULL,
    goals_conceded integer NOT NULL,
    yellow_cards integer NOT NULL,
    red_cards integer NOT NULL,
    clean_sheets integer,
    home_matches integer,
    home_goals_scored integer,
    home_goals_conceded integer,
    away_matches integer,
    away_goals_scored integer,
    away_goals_conceded integer,
    UNIQUE (season_club_id, ingestion_run_id)
);

CREATE TABLE club_match_stats (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_id bigint NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    club_id bigint NOT NULL REFERENCES clubs(id),
    opponent_club_id bigint NOT NULL REFERENCES clubs(id),
    is_home boolean NOT NULL,
    goals_scored integer,
    goals_conceded integer,
    provider_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingestion_run_id bigint NOT NULL REFERENCES ingestion_runs(id),
    UNIQUE (match_id, club_id)
);
